"""Listen-mode bridge: device WS (:8766) -> Speechmatics realtime -> live transcript file.

Each device connection is one listen session: open a Speechmatics realtime client,
relay the device's 16k PCM frames to it, and write a live-updating, speaker-labelled
markdown transcript to the output dir. No Genesis contact, no memory ingestion.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import time
from datetime import UTC, datetime

import websockets
from speechmatics.rt import (
    AsyncClient,
    AudioEncoding,
    AudioFormat,
    ServerMessageType,
    SpeakerDiarizationConfig,
    TranscriptionConfig,
)

from .config import ListenConfig, load_config
from .transcript import TranscriptAccumulator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("listen.server")


def _enable_tcp_keepalive(sock, cfg: ListenConfig) -> None:
    """Reap a silently-dead (half-open) Voice PE in minutes (WS pings are off — the device
    rejects them). Best-effort: a missing socket or unsupported option is logged, never fatal."""
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


class ListenServer:
    def __init__(self, cfg: ListenConfig) -> None:
        self._cfg = cfg
        self._active = 0
        self._handler_tasks: set[asyncio.Task] = set()
        self._active_sessions: set[str] = set()
        self._last_connection_ts: str | None = None
        os.makedirs(cfg.output_dir, exist_ok=True)

    def _read_api_key(self) -> str:
        with open(self._cfg.api_key_path) as f:
            return f.read().strip()

    def _transcription_config(self) -> TranscriptionConfig:
        return TranscriptionConfig(
            language=self._cfg.language,
            model=self._cfg.model,
            max_delay=self._cfg.max_delay,
            enable_partials=self._cfg.enable_partials,
            diarization="speaker",
            speaker_diarization_config=SpeakerDiarizationConfig(max_speakers=self._cfg.max_speakers),
        )

    # --- connection handling --------------------------------------------------

    async def _handler(self, websocket) -> None:
        source = f"listen-{getattr(websocket, 'remote_address', ('?',))[0]}"
        transport = getattr(websocket, "transport", None)
        sock = transport.get_extra_info("socket") if transport is not None else None
        _enable_tcp_keepalive(sock, self._cfg)
        task = asyncio.current_task()
        self._handler_tasks.add(task)
        self._active += 1
        ts = datetime.now(UTC)
        self._last_connection_ts = ts.isoformat()
        stamp = ts.strftime("%Y%m%dT%H%M%S")
        out_path = os.path.join(self._cfg.output_dir, f"{stamp}.md")
        acc = TranscriptAccumulator(title=f"Listen session {ts.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        self._active_sessions.add(stamp)
        logger.info("Listen START %s -> %s (active=%d)", source, out_path, self._active)

        def _flush() -> None:
            try:
                tmp = out_path + ".tmp"
                with open(tmp, "w") as f:
                    f.write(acc.render())
                os.replace(tmp, out_path)
            except Exception:  # noqa: BLE001
                logger.warning("transcript write failed for %s", out_path, exc_info=True)

        last_partial = 0.0

        def _flush_partial() -> None:
            # Partials fire up to ~2/s; bound event-loop file I/O so a slow edge disk
            # can't backpressure audio relay. Provisional anyway — ~1/s suits a live tail.
            nonlocal last_partial
            now = time.monotonic()
            if now - last_partial >= 1.0:
                last_partial = now
                _flush()

        try:
            api_key = self._read_api_key()
        except FileNotFoundError:
            logger.error("Speechmatics key missing at %s — listen session aborted", self._cfg.api_key_path)
            self._handler_tasks.discard(task)
            self._active -= 1
            self._active_sessions.discard(stamp)
            return

        audio_format = AudioFormat(
            encoding=AudioEncoding.PCM_S16LE, sample_rate=self._cfg.sample_rate, chunk_size=4096,
        )
        client_kwargs: dict = {"api_key": api_key}
        if self._cfg.connection_url:
            client_kwargs["url"] = self._cfg.connection_url
        try:
            async with AsyncClient(**client_kwargs) as client:
                client.on(ServerMessageType.ADD_TRANSCRIPT, lambda m: (acc.add_final(m), _flush()))
                client.on(ServerMessageType.ADD_PARTIAL_TRANSCRIPT, lambda m: (acc.set_partial(m), _flush_partial()))
                client.on(ServerMessageType.ERROR, lambda m: logger.error("Speechmatics ERROR: %s", str(m)[:200]))
                client.on(ServerMessageType.WARNING, lambda m: logger.warning("Speechmatics WARNING: %s", str(m)[:200]))
                _flush()  # write the header now so the file exists for tailing immediately
                await client.start_session(transcription_config=self._transcription_config(), audio_format=audio_format)
                try:
                    async for message in websocket:
                        if isinstance(message, (bytes, bytearray)):
                            await client.send_audio(bytes(message))
                        elif isinstance(message, str) and self._is_disconnect(message):
                            break
                finally:
                    # Always drain finals, however the audio loop ended (clean stop, break,
                    # or ConnectionClosed propagating out of the async-for).
                    with contextlib.suppress(Exception):
                        await client.stop_session()
        except websockets.ConnectionClosed as exc:
            logger.info("Listen %s closed (code=%s reason=%r)", source, exc.code, exc.reason)
        except Exception:  # noqa: BLE001
            logger.exception("Listen handler error for %s", source)
        finally:
            _flush()
            self._handler_tasks.discard(task)
            self._active -= 1
            self._active_sessions.discard(stamp)
            logger.info("Listen END %s -> %s (active=%d, turns=%d)", source, out_path, self._active, len(acc.committed))

    @staticmethod
    def _is_disconnect(message: str) -> bool:
        try:
            return json.loads(message).get("type") == "disconnect"
        except ValueError:
            return False

    # --- health + lifecycle ---------------------------------------------------

    def _write_health(self) -> None:
        try:
            payload = {
                "ts": datetime.now(UTC).isoformat(),
                "alive": True,
                "active_connections": self._active,
                "last_connection_ts": self._last_connection_ts,
                "listening": sorted(self._active_sessions) or None,
            }
            tmp = self._cfg.health_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, self._cfg.health_path)
        except Exception:  # noqa: BLE001
            logger.warning("health write failed", exc_info=True)

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            self._write_health()

    async def run(self) -> None:
        tasks = [tracked_task(self._health_loop(), name="listen-health")]
        self._write_health()
        logger.info(
            "Listen bridge listening on ws://%s:%d/ -> Speechmatics realtime (model=%s, max_speakers=%d)",
            self._cfg.host, self._cfg.port, self._cfg.model, self._cfg.max_speakers,
        )
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        try:
            async with websockets.serve(
                self._handler, self._cfg.host, self._cfg.port, max_size=None, ping_interval=None,
            ):
                await stop.wait()
        finally:
            if self._handler_tasks:
                await asyncio.wait(self._handler_tasks, timeout=10.0)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            logger.info("Listen bridge stopped.")


def main() -> None:
    asyncio.run(ListenServer(load_config()).run())


if __name__ == "__main__":
    main()
