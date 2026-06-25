"""Ambient bridge WebSocket server (standalone — runs on the bridge VM).

Accepts the SAME wire contract the Voice PE firmware already speaks: raw BINARY
WS frames = 16-bit mono PCM (24 kHz by default, see config), JSON text frames for
control. Each connection gets its OWN pipeline (fresh VAD state). Silent Stage-1:
capture → transcribe → (deferred) diarize → store to the isolated ambient.db. No
Genesis contact.

Run on the VM:  python -m ambient_bridge.server
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np
import websockets
from aiohttp import web

from .active_session import ActiveSession
from .config import AmbientConfig, load_config
from .connection_stats import ConnectionStats
from .pipeline import AmbientEngine, DiarizationEngine, DiarWindow, _autodetect_embedding
from .speaker_id import SpeakerIDRegistry
from .store import AmbientStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("ambient.server")


def _enable_tcp_keepalive(sock, cfg: AmbientConfig) -> None:
    """Enable + tune TCP keep-alive on an accepted client socket so a silently-dead
    (half-open) peer is detected in ~idle + intvl*cnt seconds (minutes) instead of the OS
    default (~2h). Needed because WS server PINGs are disabled (the Voice PE rejects them),
    which otherwise leaves dead sockets ESTAB for ~2h and inflates active_connections.
    Best-effort: a missing socket or an unsupported option is logged, never fatal."""
    if sock is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for opt, val in (
            ("TCP_KEEPIDLE", cfg.keepalive_idle_s),
            ("TCP_KEEPINTVL", cfg.keepalive_intvl_s),
            ("TCP_KEEPCNT", cfg.keepalive_cnt),
        ):
            if hasattr(socket, opt):
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)
    except OSError as exc:
        logger.warning("could not set TCP keep-alive on client socket: %s", exc)


@dataclass
class _EnrollSession:
    """In-flight online-enrollment state. `samples` is appended by the pipeline worker
    thread (under `lock`) and snapshotted by the async watcher, which owns all lifecycle
    transitions (start/finalize/abort) — so the capture hot path only ever buffers."""

    id: str
    name: str
    target_s: float
    samples: list[np.ndarray] = field(default_factory=list)
    total_dur: float = 0.0
    start_time: float = 0.0          # event-loop clock at session start (wallclock-faithful timeout)
    done: bool = False               # set under lock at finalize so the worker tap stops appending
    lock: threading.Lock = field(default_factory=threading.Lock)


def tracked_task(coro, *, name: str) -> asyncio.Task:
    """Like asyncio.create_task but logs exceptions at ERROR (no silent failures)."""
    t = asyncio.create_task(coro, name=name)

    def _done(fut: asyncio.Future) -> None:
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc:
            logger.error("Background task %s crashed: %r", name, exc, exc_info=exc)

    t.add_done_callback(_done)
    return t


class AmbientServer:
    def __init__(self, cfg: AmbientConfig) -> None:
        self._cfg = cfg
        self._store = AmbientStore(cfg.db_path)
        self._engine = AmbientEngine(cfg, self._store)
        self._active = 0
        self._utterances_total = 0
        self._frames_dropped = 0  # inbound audio frames shed under sustained overload (memory bound)
        self._last_connection_ts: str | None = None  # last client connect (for the health monitor)
        # Connection telemetry — records device connect/disconnect + dark gaps (the wedge signal),
        # persists across bridge restarts, surfaces in ambient_health.json.
        self._conn_stats = ConnectionStats(
            events_path=cfg.conn_events_path, stats_path=cfg.conn_stats_path,
            dark_threshold_s=cfg.conn_dark_threshold_s, events_max=cfg.conn_events_max)
        # --- diarization (deferred, additive; capture works fine without it) ---
        self._diar: DiarizationEngine | None = None
        self._diar_queue: asyncio.Queue[DiarWindow] | None = None
        self._window_counter = 0
        self._diar_dropped = 0
        self._diar_worker_alive = False
        if cfg.diar_enabled:
            try:
                self._diar = DiarizationEngine(cfg)
                self._engine.enable_diarization(
                    self.submit_window, int(cfg.diar_window_s * cfg.model_sample_rate),
                )
                if cfg.diar_num_threads + cfg.num_threads > (os.cpu_count() or 4):
                    logger.warning("diar_num_threads(%d) + num_threads(%d) > cpu_count(%s) — "
                                   "STT may contend with diarization under load",
                                   cfg.diar_num_threads, cfg.num_threads, os.cpu_count())
            except Exception:
                logger.warning("Diarization init failed — running capture-only "
                               "(speaker_label stays NULL)", exc_info=True)
                self._diar = None
        # --- speaker identification (Stage-A, additive) — needs the diar worker (cluster
        # aggregation runs there). No registry / no enrolled voiceprint → is_user stays NULL. ---
        self._speaker_id: SpeakerIDRegistry | None = None
        if cfg.speaker_id_enabled and self._diar is not None:
            try:
                model = cfg.speaker_id_model or _autodetect_embedding(cfg.models_dir)
                # Instantiate the registry even with NO voiceprint yet, so online enrollment
                # can populate it. VERIFICATION gates on has_user() at call time (no voiceprint
                # → is_user / speaker_name stay NULL; capture is unaffected).
                self._speaker_id = SpeakerIDRegistry(
                    model, persist_path=cfg.speaker_registry_path,
                    num_threads=cfg.diar_num_threads, user_name=cfg.user_speaker_name,
                )
                if self._speaker_id.has_user():
                    logger.info("Speaker-ID enabled (user=%r voiceprint loaded)", cfg.user_speaker_name)
                else:
                    logger.warning("Speaker-ID on but no %r voiceprint yet in %s — verdicts stay "
                                   "NULL until enrolled (python -m ambient_bridge.enroll [--online])",
                                   cfg.user_speaker_name, cfg.speaker_registry_path)
            except Exception:
                logger.warning("Speaker-ID init failed — verdicts stay NULL", exc_info=True)
        # --- online enrollment (no-teardown), additive — needs the registry to enroll into ---
        self._enroll: _EnrollSession | None = None
        self._enroll_last_id: str | None = None
        if self._speaker_id is not None:
            self._engine.enable_enroll(self._collect_enroll)
        # Track active handler tasks so shutdown awaits their flush before closing the store.
        self._handler_tasks: set[asyncio.Task] = set()
        # ACTIVE/PASSIVE listening mode (bridge-level; one device → one connection). Set by the
        # HTTP control endpoint. Default PASSIVE so a dropped/late mode POST fails safe to LOCAL.
        self._mode = "passive"
        # Bridge-level pointer to the live ActiveSession (if any), so the /marker HTTP handler can
        # reach it from outside the per-connection _handler scope. Mirrors the _handler's local
        # `active` at the 3 points it changes (open / passive-flip / disconnect). One device → one
        # connection, so a single ref suffices; None when no cloud session is open.
        self._active_session: ActiveSession | None = None

    # --- diarization plumbing -------------------------------------------------

    async def submit_window(self, window: DiarWindow) -> None:
        """Enqueue a closed window for deferred diarization. Runs on the event loop
        (the pipeline awaits this), so the bounded queue is touched only here + the
        worker — never from a worker thread. Drop-oldest if full (labels stay NULL)."""
        if self._diar_queue is None:
            return
        self._window_counter += 1
        window.window_idx = self._window_counter
        if self._diar_queue.full():
            # No await between full() and put_nowait() → the event loop can't switch,
            # so this evict+insert is effectively atomic.
            dropped = self._diar_queue.get_nowait()
            self._diar_dropped += 1
            logger.warning("diar queue full — dropped window w%d (%d utts)",
                           dropped.window_idx, len(dropped.spans))
        self._diar_queue.put_nowait(window)

    @staticmethod
    def _overlap_cluster(start_s: float, end_s: float,
                         segs: list[tuple[float, float, int]]) -> int | None:
        """The diar cluster id with the most temporal overlap with [start_s, end_s]
        (WhisperX-style intersection), or None if no segment overlaps."""
        best, best_ov = None, 0.0
        for ss, se, spk in segs:
            ov = max(0.0, min(end_s, se) - max(start_s, ss))
            if ov > best_ov:
                best_ov, best = ov, spk
        return best

    def _assign_labels(self, window: DiarWindow, segs: list[tuple[float, float, int]]) -> None:
        """Map each utterance to its max-overlap speaker cluster. Label = wN:c/total —
        N global window, c cluster, total #clusters in the window (so '1-speaker
        confirmed' is distinguishable from a lumped cluster)."""
        total = len({spk for _, _, spk in segs}) or 1
        labeled = 0
        for row_id, start_s, end_s in window.spans:
            best = self._overlap_cluster(start_s, end_s, segs)
            if best is not None:
                self._store.set_speaker_label(row_id, f"w{window.window_idx}:{best}/{total}")
                labeled += 1
        logger.info("diar w%d: %d speaker(s), labeled %d/%d utts",
                    window.window_idx, total, labeled, len(window.spans))

    def _verify_speaker_identities(self, window: DiarWindow,
                                   segs: list[tuple[float, float, int]]) -> None:
        """Tag each utterance with its speaker identity via speaker-ID. Runs in the diar worker
        thread (OFF the ingest path). Embeds each utterance from ``window.raw``; a DIRECT verdict
        for utts >= min_embed_s, and the shorter utts inherit their diar cluster's centroid verdict
        (recovery). Writes ``speaker_name`` (best-matching enrolled name, NULL if no match) +
        ``is_user`` (speaker_name == the user). No-op if speaker-id is off or NO voiceprint is
        enrolled yet (``has_user`` false → verdicts stay NULL; capture is unaffected)."""
        reg = self._speaker_id
        if reg is None or not reg.has_user() or not window.spans:
            return
        sr = self._cfg.model_sample_rate
        raw = window.raw
        embeddings, durations, clusters, row_ids = [], [], [], []
        for row_id, start_s, end_s in window.spans:
            embeddings.append(reg.embed(raw[int(start_s * sr):int(end_s * sr)]))
            durations.append(end_s - start_s)
            clusters.append(self._overlap_cluster(start_s, end_s, segs))
            row_ids.append(row_id)
        verdicts = reg.classify_window(
            embeddings, durations, clusters,
            threshold=self._cfg.user_verify_threshold, min_embed_s=self._cfg.min_embed_s,
        )
        n_user = 0
        for row_id, (speaker_name, is_user, method) in zip(row_ids, verdicts):
            if is_user is None:
                continue
            self._store.set_identity(row_id, speaker_name=speaker_name, is_user=is_user, method=method)
            n_user += int(is_user)
        logger.info("speaker-id w%d: %d/%d utts → user", window.window_idx, n_user, len(row_ids))

    # --- online enrollment (no-teardown) --------------------------------------

    def _collect_enroll(self, samples: np.ndarray, dur: float) -> None:
        """Buffer one utterance for an active enroll session (called from the pipeline worker
        thread). No-op unless a session is active + the utterance is long enough. ONLY buffers —
        the async watcher does finalize/abort, off the capture path. `samples` is already an
        owned copy (the pipeline copies before VAD pop)."""
        sess = self._enroll
        if sess is None or dur < self._cfg.enroll_min_dur_s:
            return
        with sess.lock:
            if sess.done or sess.total_dur >= sess.target_s:
                return  # finalizing (or already enough) — the watcher takes it from here
            sess.samples.append(samples)   # already an owned copy (pipeline copies pre-pop)
            sess.total_dur += dur

    def _read_enroll_request(self) -> dict | None:
        path = self._cfg.enroll_request_path
        if not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                data = json.load(f)
            req = {"id": str(data["id"]), "name": str(data["name"]),
                   "target_s": float(data.get("target_s", self._cfg.enroll_target_s))}
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("bad enroll request file — ignoring", exc_info=True)
            return None
        ts = data.get("ts")  # staleness guard: drop leftovers from a crash/restart
        if ts:
            try:
                age = (datetime.now(UTC) - datetime.fromisoformat(ts)).total_seconds()
            except ValueError:
                age = 0.0
            if age > 600:
                logger.warning("stale enroll request (age %.0fs) — clearing", age)
                self._delete_enroll_request()
                return None
        return req

    def _write_enroll_result(self, result: dict) -> None:
        try:
            tmp = self._cfg.enroll_result_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(result, f)
            os.replace(tmp, self._cfg.enroll_result_path)
        except OSError:
            logger.warning("enroll result write failed", exc_info=True)

    def _delete_enroll_request(self) -> None:
        with contextlib.suppress(OSError):
            os.remove(self._cfg.enroll_request_path)

    async def _finalize_enroll(self, sess: _EnrollSession, *, timed_out: bool = False) -> None:
        """Build + persist the voiceprint OFF the capture path (to_thread), write the result,
        clear the request. Detaches the session first so collection stops."""
        with sess.lock:
            sess.done = True              # stop the worker tap from appending past the snapshot
            samples = list(sess.samples)
        self._enroll = None
        self._enroll_last_id = sess.id
        result = {"id": sess.id, "name": sess.name, "ts": datetime.now(UTC).isoformat()}
        if not samples:
            result.update(status="timeout", clips=0)
            logger.warning("online enroll TIMEOUT name=%r — no speech collected", sess.name)
        else:
            try:
                n = await asyncio.to_thread(self._speaker_id.enroll, sess.name, samples)
                result.update(status="done", clips=n, partial=timed_out,
                              speakers=self._speaker_id.names())
                logger.info("online enroll DONE name=%r clips=%d%s speakers=%s", sess.name, n,
                            " (partial)" if timed_out else "", self._speaker_id.names())
            except Exception as exc:  # noqa: BLE001
                result.update(status="failed", error=str(exc))
                logger.warning("online enroll FAILED name=%r", sess.name, exc_info=True)
        self._write_enroll_result(result)
        self._delete_enroll_request()

    async def _enroll_watcher(self) -> None:
        """Poll for an enroll request + own all session lifecycle (start/finalize/abort).
        Finalize runs via to_thread so the CPU embed never blocks the event loop or capture."""
        while True:
            await asyncio.sleep(self._cfg.enroll_check_interval_s)
            try:
                if self._enroll is None:
                    req = self._read_enroll_request()
                    if req is None or req["id"] == self._enroll_last_id:
                        continue
                    sess = _EnrollSession(id=req["id"], name=req["name"], target_s=req["target_s"])
                    sess.start_time = asyncio.get_running_loop().time()
                    self._enroll = sess
                    logger.info("online enroll START name=%r target=%.0fs — have the speaker talk now",
                                req["name"], req["target_s"])
                    continue
                sess = self._enroll
                with sess.lock:
                    enough = sess.total_dur >= sess.target_s
                elapsed = asyncio.get_running_loop().time() - sess.start_time  # wallclock, not ticks
                if enough:
                    await self._finalize_enroll(sess)
                elif elapsed > self._cfg.enroll_max_wait_s:
                    await self._finalize_enroll(sess, timed_out=True)
            except Exception:  # noqa: BLE001
                logger.warning("enroll watcher tick failed", exc_info=True)

    async def _diar_worker(self) -> None:
        # Liveness flag is set at task-creation in run() (so the startup health snapshot reads
        # accurately); the finally-block below clears it on exit.
        try:
            while True:
                window = await self._diar_queue.get()
                try:
                    segs = await asyncio.to_thread(self._diar.process, window.raw)
                    await asyncio.to_thread(self._assign_labels, window, segs)
                    if self._speaker_id is not None:
                        await asyncio.to_thread(self._verify_speaker_identities, window, segs)
                except Exception:
                    logger.error("diar failed for window w%d (%d utts) — labels stay NULL",
                                 window.window_idx, len(window.spans), exc_info=True)
                finally:
                    self._diar_queue.task_done()
        finally:
            self._diar_worker_alive = False

    # --- connection handling --------------------------------------------------

    @staticmethod
    def _enqueue_drop_oldest(queue: asyncio.Queue, message: bytes) -> bool:
        """Non-blocking enqueue. If the queue is full (the consumer fell behind under audio
        load), drop the OLDEST frame to make room — shedding a little ambient audio is far
        better than blocking the WS read loop (which would stall the socket → the device's
        ping goes unanswered → 10s pong-timeout → 1006 drop → churn). Returns True if a frame
        was dropped. Read-loop only; runs on the single event loop, so no lock is needed."""
        dropped = False
        if queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
                dropped = True
        queue.put_nowait(message)
        return dropped

    async def _consume_frames(self, source: str, pipeline, queue: asyncio.Queue) -> None:
        """Single per-connection consumer: does the heavy, ORDERED per-frame work (mode
        routing + STT/diar/speaker-id or the active cloud relay) OFF the WS read path, so the
        read loop never blocks. Runs until it gets the ``None`` sentinel (sent by _handler on
        close), then finalizes + flushes. A per-frame error is logged but never kills the
        consumer or the socket (one bad frame must not drop capture)."""
        active: ActiveSession | None = None
        try:
            while True:
                message = await queue.get()
                if message is None:  # sentinel: read loop ended → drain done, stop
                    break
                try:
                    # self._mode is read here + written by _handle_mode — both on THIS single
                    # event loop, so no lock is needed. Default 'passive' fails safe to LOCAL.
                    if self._mode == "active":
                        # ACTIVE: relay to a cloud Speechmatics session (opened lazily). Pass the
                        # shared eres2net registry so active mode can relabel S1/S2 → enrolled names
                        # (None when speaker-ID is off/unenrolled → positional).
                        if active is None:
                            active = ActiveSession(self._cfg, source=source, speaker_id=self._speaker_id)
                            await active.start()
                            self._active_session = active  # expose to /marker
                        await active.send_audio(message)
                    else:
                        # PASSIVE (default): local Zipformer pipeline.
                        if active is not None:
                            await active.finalize()
                            if self._active_session is active:
                                self._active_session = None
                            active = None
                        self._utterances_total += await pipeline.feed(message)
                except Exception:  # noqa: BLE001 — one bad frame must not kill capture or the socket
                    logger.exception("frame processing error for %s", source)
        finally:
            # Dirty or clean: finalize any active cloud session, then flush any buffered
            # partial utterance + close the final diar window (flush() submits it).
            if active is not None:
                with contextlib.suppress(Exception):
                    await active.finalize()
                if self._active_session is active:
                    self._active_session = None
            try:
                self._utterances_total += await pipeline.flush()
            except Exception:  # noqa: BLE001
                logger.warning("flush failed for %s", source, exc_info=True)

    async def _handler(self, websocket) -> None:
        source = f"ambient-{getattr(websocket, 'remote_address', ('?',))[0]}"
        # Reap a silently-dead (half-open) peer in minutes: WS pings are off (the device
        # rejects them), so without tuned TCP keep-alive a dead socket would sit ESTAB ~2h.
        # get_extra_info("socket") may be None (non-TCP transport) → helper no-ops on None.
        transport = getattr(websocket, "transport", None)
        sock = transport.get_extra_info("socket") if transport is not None else None
        _enable_tcp_keepalive(sock, self._cfg)
        pipeline = self._engine.new_pipeline(source)
        task = asyncio.current_task()
        self._handler_tasks.add(task)
        self._active += 1
        self._last_connection_ts = datetime.now(UTC).isoformat()
        if self._active == 1:  # device came online (0→1); ignores zombie-overlap re-counts
            self._conn_stats.on_connect()
        logger.info("Client connected: %s (active=%d)", source, self._active)
        # Decouple WS I/O from processing: the read loop ONLY enqueues frames (instant), so the
        # socket is drained as fast as the device sends → pings stay answered → no pong-timeout
        # churn. A single consumer task does the heavy per-frame work in order. See _consume_frames.
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._cfg.frame_queue_max)
        consumer = tracked_task(self._consume_frames(source, pipeline, queue),
                                name=f"ambient-consume-{source}")
        try:
            async for message in websocket:
                if isinstance(message, bytes):
                    if self._enqueue_drop_oldest(queue, message):
                        self._frames_dropped += 1
                elif isinstance(message, str):
                    self._on_control(source, message)
        except websockets.ConnectionClosed as exc:
            logger.info("Client %s closed (code=%s reason=%r)", source, exc.code, exc.reason)
        except Exception:  # noqa: BLE001
            logger.exception("Handler error for %s", source)
        finally:
            # Signal the consumer (None sentinel) to drain remaining frames then finalize+flush,
            # then join it. The sentinel put MUST NOT block — at close the queue may be full of
            # unconsumed frames — so drop one to make room if needed. Awaiting the consumer is then
            # bounded: it gets the sentinel and stops, or has already finished/crashed and returns
            # immediately (tracked_task logged any crash; suppress() guards the join).
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                queue.put_nowait(None)
            with contextlib.suppress(Exception):
                await consumer
            self._handler_tasks.discard(task)
            self._active -= 1
            if self._active == 0:  # device fully gone (1→0); starts a dark-period timer
                self._conn_stats.on_disconnect()
            logger.info("Client gone: %s (active=%d, utterances=%d)",
                        source, self._active, pipeline.utterances)

    def _on_control(self, source: str, message: str) -> None:
        try:
            data = json.loads(message)
        except ValueError:
            logger.warning("[%s] non-JSON text frame ignored: %.80s", source, message)
            return
        logger.info("[%s] control: %s", source, data)
        # Stage-1 has no playback to interrupt; we just log interrupt/disconnect.

    async def _handle_mode(self, request: web.Request) -> web.Response:
        """HTTP control: POST /mode {"mode":"active"|"passive"}. Bridge-level (one device →
        one connection). Default stays passive so a dropped/late POST fails safe to LOCAL."""
        try:
            data = await request.json()
            mode = data.get("mode")
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        if mode not in ("active", "passive"):
            return web.json_response({"error": "mode must be 'active' or 'passive'"}, status=400)
        prev, self._mode = self._mode, mode
        if mode != prev:
            logger.warning("LISTENING MODE -> %s (was %s)", mode, prev)
        return web.json_response({"mode": self._mode})

    async def _handle_marker(self, request: web.Request) -> web.Response:
        """HTTP control: POST /marker — drop a timestamped bookmark into the live ACTIVE
        transcript (the device's single-press while Active Listening is on). Graceful no-op
        (200, marked=false) when no cloud session is open, so a stray press in passive mode —
        or a race against session open/close — is harmless. Same event loop as the audio relay
        + Speechmatics callbacks, so reading/mutating the session needs no lock."""
        session = self._active_session
        if session is None:
            return web.json_response({"marked": False})
        session.add_marker()
        return web.json_response({"marked": True})

    # --- health + purge -------------------------------------------------------

    def _write_health(self) -> None:
        try:
            self._conn_stats.tick()  # count an ongoing dark period once it crosses the threshold
            stats = self._store.stats()
            payload = {
                "ts": datetime.now(UTC).isoformat(),
                "alive": True,
                "mode": self._mode,
                "active_connections": self._active,
                "last_connection_ts": self._last_connection_ts,
                "utterances_total": self._utterances_total,
                "diar_enabled": self._diar is not None,
                "diar_worker_alive": self._diar_worker_alive,
                "diar_queue_depth": self._diar_queue.qsize() if self._diar_queue else 0,
                "diar_windows_dropped": self._diar_dropped,
                "frames_dropped": self._frames_dropped,
                "speaker_id_enabled": self._speaker_id is not None,
                "enrolling": self._enroll.name if self._enroll else None,
                **self._conn_stats.snapshot(),
                **stats,
            }
            tmp = self._cfg.health_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, self._cfg.health_path)
        except Exception:  # noqa: BLE001
            logger.warning("health write failed", exc_info=True)

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(self._cfg.health_interval_s)
            self._write_health()

    async def _purge_loop(self) -> None:
        while True:
            await asyncio.sleep(self._cfg.purge_interval_s)
            try:
                ttl_d, ceil_d = await asyncio.to_thread(
                    self._store.purge, self._cfg.ttl_hours, self._cfg.row_ceiling,
                )
                if ttl_d or ceil_d:
                    logger.info("purge: ttl=%d ceiling=%d", ttl_d, ceil_d)
            except Exception:  # noqa: BLE001
                logger.warning("purge failed", exc_info=True)

    # --- lifecycle ------------------------------------------------------------

    async def run(self) -> None:
        # Purge stale rows immediately on startup.
        try:
            await asyncio.to_thread(self._store.purge, self._cfg.ttl_hours, self._cfg.row_ceiling)
        except Exception:  # noqa: BLE001
            logger.warning("startup purge failed", exc_info=True)
        tasks = [
            tracked_task(self._health_loop(), name="ambient-health"),
            tracked_task(self._purge_loop(), name="ambient-purge"),
        ]
        if self._diar is not None:
            # Create the queue inside the running loop, before any connection can submit.
            self._diar_queue = asyncio.Queue(maxsize=self._cfg.diar_queue_max)
            # Mark alive at task creation so the startup health snapshot isn't spuriously
            # False (the worker's finally-block flips it back off on exit).
            self._diar_worker_alive = True
            tasks.append(tracked_task(self._diar_worker(), name="ambient-diar"))
        if self._speaker_id is not None:
            # Online (no-teardown) enrollment watcher — owns enroll session lifecycle.
            tasks.append(tracked_task(self._enroll_watcher(), name="ambient-enroll"))
        # First health write AFTER tasks are wired, so the snapshot reflects real state.
        self._write_health()
        logger.info("Ambient bridge listening on ws://%s:%d/ (input_sr=%d → 16k, diar=%s)",
                    self._cfg.host, self._cfg.port, self._cfg.input_sample_rate, self._diar is not None)
        # Graceful stop on SIGINT (Ctrl-C) AND SIGTERM (systemd stop/restart).
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        # HTTP control endpoint (same event loop): the device POSTs the active/passive mode
        # here on its Active-Listening toggle. One device -> one connection -> a bridge-level flag.
        # Trust boundary: LAN-internal + unauthenticated, like the ambient/s2s WS endpoints on this
        # box (threat model = public exposure, NOT the LAN). Add a shared token if that changes.
        control_app = web.Application()
        control_app.add_routes([
            web.post("/mode", self._handle_mode),
            web.post("/marker", self._handle_marker),
        ])
        control_runner = web.AppRunner(control_app)
        await control_runner.setup()
        await web.TCPSite(control_runner, self._cfg.host, self._cfg.control_http_port).start()
        logger.info("Listening-mode control endpoint on http://%s:%d/mode",
                    self._cfg.host, self._cfg.control_http_port)
        try:
            # ping_interval=None: the Voice PE's minimal WS stack rejects server PING control
            # frames (-> 1002 invalid-opcode close ~every 60s, with no client-side reconnect),
            # which silently kills ambient capture. Audio is a continuous stream, so we rely on
            # app-level liveness instead. A silently-dead (half-open) socket is reaped via the
            # per-connection TCP keep-alive set in _handler (_enable_tcp_keepalive: minutes, NOT
            # the ~2h OS default) so active_connections self-corrects; the health MONITOR still gates
            # on `last_ts` (audio-gap) freshness as the primary signal.
            async with websockets.serve(
                self._handler, self._cfg.host, self._cfg.port, max_size=None, ping_interval=None,
            ):
                await stop.wait()
        finally:
            with contextlib.suppress(Exception):
                await control_runner.cleanup()
            # Let in-flight handlers finish their flush() (which may submit a final
            # window) before tearing down the store + background tasks.
            if self._handler_tasks:
                await asyncio.wait(self._handler_tasks, timeout=10.0)
            # Drain queued diar windows so their labels land (the worker is still
            # running here). Bounded by ~queue_max windows of diar time; the timeout
            # keeps shutdown well under systemd's default stop window.
            if self._diar_queue is not None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._diar_queue.join(), timeout=30.0)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._store.close()
            logger.info("Ambient bridge stopped; store closed.")


def main() -> None:
    cfg = load_config()
    asyncio.run(AmbientServer(cfg).run())


if __name__ == "__main__":
    main()
