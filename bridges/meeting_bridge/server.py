"""Meeting bridge server — aiohttp app: authenticated audio WebSocket + the phone capture page.

One app, one port. ``GET /capture/<token>`` serves the phone PWA; ``GET /meeting/<token>`` is the
audio WebSocket (raw 16-bit PCM binary frames + JSON control frames). Both gate on a constant-time
path-token compare (the browser can't set custom WS headers, so the token rides the URL). A WS
connection drives a VAD-gated session lifecycle (dependency-injected — default = Speechmatics via
``ActiveSession``): a cloud session opens on speech and finalizes after a sustained silence, so one
connection can span several sessions — one transcript per meeting. ``MEETING_VAD_THRESHOLD=0``
(default) disables gating → a single session spans the whole connection (the legacy behavior).

Runs behind the Tailscale Funnel (the one authenticated public door); binds loopback by default.
No ``genesis.*`` imports. The cloud SDK is imported lazily by the default factory, so the server
imports (and unit-tests) without speechmatics-rt installed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hmac
import json
import logging
import os
import signal
import time
from pathlib import Path

from aiohttp import WSMsgType, web

from .config import MeetingConfig, load_config
from .session import default_session_factory
from .vad import GateStats, SessionGate, peak_amplitude

logger = logging.getLogger(__name__)

_CAPTURE_HTML_PATH = Path(__file__).with_name("capture.html")
_TOKEN_PLACEHOLDER = "__MEETING_TOKEN__"  # replaced per-request with the validated path token
# Speechmatics operating points the client may pick per session via ?model=. Anything else
# (absent, typo, hostile) falls back to the configured default — the model is never trusted raw.
_ALLOWED_MODELS = ("standard", "enhanced")


def tracked_task(coro, *, name: str) -> asyncio.Task:
    """Like asyncio.create_task but logs exceptions at ERROR (no silent failures)."""
    t = asyncio.create_task(coro, name=name)

    def _done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("task %s crashed", name, exc_info=exc)

    t.add_done_callback(_done)
    return t


class MeetingServer:
    def __init__(self, cfg: MeetingConfig, *, session_factory=default_session_factory):
        self._cfg = cfg
        self._session_factory = session_factory
        self._closed = False
        # counters (surfaced in the health JSON)
        self._active = 0  # concurrent WS connections
        self._sessions_total = 0  # cloud (Speechmatics) sessions opened — now one per meeting
        self._frames = 0  # frames forwarded to a cloud session
        self._frames_gated = 0  # frames dropped as silence / not billed (incl. noise while dormant)
        self._sessions_idle_closed = 0  # sessions finalized on transcript-idle (post-meeting noise tail)
        self._bytes = 0
        self._last_frame_ts: float | None = None
        self._capture_html: str | None = None

    # ── app wiring ─────────────────────────────────────────────────────────
    def build_app(self) -> web.Application:
        app = web.Application()
        app.add_routes(
            [
                web.get("/capture/{token}", self._handle_capture),
                web.get("/meeting/{token}", self._handle_ws),
                web.get("/health", self._handle_health),
                web.get("/health/{token}", self._handle_health_full),
            ]
        )
        return app

    # ── auth ───────────────────────────────────────────────────────────────
    def _authenticate(self, token: str) -> bool:
        candidates = self._cfg.token_candidates()
        if not candidates:
            return False  # no token configured -> nothing authenticates (fail closed)
        tok = token.encode("utf-8", "ignore")
        return any(hmac.compare_digest(tok, c.encode("utf-8")) for c in candidates)

    # ── per-connection config ────────────────────────────────────────────────
    def _cfg_for_request(self, request: web.Request) -> MeetingConfig:
        """Config for this connection, honoring a validated ``?model=`` override.

        The client picks the Speechmatics operating point per session; an absent/unknown value
        keeps the configured default (never trust the query value raw). Everything else is unchanged.
        """
        model = request.query.get("model", "").strip().lower()
        if model in _ALLOWED_MODELS and model != self._cfg.model:
            return dataclasses.replace(self._cfg, model=model)
        return self._cfg

    # ── capture page ─────────────────────────────────────────────────────────
    def _load_capture_html(self) -> str:
        if self._capture_html is None:
            self._capture_html = _CAPTURE_HTML_PATH.read_text(encoding="utf-8")
        return self._capture_html

    async def _handle_capture(self, request: web.Request) -> web.Response:
        token = request.match_info.get("token", "")
        if not self._authenticate(token):
            return web.Response(status=403, text="forbidden")
        # Inject the validated token so the page's JS opens a same-origin wss to /meeting/<token>.
        html = self._load_capture_html().replace(_TOKEN_PLACEHOLDER, token)
        return web.Response(status=200, text=html, content_type="text/html")

    # ── audio websocket ──────────────────────────────────────────────────────
    async def _handle_ws(self, request: web.Request) -> web.StreamResponse:
        token = request.match_info.get("token", "")
        if not self._authenticate(token):
            # Reject BEFORE the WS upgrade so the client sees a clean 403 handshake failure.
            return web.Response(status=403, text="forbidden")
        # max_msg_size enforces the per-frame cap (an oversize frame trips a WS ERROR + close
        # instead of being relayed). heartbeat pings the phone and force-closes on a missed pong,
        # so a silently-vanished peer is finalized in seconds instead of leaking an open, billed
        # cloud session with a stuck active-count.
        hb = self._cfg.ws_heartbeat_s if self._cfg.ws_heartbeat_s > 0 else None
        ws = web.WebSocketResponse(max_msg_size=self._cfg.max_frame_bytes, heartbeat=hb)
        await ws.prepare(request)
        source = f"meeting-{request.remote or '?'}-{int(time.time())}"
        # The client may pick the Speechmatics model per session via ?model=standard|enhanced
        # (a validated whitelist; anything else keeps the configured default). The model is fixed
        # for the life of a Speechmatics session, so "switching" = stop + restart with the other one.
        cfg = self._cfg_for_request(request)
        # VAD-driven session lifecycle: the phone streams continuously, so we open a cloud session
        # on speech and finalize it after `silence_close_s` of silence — Speechmatics only bills
        # while someone is talking and each meeting lands in its own transcript. threshold=0 disables
        # the gate (one session spans the whole connection — the legacy behavior). The silence-close
        # is evaluated inline per frame, which is sound precisely because the client sends frames
        # continuously; a future client-side VAD (which would go silent on the wire) would instead
        # need a timer to close an idle session.
        gate = SessionGate(
            threshold=cfg.vad_threshold, hangover_s=cfg.vad_hangover_s, silence_close_s=cfg.silence_close_s
        )
        stats = GateStats(self._cfg.vad_log_interval_s)
        session = None
        opened = 0  # cloud sessions opened on THIS connection (for the close log)
        # Transcript-idle lifecycle (only meaningful when the gate is armed): `dormant` suppresses
        # reopening a billed session on mere room noise after the ASR's speech evidence goes quiet,
        # until the room actually falls silent for silence_close_s / a marker / a reconnect.
        # last_evidence_ts tracks ASR speech evidence — the session's `last_activity` (non-empty
        # partials + committed finals) with the committed-turn count (last_turns) as a fallback for
        # backends without it; last_loud_ts is a local silence timer driving the dormant→armed re-arm
        # (needed because after gate.reset() the gate can't re-emit `close` without a fresh speech
        # run, so an immediately-quiet room would never re-arm off it).
        dormant = False
        last_turns = 0
        last_evidence_ts = 0.0
        last_loud_ts = time.monotonic()
        self._active += 1
        logger.info(
            "meeting connection opening: %s model=%s vad_threshold=%d (active=%d)",
            source,
            cfg.model,
            cfg.vad_threshold,
            self._active,
        )
        try:
            # The session is opened lazily (on first speech) INSIDE the loop, still within this try,
            # so a factory/start failure is CONTAINED (logged, not propagated to aiohttp's error
            # logger) and still runs the finally cleanup below. A connection that never sends speech
            # never opens a billed cloud session.
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    now = time.monotonic()
                    peak = peak_amplitude(msg.data)
                    forward, close = gate.observe(peak, now)
                    # Track strict above-threshold loudness (independent of the gate's hangover) so a
                    # dormant connection can measure a genuinely quiet room and re-arm.
                    if gate.enabled and peak >= cfg.vad_threshold:
                        last_loud_ts = now
                    # Re-arm: a transcript-idle close parked this connection dormant; a real quiet gap
                    # (or a marker / reconnect) ends dormancy so a genuine next meeting opens fresh,
                    # while continuing room noise does not.
                    if dormant and gate.enabled and now - last_loud_ts >= cfg.silence_close_s:
                        dormant = False
                        logger.info("meeting re-armed after %.0fs silence: %s", now - last_loud_ts, source)
                    # Transcript-idle close: stop billing when the ASR stops producing SPEECH EVIDENCE
                    # even though the room hasn't gone quiet (the energy gate can't tell noise from
                    # speech). Evidence = the session's `last_activity` (non-empty partials + finals —
                    # partials fire on quiet/far-field speech that never commits a final, so a hard-to-
                    # hear live meeting doesn't read as "over"), with the committed-turn count as the
                    # fallback for backends without it. Neither signal → idle-close disabled.
                    if gate.enabled and session is not None and cfg.transcript_idle_close_s > 0:
                        act = getattr(session, "last_activity", None)
                        turns = getattr(session, "turns", None)
                        if act is not None and act > last_evidence_ts:
                            last_evidence_ts = act
                        if turns is not None and turns != last_turns:
                            last_turns = turns
                            last_evidence_ts = max(last_evidence_ts, now)
                        if (
                            act is not None or turns is not None
                        ) and now - last_evidence_ts >= cfg.transcript_idle_close_s:
                            try:
                                await session.finalize()
                            except Exception:
                                logger.warning("meeting session idle-finalize failed for %s", source, exc_info=True)
                            session = None
                            gate.reset()
                            dormant = True
                            self._sessions_idle_closed += 1
                            logger.info(
                                "meeting cloud session CLOSE (transcript idle %.0fs): %s",
                                cfg.transcript_idle_close_s,
                                source,
                            )
                    # `dormant` suppresses billing on noise that isn't a new meeting; a freshly-opened
                    # session seeds the idle tracker so a session that never transcribes still closes.
                    send = forward and not dormant
                    if send:
                        if session is None:
                            session = await self._open_session(cfg, source)
                            opened += 1
                            last_turns = getattr(session, "turns", 0) or 0
                            last_evidence_ts = now
                        await session.send_audio(msg.data)
                        self._frames += 1
                        self._bytes += len(msg.data)
                        self._last_frame_ts = time.time()
                    else:
                        self._frames_gated += 1
                        if close and session is not None:
                            # Mirror the teardown finalize's containment: a pluggable backend whose
                            # finalize() can raise must not kill the connection (and session=None
                            # below prevents a double-finalize in the finally block).
                            try:
                                await session.finalize()
                            except Exception:
                                logger.warning("meeting session silence-finalize failed for %s", source, exc_info=True)
                            session = None
                            gate.reset()
                            logger.info("meeting cloud session CLOSE (silence): %s", source)
                    summary = stats.observe(peak, send, now)
                    if summary is not None:
                        mode = f"ON(thr={cfg.vad_threshold})" if gate.enabled else "OFF(observe)"
                        logger.info(
                            "meeting vad[%.0fs] %s: fwd=%d gated=%d max_peak=%d avg_peak=%d",
                            summary.window_s,
                            mode,
                            summary.forwarded,
                            summary.gated,
                            summary.max_peak,
                            summary.avg_peak,
                        )
                elif msg.type == WSMsgType.TEXT:
                    # A marker is an explicit "pay attention here" — never drop it into a silence
                    # gap, and it ends dormancy (an explicit "a new meeting starts here").
                    if self._is_marker(msg.data):
                        dormant = False
                        if session is None:
                            session = await self._open_session(cfg, source)
                            opened += 1
                            last_turns = getattr(session, "turns", 0) or 0
                            last_evidence_ts = time.monotonic()
                        session.add_marker()
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.ERROR):
                    break
        except Exception:
            logger.warning("meeting ws handler error for %s", source, exc_info=True)
        finally:
            if session is not None:
                try:
                    await session.finalize()
                except Exception:
                    logger.warning("meeting session finalize failed for %s", source, exc_info=True)
            self._active -= 1
            logger.info("meeting connection closed: %s (meetings=%d, active=%d)", source, opened, self._active)
            if not ws.closed:
                await ws.close()
        return ws

    async def _open_session(self, cfg: MeetingConfig, source: str):
        """Build + start a cloud session and count it. Kept tiny so the BINARY (speech) and TEXT
        (marker) paths open sessions identically."""
        session = self._session_factory(cfg, source)
        await session.start()
        self._sessions_total += 1
        logger.info(
            "meeting cloud session OPEN #%d: %s (transcript: %s)",
            self._sessions_total,
            source,
            getattr(session, "path", "?"),
        )
        return session

    @staticmethod
    def _is_marker(raw: str) -> bool:
        """True iff `raw` is a JSON control frame of type 'marker'. Non-JSON / other → False."""
        try:
            return json.loads(raw).get("type") == "marker"
        except ValueError:
            return False

    # ── health ───────────────────────────────────────────────────────────────
    async def _handle_health(self, _request: web.Request) -> web.Response:
        # Unauthenticated liveness ONLY — deliberately minimal (no session counts, activity
        # timestamps, or pid) because this route is reachable through the public Funnel. Full
        # operational metrics require the token: GET /health/<token>.
        return web.json_response({"alive": True, "ts": time.time()})

    async def _handle_health_full(self, request: web.Request) -> web.Response:
        token = request.match_info.get("token", "")
        if not self._authenticate(token):
            return web.Response(status=403, text="forbidden")
        return web.json_response(self._health_payload())

    def _health_payload(self) -> dict:
        return {
            "ts": time.time(),
            "alive": True,
            "active_sessions": self._active,
            "sessions_total": self._sessions_total,
            "sessions_idle_closed": self._sessions_idle_closed,
            "frames": self._frames,
            "frames_gated": self._frames_gated,
            "bytes": self._bytes,
            "last_frame_ts": self._last_frame_ts,
            "pid": os.getpid(),
        }

    def _write_health(self) -> None:
        try:
            tmp = self._cfg.health_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._health_payload(), f)
            os.replace(tmp, self._cfg.health_path)
        except Exception:
            logger.warning("health write failed", exc_info=True)

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(self._cfg.health_interval_s)
            self._write_health()

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def serve(self) -> None:
        os.makedirs(self._cfg.output_dir, exist_ok=True)
        # access_log=None: the secret token rides the URL PATH (a browser can't send auth headers),
        # so aiohttp's default access log would write the token to disk on every request.
        runner = web.AppRunner(self.build_app(), access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self._cfg.host, self._cfg.port)
        await site.start()
        logger.info("meeting bridge listening on %s:%d (capture + /meeting ws)", self._cfg.host, self._cfg.port)
        self._write_health()
        tasks = [tracked_task(self._health_loop(), name="meeting-health")]

        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        try:
            await stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            await runner.cleanup()
            self.close()

    def close(self) -> None:
        self._closed = True


def _resolve_factory(path: str):
    """Resolve a ``module.path:callable`` string to the session factory callable."""
    import importlib

    mod_name, _, attr = path.partition(":")
    if not attr:
        raise ValueError(f"MEETING_SESSION_FACTORY must be 'module:callable', got {path!r}")
    return getattr(importlib.import_module(mod_name), attr)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("MEETING_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = load_config()
    factory = _resolve_factory(cfg.session_factory_path)
    asyncio.run(MeetingServer(cfg, session_factory=factory).serve())


if __name__ == "__main__":
    main()
