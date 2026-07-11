"""Meeting bridge server — aiohttp app: authenticated audio WebSocket + the phone capture page.

One app, one port. ``GET /capture/<token>`` serves the phone PWA; ``GET /meeting/<token>`` is the
audio WebSocket (raw 16-bit PCM binary frames + JSON control frames). Both gate on a constant-time
path-token compare (the browser can't set custom WS headers, so the token rides the URL). Each WS
connection opens ONE cloud session (dependency-injected — default = Speechmatics via
``ActiveSession``), relays PCM to it, and finalizes on disconnect.

Runs behind the Tailscale Funnel (the one authenticated public door); binds loopback by default.
No ``genesis.*`` imports. The cloud SDK is imported lazily by the default factory, so the server
imports (and unit-tests) without speechmatics-rt installed.
"""

from __future__ import annotations

import asyncio
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

logger = logging.getLogger(__name__)

_CAPTURE_HTML_PATH = Path(__file__).with_name("capture.html")
_TOKEN_PLACEHOLDER = "__MEETING_TOKEN__"  # replaced per-request with the validated path token


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
        self._active = 0
        self._sessions_total = 0
        self._frames = 0
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
        session = None
        self._active += 1
        self._sessions_total += 1
        logger.info("meeting session opening: %s (active=%d)", source, self._active)
        try:
            # Inside the try so a factory/start failure is CONTAINED (logged, not propagated to
            # aiohttp's error logger) and still runs the finally cleanup below.
            session = self._session_factory(self._cfg, source)
            await session.start()
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    await session.send_audio(msg.data)
                    self._frames += 1
                    self._bytes += len(msg.data)
                    self._last_frame_ts = time.time()
                elif msg.type == WSMsgType.TEXT:
                    self._on_control(session, msg.data)
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
            logger.info("meeting session closed: %s (active=%d)", source, self._active)
            if not ws.closed:
                await ws.close()
        return ws

    def _on_control(self, session, raw: str) -> None:
        try:
            data = json.loads(raw)
        except ValueError:
            logger.debug("meeting: non-JSON control frame ignored")
            return
        if data.get("type") == "marker":
            session.add_marker()

    # ── health ───────────────────────────────────────────────────────────────
    async def _handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response(self._health_payload())

    def _health_payload(self) -> dict:
        return {
            "ts": time.time(),
            "alive": True,
            "active_sessions": self._active,
            "sessions_total": self._sessions_total,
            "frames": self._frames,
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
