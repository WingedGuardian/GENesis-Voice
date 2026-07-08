"""OMI wearable ingest server — aiohttp receiver for OMI's real-time transcript webhook.

Standalone edge service (own venv), no ``genesis.*`` imports. Receives ``POST
/omi/<token>/ingest?uid=<uid>`` (OMI's dev real-time webhook body ``{segments, session_id}``),
dedups, anchors conversation-relative timestamps to wall-clock, and writes rows into the SHARED
``ambient.db`` via the ambient bridge's ``AmbientStore`` (single schema source of truth).

Error policy — the receiver must be a good webhook citizen. OMI retries a non-2xx (1s/5s/30s),
trips a circuit breaker, and auto-disables the webhook after consecutive failures. So after auth
we **never** return 5xx/429: an internal error DROPS the batch with a 200. We also never return a
JSON ``message`` (>5 chars) — OMI would turn it into a phone push notification. Auth failures
(bad token / disallowed uid) DO return 403, and malformed bodies 400 / oversize 413 — none of
which the real device ever triggers.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import signal
import time

from aiohttp import web

# Single schema source of truth: the ambient bridge owns the ``ambient_transcripts`` table.
# ``store`` is stdlib-only and ``ambient_bridge/__init__`` is import-light, so this pulls in no
# sherpa/ML deps — safe in the lean omi venv.
from ambient_bridge.store import AmbientStore

from .config import OmiConfig, load_config
from .normalize import _as_float, decide_anchor, normalize_segments, parse_payload
from .state import OmiState

logger = logging.getLogger(__name__)


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


class OmiServer:
    def __init__(self, cfg: OmiConfig):
        self._cfg = cfg
        self._store = AmbientStore(cfg.db_path)
        self._state = OmiState(cfg.state_db_path)
        # One data-path lock: dedup -> anchor read/modify/write -> insert must be serialized
        # (OmiState is a single shared connection; the anchor is a read-modify-write).
        self._lock = asyncio.Lock()
        self._closed = False
        # counters (surfaced in the health JSON)
        self._received = 0
        self._inserted = 0
        self._duplicates = 0
        self._dropped = 0
        self._last_ingest_ts: float | None = None

    # ── app wiring ─────────────────────────────────────────────────────────
    def build_app(self) -> web.Application:
        # client_max_size enforces the 413 before we ever parse the body.
        app = web.Application(client_max_size=self._cfg.max_body_bytes)
        app.add_routes([web.post("/omi/{token}/ingest", self._handle_ingest)])
        return app

    # ── auth ───────────────────────────────────────────────────────────────
    def _authenticate(self, token: str, uid: str | None) -> bool:
        candidates = self._cfg.token_candidates()
        if not candidates:
            return False  # no token configured -> nothing authenticates (fail closed)
        tok = token.encode("utf-8", "ignore")
        token_ok = any(hmac.compare_digest(tok, c.encode("utf-8")) for c in candidates)
        return token_ok and self._cfg.uid_allowed(uid)

    # ── ingest handler ─────────────────────────────────────────────────────
    async def _handle_ingest(self, request: web.Request) -> web.Response:
        token = request.match_info.get("token", "")
        uid = request.query.get("uid")
        if not self._authenticate(token, uid):
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid json"}, status=400)
        # NB: an oversize body raises HTTPRequestEntityTooLarge (413) inside request.json()
        # above — an HTTPException, NOT caught here, so aiohttp renders the 413 cleanly.
        try:
            accepted, duplicates = await self._ingest(uid, data, request.headers.get("Idempotency-Key"))
        except Exception:
            # NEVER 5xx after auth: a drop is safer than tripping OMI's retry/disable machinery.
            logger.warning("omi ingest dropped (returning 200)", exc_info=True)
            self._dropped += 1
            return web.json_response({"accepted": 0}, status=200)
        return web.json_response({"accepted": accepted, "duplicates": duplicates}, status=200)

    async def _ingest(self, uid: str | None, data, idem_key: str | None) -> tuple[int, int]:
        async with self._lock:
            now = time.time()
            self._received += 1
            # Idempotency-Key (bonus layer; absent in prod) — skips a whole re-delivered batch.
            if self._state.is_duplicate_delivery(idem_key, now=now):
                return 0, 0
            session_id, segments = parse_payload(data)
            if session_id and uid and session_id != uid:
                logger.debug("omi session_id != query uid (using query uid for source)")
            if not segments:
                return 0, 0
            seen = self._state.seen_segment_ids([s.get("id") for s in segments], now=now)
            fresh = [s for s in segments if not (s.get("id") and s.get("id") in seen)]
            dup_count = len(segments) - len(fresh)
            if not fresh:
                return 0, dup_count
            batch_max_end = max((_as_float(s.get("end")) for s in fresh), default=0.0)
            cur = self._state.get_anchor(uid)
            epoch0 = decide_anchor(cur[0] if cur else None, batch_max_end, now, self._cfg.anchor_tolerance_s)
            self._state.set_anchor(uid, epoch0, batch_max_end, now=now)
            rows = normalize_segments(fresh, uid=uid, epoch0=epoch0)
            inserted = 0
            for r in rows:
                row_id = self._store.insert(
                    text=r.text, duration_s=r.duration_s, source=r.source, meta=r.meta, ts=r.ts
                )
                if r.is_user is not None:
                    self._store.set_identity(
                        row_id, speaker_name=r.speaker_name, is_user=bool(r.is_user), method="omi_webhook"
                    )
                inserted += 1
            # Mark segments seen only AFTER a successful insert, so a mid-batch failure re-processes.
            self._state.record_segment_ids([s.get("id") for s in fresh], now=now)
            self._inserted += inserted
            self._duplicates += dup_count
            self._last_ingest_ts = now
            return inserted, dup_count

    # ── health + purge ─────────────────────────────────────────────────────
    def _write_health(self) -> None:
        try:
            try:
                stats = self._store.stats()
            except Exception:
                stats = {"total_rows": None, "last_ts": None, "rows_last_hour": None}
            payload = {
                **stats,
                "received": self._received,
                "inserted": self._inserted,
                "duplicates": self._duplicates,
                "dropped": self._dropped,
                "last_ingest_ts": self._last_ingest_ts,
                "pid": os.getpid(),
            }
            tmp = self._cfg.health_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, self._cfg.health_path)
        except Exception:
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
                    self._store.purge, self._cfg.ttl_hours, self._cfg.row_ceiling
                )
                if ttl_d or ceil_d:
                    logger.info("purge: ttl=%d ceiling=%d", ttl_d, ceil_d)
            except Exception:
                logger.warning("purge failed", exc_info=True)

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def serve(self) -> None:
        try:
            await asyncio.to_thread(self._store.purge, self._cfg.ttl_hours, self._cfg.row_ceiling)
        except Exception:
            logger.warning("startup purge failed", exc_info=True)

        runner = web.AppRunner(self.build_app())
        await runner.setup()
        site = web.TCPSite(runner, self._cfg.host, self._cfg.port)
        await site.start()
        logger.info("omi ingest listening on %s:%d", self._cfg.host, self._cfg.port)

        self._write_health()
        tasks = [
            tracked_task(self._health_loop(), name="omi-health"),
            tracked_task(self._purge_loop(), name="omi-purge"),
        ]

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
        if self._closed:
            return
        self._closed = True
        try:
            self._store.close()
        finally:
            self._state.close()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("OMI_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(OmiServer(load_config()).serve())


if __name__ == "__main__":
    main()
