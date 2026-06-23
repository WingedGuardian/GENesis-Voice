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

import logging
import os
import time
from datetime import UTC, datetime

from speechmatics.rt import (
    AsyncClient,
    AudioEncoding,
    AudioFormat,
    ServerMessageType,
    SpeakerDiarizationConfig,
    TranscriptionConfig,
)

from .active_transcript import TranscriptAccumulator

logger = logging.getLogger("ambient.active")


class ActiveSession:
    """One cloud transcription session, tied to one device WS connection."""

    def __init__(self, cfg, *, source: str) -> None:
        self._cfg = cfg
        self._source = source
        ts = datetime.now(UTC)
        os.makedirs(cfg.active_output_dir, exist_ok=True)
        self._path = os.path.join(cfg.active_output_dir, ts.strftime("%Y%m%dT%H%M%S") + ".md")
        self._acc = TranscriptAccumulator(
            title=f"Active listen {ts.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        self._client: AsyncClient | None = None
        self._last_partial = 0.0
        self._finalized = False

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
        client.on(ServerMessageType.ADD_TRANSCRIPT, lambda m: (self._acc.add_final(m), self._flush()))
        client.on(ServerMessageType.ADD_PARTIAL_TRANSCRIPT,
                  lambda m: (self._acc.set_partial(m), self._flush_partial()))
        client.on(ServerMessageType.ERROR, lambda m: logger.error("Speechmatics ERROR: %s", str(m)[:200]))
        client.on(ServerMessageType.WARNING, lambda m: logger.warning("Speechmatics WARNING: %s", str(m)[:200]))
        self._flush()  # header now, so the file exists for tailing immediately
        audio_format = AudioFormat(encoding=AudioEncoding.PCM_S16LE, sample_rate=16000, chunk_size=4096)
        config = TranscriptionConfig(
            language=self._cfg.active_language,
            model=self._cfg.active_model,
            max_delay=self._cfg.active_max_delay,
            enable_partials=True,
            diarization="speaker",
            speaker_diarization_config=SpeakerDiarizationConfig(max_speakers=self._cfg.active_max_speakers),
        )
        await client.start_session(transcription_config=config, audio_format=audio_format)
        self._client = client

    async def send_audio(self, frame: bytes) -> None:
        if self._client is not None:
            await self._client.send_audio(frame)

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
