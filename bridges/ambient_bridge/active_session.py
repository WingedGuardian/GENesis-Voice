"""Active-mode (cloud) transcription session.

When the device signals mode=active (via the HTTP control endpoint), the bridge opens an
ActiveSession: a Speechmatics realtime session that relays the connection's 16k PCM and
writes a live-updating, diarized transcript file. Passive (local Zipformer) is the default
— active is entered ONLY on an explicit control signal, and every cloud-session open/close
is loud-logged (privacy guard: ambient audio never reaches the cloud without this).

Uses the speechmatics-rt SDK's MANUAL lifecycle (start_session/send_audio/stop_session/close)
so the session opens mid-handler cleanly (not the context manager).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from datetime import UTC, datetime

from speechmatics.rt import (
    AsyncClient,
    AudioEncoding,
    AudioFormat,
    ServerMessageType,
    SpeakerDiarizationConfig,
    TranscriptionConfig,
)

from .active_speaker_id import ActivePcmRing, SpeakerResolver
from .active_transcript import TranscriptAccumulator, runs_with_spans

logger = logging.getLogger("ambient.active")


def _diar_kwargs(cfg) -> dict:
    """Build SpeakerDiarizationConfig kwargs from config, OMITTING unset (None) fields so the
    SDK applies its own defaults. max_speakers None → auto-detect (no cap). Pure (no SDK types)
    so it unit-tests without the speechmatics SDK installed."""
    kw: dict = {}
    if cfg.active_max_speakers is not None:
        kw["max_speakers"] = cfg.active_max_speakers
    if cfg.active_prefer_current_speaker is not None:
        kw["prefer_current_speaker"] = cfg.active_prefer_current_speaker
    if cfg.active_speaker_sensitivity is not None:
        kw["speaker_sensitivity"] = cfg.active_speaker_sensitivity
    return kw


class ActiveSession:
    """One cloud transcription session, tied to one device WS connection."""

    def __init__(self, cfg, *, source: str, speaker_id=None) -> None:
        self._cfg = cfg
        self._source = source
        ts = datetime.now(UTC)
        os.makedirs(cfg.active_output_dir, exist_ok=True)
        # Microsecond stamp + a short random suffix so two sessions opened in the same second
        # (or microsecond) never collide — with session-per-meeting, many transcripts land in one
        # dir per day, and a second-granular name would silently overwrite the earlier meeting.
        self._path = os.path.join(
            cfg.active_output_dir,
            ts.strftime("%Y%m%dT%H%M%S_%f") + "_" + uuid.uuid4().hex[:6] + ".md",
        )
        self._acc = TranscriptAccumulator(
            title=f"Active listen {ts.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        self._client: AsyncClient | None = None
        self._last_partial = 0.0
        # Monotonic ts of the last ASR SPEECH EVIDENCE — a non-empty partial or a committed final.
        # Exposed via `last_activity` for liveness consumers (the meeting bridge's transcript-idle
        # close): partials are the earliest "someone is talking" signal, since finals can lag many
        # seconds — or never commit at all on quiet/far-field audio.
        self._last_activity: float | None = None
        # Last partial TEXT seen — evidence requires the partial to CHANGE (new words being heard).
        # Speechmatics re-emits the same trailing partial for as long as audio keeps flowing, so a
        # frozen partial over post-speech noise must not read as a live meeting.
        self._last_partial_text = ""
        self._finalized = False
        # Set once audio streaming begins (end of start()) — the session's t=0, against which a
        # user marker's elapsed time is measured so its [ts] aligns with the Speechmatics ones.
        self._t0: float | None = None
        # --- speaker IDENTITY (relabel S1/S2 → enrolled names) — off → zero overhead ---
        self._sid = speaker_id if (
            getattr(cfg, "active_speaker_id_enabled", False)
            and speaker_id is not None and speaker_id.has_user()
        ) else None
        self._ring = ActivePcmRing(cfg.active_speaker_ring_s) if self._sid else None
        self._resolver = SpeakerResolver(
            min_speaker_s=cfg.active_min_speaker_s, recheck_s=cfg.active_recheck_s,
            embed_window_s=cfg.active_embed_window_s) if self._sid else None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_resolve = 0.0  # monotonic; debounces how often a resolve pass is scheduled
        self._resolve_lock = asyncio.Lock()  # serializes resolve passes (periodic + finalize)

    @property
    def path(self) -> str:
        return self._path

    @property
    def turns(self) -> int:
        return len(self._acc.committed)

    async def start(self) -> None:
        try:
            key = open(self._cfg.active_sm_key_path).read().strip()
        except FileNotFoundError:
            logger.error("ACTIVE mode requested but Speechmatics key missing at %s — "
                         "no transcript will be produced", self._cfg.active_sm_key_path)
            # Make the failure VISIBLE in the transcript itself, not just a silent empty file.
            self._acc.committed.append(
                ("SYSTEM", 0.0, f"active mode unavailable — Speechmatics key missing at "
                                f"{self._cfg.active_sm_key_path}"))
            self._flush()
            return
        # LOUD privacy guard: every cloud-session open is asserted + logged.
        logger.warning("ACTIVE/CLOUD session OPENING for %s → Speechmatics (transcript: %s)",
                       self._source, self._path)
        client = AsyncClient(api_key=key)
        client.on(ServerMessageType.ADD_TRANSCRIPT, self._on_final)
        client.on(ServerMessageType.ADD_PARTIAL_TRANSCRIPT, self._on_partial)
        client.on(ServerMessageType.ERROR, lambda m: logger.error("Speechmatics ERROR: %s", str(m)[:200]))
        client.on(ServerMessageType.WARNING, lambda m: logger.warning("Speechmatics WARNING: %s", str(m)[:200]))
        self._flush()  # header now, so the file exists for tailing immediately
        audio_format = AudioFormat(encoding=AudioEncoding.PCM_S16LE, sample_rate=16000, chunk_size=4096)
        diar_kwargs = _diar_kwargs(self._cfg)
        logger.info("active diarization config: max_speakers=%s (auto if absent), %s",
                    diar_kwargs.get("max_speakers", "AUTO"), diar_kwargs)
        config = TranscriptionConfig(
            language=self._cfg.active_language,
            model=self._cfg.active_model,
            max_delay=self._cfg.active_max_delay,
            enable_partials=True,
            diarization="speaker",
            speaker_diarization_config=SpeakerDiarizationConfig(**diar_kwargs),
        )
        await client.start_session(transcription_config=config, audio_format=audio_format)
        self._client = client
        self._t0 = time.monotonic()  # audio is about to flow → session clock starts here
        # Capture the loop the SDK callbacks fire on (verified: speechmatics-rt emits on the recv
        # task's loop), so _on_final can schedule the off-loop resolve from inside a sync callback.
        self._loop = asyncio.get_running_loop()

    @property
    def last_activity(self) -> float | None:
        """Monotonic ts of the last ASR speech evidence (non-empty partial or committed final);
        None until the first. The meeting bridge gates its transcript-idle close on this."""
        return self._last_activity

    def _on_final(self, msg: dict) -> None:
        """ADD_TRANSCRIPT handler (sync, on the SDK's event loop): commit + flush, then maybe
        kick a debounced, off-loop speaker-ID resolve pass. Counts as speech evidence ONLY when
        the final creates a NEW committed entry: verified against the live API, Speechmatics emits
        EMPTY finals every couple of seconds for as long as audio flows (a commit heartbeat), and
        those must not keep a noise-only session's liveness fresh. A same-speaker continuation
        final MERGES into the last entry (len unchanged) and deliberately doesn't stamp either —
        its words already stamped as partials, which are the primary evidence signal."""
        before = len(self._acc.committed)
        self._acc.add_final(msg)
        if len(self._acc.committed) != before:
            self._last_activity = time.monotonic()
            self._last_partial_text = ""  # partial restarts after a commit — the next one counts anew
        self._flush()
        self._maybe_resolve()

    def _on_partial(self, msg: dict) -> None:
        """ADD_PARTIAL_TRANSCRIPT handler (sync, on the SDK's loop): live-preview + liveness.
        Same accumulator/flush behavior as the previous inline lambda; additionally counts a
        CHANGED non-empty partial as speech evidence (same text extraction as
        TranscriptAccumulator.set_partial). The change requirement is load-bearing: verified
        against the live API, Speechmatics re-emits the same trailing partial for as long as
        audio keeps flowing, so with noise after speech a frozen partial would otherwise keep a
        session's liveness fresh forever. New words → changed text → evidence."""
        text = ((msg.get("metadata") or {}).get("transcript") or "").strip()
        if text and text != self._last_partial_text:
            self._last_partial_text = text
            self._last_activity = time.monotonic()
        self._acc.set_partial(msg)
        self._flush_partial()

    async def send_audio(self, frame: bytes) -> None:
        if self._client is not None:
            await self._client.send_audio(frame)
        if self._ring is not None:  # buffer AFTER relaying (append is O(1); never delays audio)
            self._ring.append(frame)

    # --- speaker IDENTITY resolution (off the event loop) --------------------------------------

    def _maybe_resolve(self) -> None:
        """Schedule a resolve pass if enabled, due (debounced), and none is already running.
        Sync, on the loop — cheap guard; the heavy embed runs in a worker thread."""
        if self._resolver is None or self._loop is None or self._resolve_lock.locked():
            return
        now = time.monotonic()
        if now - self._last_resolve < self._cfg.active_resolve_interval_s:
            return
        self._last_resolve = now
        self._loop.create_task(self._resolve_task())

    async def _resolve_task(self) -> None:
        async with self._resolve_lock:  # one resolve at a time (vs. a later pass or finalize)
            await self._run_resolution()

    async def _run_resolution(self) -> None:
        try:
            runs = list(self._acc.committed)        # snapshot on the loop (same loop → no lock)
            now_elapsed = self._ring.elapsed_s
            changed = await asyncio.to_thread(self._resolve_blocking, runs, now_elapsed)
            if changed:
                self._apply_labels()                 # back on the loop
                self._flush()
                for s in changed:
                    logger.info("active speaker-id: %s → %s (%s)", s,
                                self._resolver.assigned.get(s, "(positional)"), self._source)
        except Exception:  # noqa: BLE001
            logger.warning("active speaker-id resolve failed for %s", self._source, exc_info=True)

    def _resolve_blocking(self, runs, now_elapsed: float) -> set[str]:
        """Worker-thread body: slice the ring + embed (blocking ONNX) + match → update the map."""
        spans = runs_with_spans(runs, now_elapsed)
        return self._resolver.resolve(
            spans, now_elapsed=now_elapsed,
            embed_spans_fn=self._embed_spans, match_fn=self._match)

    def _embed_spans(self, spans: list[tuple[float, float]]):
        """(worker thread) Slice each span from the ring → embed → mean centroid, or None."""
        embs = []
        for start_s, end_s in spans:
            samp = self._ring.slice(start_s, end_s)
            if samp is None:
                continue
            e = self._sid.embed(samp)
            if e is not None:
                embs.append(e)
        return self._sid.mean_embedding(embs) if embs else None

    def _match(self, emb):
        """(worker thread) Best enrolled match for an embedding, at the active threshold."""
        return self._sid.best_match(emb, self._cfg.active_user_verify_threshold)

    def _apply_labels(self) -> None:
        """(loop) Translate the resolver's registry-name map → transcript DISPLAY names."""
        self._acc.labels = {
            s: (self._cfg.active_user_display_name if name == self._cfg.user_speaker_name else name)
            for s, name in self._resolver.assigned.items()
        }

    def add_marker(self) -> None:
        """Drop a user bookmark into the live transcript at the current session time.
        Called from the /marker HTTP handler (single-press) — same event loop as the audio
        relay + Speechmatics callbacks, so no lock is needed. Elapsed is measured from _t0
        (audio start); falls back to 0.0 if the session never opened (e.g. missing key)."""
        elapsed = (time.monotonic() - self._t0) if self._t0 is not None else 0.0
        self._acc.add_marker(elapsed)
        self._flush()
        logger.info("transcript MARKER dropped for %s at %.1fs (%s)",
                    self._source, elapsed, self._path)

    async def finalize(self) -> None:
        if self._finalized:  # idempotent: one CLOSED log per session (clean audit trail)
            return
        self._finalized = True
        if self._client is not None:
            try:
                await self._client.stop_session()
            except Exception:  # noqa: BLE001
                logger.warning("active stop_session failed for %s", self._source, exc_info=True)
            try:
                await self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
        if self._resolver is not None:  # one last identity pass (awaits any in-flight one)
            with contextlib.suppress(Exception):
                async with self._resolve_lock:
                    await self._run_resolution()
        self._flush()
        logger.warning("ACTIVE/CLOUD session CLOSED for %s (transcript: %s, turns=%d)",
                       self._source, self._path, self.turns)

    def _flush(self) -> None:
        try:
            tmp = self._path + ".tmp"
            with open(tmp, "w") as f:
                f.write(self._acc.render())
            os.replace(tmp, self._path)
        except Exception:  # noqa: BLE001
            logger.warning("active transcript write failed for %s", self._path, exc_info=True)

    def _flush_partial(self) -> None:
        # Partials fire up to ~2/s; bound event-loop file I/O so a slow edge disk can't
        # backpressure audio relay. Provisional anyway — ~1/s suits a live tail.
        now = time.monotonic()
        if now - self._last_partial >= 1.0:
            self._last_partial = now
            self._flush()
