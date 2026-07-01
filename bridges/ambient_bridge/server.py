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
import multiprocessing as mp
import os
import signal
import socket
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np
import websockets
from aiohttp import web

from . import diar_subprocess
from .active_session import ActiveSession
from .config import AmbientConfig, load_config
from .connection_stats import ConnectionStats
from .esphome_recovery import RecoveryState, reboot_device
from .pipeline import AmbientEngine, DiarWindow, _autodetect_embedding
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


def _rss_mb(pid: int) -> float | None:
    """Resident set size (MB) of a process via ``/proc/<pid>/statm`` — Linux, no psutil dep.
    Field 2 is the resident page count; × the page size → bytes. Returns None if the pid is
    gone or /proc is unreadable (best-effort: the health writer must never fail on this)."""
    try:
        with open(f"/proc/{pid}/statm") as f:
            resident_pages = int(f.read().split()[1])
        return round(resident_pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024), 1)
    except (OSError, ValueError, IndexError):
        return None


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
        self._last_connection_ts: str | None = None  # last client connect (for the health monitor)
        self._max_loop_lag_s = 0.0  # diagnostic: peak event-loop scheduling lag (AMBIENT_INSTRUMENT)
        # Connection telemetry — records device connect/disconnect + dark gaps (the wedge signal),
        # persists across bridge restarts, surfaces in ambient_health.json.
        self._conn_stats = ConnectionStats(
            events_path=cfg.conn_events_path, stats_path=cfg.conn_stats_path,
            dark_threshold_s=cfg.conn_dark_threshold_s, events_max=cfg.conn_events_max)
        # --- device auto-recovery (reboot a wedged Voice PE via the ESPHome API; default OFF) ---
        # Arms only when enabled AND a device IP + a readable PSK key file are present. Keyed off a
        # PERSISTED last-seen ts (restart-safe). See esphome_recovery.py.
        self._recovery: RecoveryState | None = None
        self._recovery_psk = ""
        self._reboot_inflight = False
        if cfg.recovery_enabled and cfg.recovery_device_ip:
            try:
                with open(cfg.recovery_psk_path) as f:
                    self._recovery_psk = f.read().strip()
            except OSError:
                logger.error("recovery enabled but PSK key unreadable at %s — recovery DISABLED",
                             cfg.recovery_psk_path)
            if self._recovery_psk:
                self._recovery = RecoveryState(
                    path=cfg.recovery_state_path, cooldown_s=cfg.recovery_reboot_cooldown_s,
                    max_per_window=cfg.recovery_max_reboots_per_window,
                    window_s=cfg.recovery_reboot_window_s)
                try:
                    import aioesphomeapi  # noqa: F401 — fail loud NOW, not at the first reboot
                except Exception:  # noqa: BLE001
                    logger.error("recovery ARMED but aioesphomeapi is NOT installed — reboots will "
                                 "no-op; run: pip install aioesphomeapi")
                logger.warning(
                    "device auto-recovery ARMED: reboot %s:%d (%r) after %.0fs dark "
                    "(cooldown %.0fs, cap %d/%.0fs)", cfg.recovery_device_ip, cfg.recovery_device_port,
                    cfg.recovery_button_name, cfg.recovery_no_conn_threshold_s,
                    cfg.recovery_reboot_cooldown_s, cfg.recovery_max_reboots_per_window,
                    cfg.recovery_reboot_window_s)
        elif cfg.recovery_enabled:
            logger.error("recovery enabled but AMBIENT_RECOVERY_DEVICE_IP unset — recovery DISABLED")
        # --- diarization (deferred, additive; capture works fine without it) ---
        # The diarization ONNX runs in a SEPARATE PROCESS (see diar_subprocess): sherpa's process()
        # holds the GIL for tens of seconds on a busy window, which would freeze the event loop and
        # trip the device's WS pong-timeout. The pool is created in run() (inside the loop); here we
        # only record intent + wire the window producer. The PARENT never loads the diar models.
        self._diar_enabled = False
        self._diar_pool: ProcessPoolExecutor | None = None
        self._diar_queue: asyncio.Queue[DiarWindow] | None = None
        self._window_counter = 0
        self._diar_dropped = 0
        self._diar_pool_failures = 0  # consecutive subprocess crashes (cap → disable diar, no loop)
        self._diar_worker_alive = False
        if cfg.diar_enabled:
            self._engine.enable_diarization(
                self.submit_window, int(cfg.diar_window_s * cfg.model_sample_rate),
            )
            self._diar_enabled = True
            if cfg.diar_num_threads + cfg.num_threads > (os.cpu_count() or 4):
                logger.warning("diar_num_threads(%d) + num_threads(%d) > cpu_count(%s) — diar "
                               "(separate process) may contend with STT for cores under load",
                               cfg.diar_num_threads, cfg.num_threads, os.cpu_count())
        # --- speaker identification (Stage-A, additive) — the PARENT keeps the voiceprint registry
        # (cheap classify here + active-mode + online-enroll); the heavy per-utterance EMBEDDING runs
        # in the diar subprocess. No registry / no enrolled voiceprint → is_user stays NULL. ---
        self._speaker_id: SpeakerIDRegistry | None = None
        if cfg.speaker_id_enabled and self._diar_enabled:
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

    def _finish_window(self, window: DiarWindow, segs: list[tuple[float, float, int]],
                       embeddings: list | None) -> None:
        """Parent-side post-processing for a diarized window (runs in a worker thread; cheap — NO
        ONNX). Writes the diar ``speaker_label`` (``_assign_labels``), then — if speaker-ID is on and
        the child computed embeddings — classifies them against the enrolled voiceprints
        (``classify_window`` is pure cosine, no model) and writes ``speaker_name``/``is_user``. ALL
        store writes happen here in the parent (single writer); the GIL-heavy diarization + embedding
        already ran in the subprocess. No-op classify if speaker-id off / no voiceprint / no embeddings."""
        self._assign_labels(window, segs)
        reg = self._speaker_id
        if reg is None or not reg.has_user() or embeddings is None or not window.spans:
            return
        durations = [end_s - start_s for (_rid, start_s, end_s) in window.spans]
        clusters = [self._overlap_cluster(start_s, end_s, segs) for (_rid, start_s, end_s) in window.spans]
        verdicts = reg.classify_window(
            embeddings, durations, clusters,
            threshold=self._cfg.user_verify_threshold, min_embed_s=self._cfg.min_embed_s,
        )
        n_user = 0
        for (row_id, _start, _end), (speaker_name, is_user, method) in zip(window.spans, verdicts):
            if is_user is None:
                continue
            self._store.set_identity(row_id, speaker_name=speaker_name, is_user=is_user, method=method)
            n_user += int(is_user)
        logger.info("speaker-id w%d: %d/%d utts → user", window.window_idx, n_user, len(window.spans))

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
        # accurately); the finally-block below clears it on exit. The HEAVY work (diar.process +
        # per-utterance embedding) runs in a SEPARATE PROCESS so it can't freeze the event loop;
        # the cheap label/classify/store writes run here in the parent.
        loop = asyncio.get_running_loop()
        try:
            while True:
                window = await self._diar_queue.get()
                try:
                    if self._diar_pool is None:
                        continue  # pool unavailable (init/recreate failed) — labels stay NULL
                    do_spk = self._speaker_id is not None and self._speaker_id.has_user()
                    t0 = loop.time()
                    segs, embeddings = await loop.run_in_executor(
                        self._diar_pool, diar_subprocess.process_window,
                        window.raw, window.spans, self._cfg.model_sample_rate, do_spk,
                    )
                    t1 = loop.time()
                    await asyncio.to_thread(self._finish_window, window, segs, embeddings)
                    t2 = loop.time()
                    self._diar_pool_failures = 0  # a clean window resets the crash counter
                    if self._cfg.instrument:
                        # child = the GIL-heavy work, now OFF the loop's process; parent = cheap
                        # writes. POST-FIX the loop-lag monitor should stay ~0 even while child is big.
                        n_spk = len({spk for _, _, spk in segs}) if segs else 0
                        logger.warning(
                            "INSTRUMENT diar w%d: child(process+embed)=%.2fs parent(label+store)=%.2fs "
                            "(%d spk, %d utts, %.1fs audio)",
                            window.window_idx, t1 - t0, t2 - t1, n_spk, len(window.spans),
                            len(window.raw) / self._cfg.model_sample_rate)
                except BrokenProcessPool as exc:
                    self._diar_pool_failures += 1
                    if self._diar_pool_failures > 3:
                        # Persistent child-init failure (e.g. a missing model) — stop the
                        # recreate-crash loop and degrade to capture-only.
                        logger.error("diar subprocess failed %d× consecutively — disabling diar "
                                     "(capture-only); last: %r", self._diar_pool_failures, exc)
                        old, self._diar_pool, self._diar_enabled = self._diar_pool, None, False
                        if old is not None:
                            with contextlib.suppress(Exception):
                                old.shutdown(wait=False, cancel_futures=True)
                    else:
                        logger.error("diar subprocess died for w%d (%r) — recreating pool (#%d); "
                                     "labels NULL", window.window_idx, exc, self._diar_pool_failures)
                        self._recreate_diar_pool()
                except Exception:
                    logger.error("diar failed for window w%d (%d utts) — labels stay NULL",
                                 window.window_idx, len(window.spans), exc_info=True)
                finally:
                    self._diar_queue.task_done()
        finally:
            self._diar_worker_alive = False

    def _make_diar_pool(self) -> ProcessPoolExecutor:
        """A ProcessPoolExecutor (spawn) whose single worker loads the diarization engine + an
        embedder (diar_subprocess.init_worker). spawn — NOT fork — avoids onnxruntime/sherpa
        deadlocks from forking a process that already loaded native threads; the child reads config
        from the inherited environment."""
        return ProcessPoolExecutor(
            max_workers=1, mp_context=mp.get_context("spawn"),
            initializer=diar_subprocess.init_worker,
        )

    def _recreate_diar_pool(self) -> None:
        """Replace a broken diar pool (child crashed/OOM) so the next window can be processed.
        Best-effort: on failure diar goes off (labels stay NULL) but capture is unaffected."""
        old, self._diar_pool = self._diar_pool, None
        if old is not None:
            with contextlib.suppress(Exception):
                old.shutdown(wait=False, cancel_futures=True)
        try:
            self._diar_pool = self._make_diar_pool()
        except Exception:
            logger.error("diar pool re-init failed — capture continues, labels NULL", exc_info=True)

    async def _loop_lag_monitor(self) -> None:
        """Diagnostic (AMBIENT_INSTRUMENT): sample event-loop scheduling lag every 100ms. A spike
        means a coroutine or a worker-thread C call (holding the GIL) monopolised the loop — which
        delays the websockets auto-PONG and trips the device's 10s pong-timeout. Each spike >
        instrument_lag_warn_s is logged with a wall-clock stamp so it can be correlated against the
        diar-worker phase logs and the 1006 closes. Started only when instrument is on."""
        loop = asyncio.get_running_loop()
        interval = 0.1
        while True:
            t0 = loop.time()
            await asyncio.sleep(interval)
            lag = loop.time() - t0 - interval
            if lag > self._max_loop_lag_s:
                self._max_loop_lag_s = lag
            if lag > self._cfg.instrument_lag_warn_s:
                logger.warning("INSTRUMENT loop-lag %.2fs (loop stalled — WS PONG delayed)", lag)

    # --- connection handling --------------------------------------------------

    async def _handler(self, websocket) -> None:
        source = f"ambient-{getattr(websocket, 'remote_address', ('?',))[0]}"
        # Reap a silently-dead (half-open) peer in minutes: WS pings are off (the device
        # rejects them), so without tuned TCP keep-alive a dead socket would sit ESTAB ~2h.
        # get_extra_info("socket") may be None (non-TCP transport) → helper no-ops on None.
        transport = getattr(websocket, "transport", None)
        sock = transport.get_extra_info("socket") if transport is not None else None
        _enable_tcp_keepalive(sock, self._cfg)
        pipeline = self._engine.new_pipeline(source)
        active: ActiveSession | None = None  # opened lazily while mode=active
        task = asyncio.current_task()
        self._handler_tasks.add(task)
        self._active += 1
        self._last_connection_ts = datetime.now(UTC).isoformat()
        if self._active == 1:  # device came online (0→1); ignores zombie-overlap re-counts
            self._conn_stats.on_connect()
        logger.info("Client connected: %s (active=%d)", source, self._active)
        try:
            async for message in websocket:
                if isinstance(message, bytes):
                    # self._mode is read here + written by _handle_mode — both on THIS single
                    # event loop, so no lock is needed. Default 'passive' fails safe to LOCAL.
                    if self._mode == "active":
                        # ACTIVE: relay to a cloud Speechmatics session (opened lazily).
                        if active is None:
                            # Pass the shared eres2net registry so active mode can relabel S1/S2 →
                            # enrolled names (None when speaker-ID is off/unenrolled → positional).
                            active = ActiveSession(self._cfg, source=source, speaker_id=self._speaker_id)
                            await active.start()
                            self._active_session = active  # expose to /marker
                        await active.send_audio(message)
                    else:
                        # PASSIVE (default): local Zipformer pipeline — UNCHANGED path.
                        if active is not None:
                            await active.finalize()
                            if self._active_session is active:
                                self._active_session = None
                            active = None
                        self._utterances_total += await pipeline.feed(message)
                elif isinstance(message, str):
                    self._on_control(source, message)
        except websockets.ConnectionClosed as exc:
            logger.info("Client %s closed (code=%s reason=%r)", source, exc.code, exc.reason)
        except Exception:  # noqa: BLE001
            logger.exception("Handler error for %s", source)
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

    def _memory_snapshot(self) -> dict:
        """Parent + diar-child RSS (MB) for the health JSON, so the ``MALLOC_ARENA_MAX=2`` leak
        fix stays watchable in production. Post subprocess-isolation the diar work runs in a
        separate spawn CHILD, so its footprint is invisible to the parent's own RSS — read both.
        The child pid(s) come from the executor's private ``_processes`` map (a ``{pid: Process}``
        dict) which — unlike a /proc ppid scan — naturally excludes the spawn resource_tracker
        sibling. Best-effort: any failure leaves a key None and never breaks the health write."""
        rss_parent = _rss_mb(os.getpid())
        rss_child: float | None = None
        pool = self._diar_pool
        if pool is not None:
            try:
                # The executor's manager thread can mutate _processes; snapshot keys under guard.
                pids = list(getattr(pool, "_processes", {}) or {})
                vals = [v for v in (_rss_mb(p) for p in pids) if v is not None]
                rss_child = round(sum(vals), 1) if vals else None
            except Exception:  # noqa: BLE001
                rss_child = None
        total = (
            round((rss_parent or 0) + (rss_child or 0), 1)
            if (rss_parent is not None or rss_child is not None)
            else None
        )
        return {
            "rss_parent_mb": rss_parent,
            "rss_diar_child_mb": rss_child,
            "rss_total_mb": total,
        }

    def _write_health(self) -> None:
        try:
            self._conn_stats.tick()  # count an ongoing dark period once it crosses the threshold
            if self._cfg.instrument:
                # store.stats() runs a SQLite query ON the event loop — time it to rule the on-loop
                # DB path in/out as a pong-stall contributor (vs the diar worker).
                _t = time.monotonic()
                stats = self._store.stats()
                _dt = time.monotonic() - _t
                if _dt > 0.2:
                    logger.warning("INSTRUMENT store.stats() blocked the loop %.2fs", _dt)
            else:
                stats = self._store.stats()
            payload = {
                "ts": datetime.now(UTC).isoformat(),
                "alive": True,
                "mode": self._mode,
                "active_connections": self._active,
                "last_connection_ts": self._last_connection_ts,
                "utterances_total": self._utterances_total,
                "diar_enabled": self._diar_enabled,
                "diar_worker_alive": self._diar_worker_alive,
                "diar_queue_depth": self._diar_queue.qsize() if self._diar_queue else 0,
                "diar_windows_dropped": self._diar_dropped,
                "speaker_id_enabled": self._speaker_id is not None,
                "enrolling": self._enroll.name if self._enroll else None,
                # Parent + diar-child RSS so the MALLOC_ARENA_MAX=2 leak fix stays watchable live.
                **self._memory_snapshot(),
                # Surfaced only under instrumentation, so the default health JSON is unchanged.
                **({"max_loop_lag_s": round(self._max_loop_lag_s, 3)} if self._cfg.instrument else {}),
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
            self._maybe_recover()

    def _maybe_recover(self) -> None:
        """Device auto-recovery watchdog (runs at the health cadence). While the device is connected,
        refresh the persisted last-seen ts; once it's been GONE past the threshold (and was recently
        present), schedule a one-shot reboot. Restart-safe: the wedge signal is the persisted last-seen,
        NOT conn_stats' in-process dark timer (which is None after a restart)."""
        rec = self._recovery
        if rec is None:
            return
        if self._active > 0:
            rec.mark_seen()
            return
        if self._reboot_inflight:
            return
        if rec.should_reboot(active=self._active,
                             dark_threshold_s=self._cfg.recovery_no_conn_threshold_s,
                             seen_window_s=self._cfg.recovery_seen_window_s):
            self._reboot_inflight = True
            tracked_task(self._do_device_reboot(), name="ambient-recovery-reboot")

    async def _do_device_reboot(self) -> None:
        """Press the device's Restart button. Records the ATTEMPT (so cooldown/cap count failures too),
        logs the outcome, and WARNs loudly if the rolling-window cap is now reached (a human should look).
        Best-effort — reboot_device never raises; the inflight flag is always cleared."""
        rec = self._recovery  # always set: _maybe_recover only schedules this when armed
        cfg = self._cfg
        try:
            dark = rec.dark_for()
            rec.record_reboot()
            logger.warning("RECOVERY: device dark %.0fs (> %.0fs) — rebooting %s",
                           dark or 0.0, cfg.recovery_no_conn_threshold_s, cfg.recovery_device_ip)
            ok = await reboot_device(
                cfg.recovery_device_ip, cfg.recovery_device_port, self._recovery_psk,
                button_name=cfg.recovery_button_name, timeout_s=cfg.recovery_reboot_timeout_s)
            if ok:
                logger.warning("RECOVERY: reboot press sent to %s — awaiting reconnect",
                               cfg.recovery_device_ip)
            else:
                logger.error("RECOVERY: reboot FAILED for %s (see prior log)", cfg.recovery_device_ip)
            if rec.at_cap():
                logger.error("RECOVERY: reboot cap reached (%d in %.0fs) — HALTING auto-reboot until the "
                             "window rolls; investigate the device", cfg.recovery_max_reboots_per_window,
                             cfg.recovery_reboot_window_s)
        finally:
            self._reboot_inflight = False

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
        if self._cfg.instrument:
            tasks.append(tracked_task(self._loop_lag_monitor(), name="ambient-loop-lag"))
            logger.warning("INSTRUMENT mode ON — event-loop-lag monitor + diar-phase timing active")
        if self._diar_enabled:
            # Spawn the diar subprocess pool inside the loop (its first task incurs spawn + model
            # load). If it can't be created, degrade to capture-only rather than crash.
            try:
                self._diar_pool = self._make_diar_pool()
            except Exception:
                logger.warning("diar pool init failed — running capture-only (labels NULL)", exc_info=True)
                self._diar_enabled = False
        if self._diar_enabled:
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
                    self._cfg.host, self._cfg.port, self._cfg.input_sample_rate, self._diar_enabled)
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
            # Drain queued diar windows so their labels land (the worker is still running here).
            # Bounded by the timeout — an in-flight SUBPROCESS window that outlasts it is abandoned
            # (its labels stay NULL; the child finishes independently and exits). Keeps shutdown well
            # under systemd's default stop window.
            if self._diar_queue is not None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._diar_queue.join(), timeout=30.0)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._diar_pool is not None:
                self._diar_pool.shutdown(wait=False, cancel_futures=True)
            self._store.close()
            logger.info("Ambient bridge stopped; store closed.")


def main() -> None:
    cfg = load_config()
    asyncio.run(AmbientServer(cfg).run())


if __name__ == "__main__":
    main()
