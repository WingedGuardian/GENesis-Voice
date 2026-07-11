"""Server tests for the meeting bridge (aiohttp TestClient, in-loop).

The cloud session is dependency-injected via a fake factory, so these exercise the server's
own responsibilities — path-token auth on both the capture page and the WebSocket, binary-PCM
relay to the session, marker control frames, graceful finalize on close, the oversize-frame
guard, and the health JSON — WITHOUT needing speechmatics-rt / numpy.
"""

import asyncio
import json

import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from meeting_bridge.config import MeetingConfig
from meeting_bridge.server import MeetingServer

TOKEN = "meet-secret-123"


def _cfg(tmp_path, **over):
    base = dict(
        ingest_token=TOKEN,
        output_dir=str(tmp_path / "sessions"),
        health_path=str(tmp_path / "meeting_health.json"),
    )
    base.update(over)
    return MeetingConfig(**base)


class FakeSession:
    """Records what the server relays; signals completion via an event (no wall-clock waits)."""

    def __init__(self, done: asyncio.Event):
        self._done = done
        self.frames: list[bytes] = []
        self.markers = 0
        self.started = False
        self.finalized = False
        self.path = "/fake/session.md"

    async def start(self):
        self.started = True

    async def send_audio(self, frame: bytes):
        self.frames.append(frame)

    def add_marker(self):
        self.markers += 1

    async def finalize(self):
        self.finalized = True
        self._done.set()


def _server_with_fake(cfg):
    """Return (server, get_session) where get_session() yields the last FakeSession built."""
    box = {}
    done = asyncio.Event()

    def factory(_cfg, source):
        s = FakeSession(done)
        box["session"] = s
        box["source"] = source
        return s

    server = MeetingServer(cfg, session_factory=factory)
    return server, box, done


async def _client(server):
    return TestClient(TestServer(server.build_app()))


@pytest.mark.asyncio
async def test_capture_page_served_with_token(tmp_path):
    cfg = _cfg(tmp_path)
    server, _box, _done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            r = await c.get(f"/capture/{TOKEN}")
            assert r.status == 200
            body = await r.text()
            # The page must carry the token so its JS can open the same-origin wss.
            assert TOKEN in body
            assert "meeting/" in body
    finally:
        server.close()


@pytest.mark.asyncio
async def test_capture_bad_token_403(tmp_path):
    cfg = _cfg(tmp_path)
    server, _box, _done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            r = await c.get("/capture/wrong-token")
            assert r.status == 403
    finally:
        server.close()


@pytest.mark.asyncio
async def test_ws_bad_token_rejected_before_upgrade(tmp_path):
    from aiohttp import WSServerHandshakeError

    cfg = _cfg(tmp_path)
    server, box, _done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            with pytest.raises(WSServerHandshakeError) as ei:
                await c.ws_connect("/meeting/wrong-token")
            assert ei.value.status == 403
            assert "session" not in box  # no session ever created for a bad token
    finally:
        server.close()


@pytest.mark.asyncio
async def test_ws_relays_pcm_and_marker_then_finalizes(tmp_path):
    cfg = _cfg(tmp_path)
    server, box, done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}")
            await ws.send_bytes(b"\x01\x02\x03\x04")
            await ws.send_bytes(b"\x05\x06")
            await ws.send_str(json.dumps({"type": "marker"}))
            await ws.close()
            await asyncio.wait_for(done.wait(), timeout=2)
        s = box["session"]
        assert s.started is True
        assert s.frames == [b"\x01\x02\x03\x04", b"\x05\x06"]
        assert s.markers == 1
        assert s.finalized is True
    finally:
        server.close()


@pytest.mark.asyncio
async def test_oversize_frame_guarded(tmp_path):
    cfg = _cfg(tmp_path, max_frame_bytes=8)
    server, box, done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}")
            await ws.send_bytes(b"x" * 64)  # over the 8-byte cap
            await asyncio.wait_for(done.wait(), timeout=2)  # server closes + finalizes
            await ws.close()
        s = box["session"]
        assert s.frames == []  # the oversize frame was never relayed
        assert s.finalized is True
    finally:
        server.close()


@pytest.mark.asyncio
async def test_health_file_written(tmp_path):
    cfg = _cfg(tmp_path)
    server, _box, _done = _server_with_fake(cfg)
    try:
        server._write_health()
        data = json.loads((tmp_path / "meeting_health.json").read_text())
        assert "sessions_total" in data and "active_sessions" in data
    finally:
        server.close()


@pytest.mark.asyncio
async def test_ws_heartbeat_finalizes_silent_peer(tmp_path):
    # A phone that vanishes without a WS close frame (screen lock / tab kill / wifi handoff) must
    # still get finalized — the heartbeat pings it and force-closes on a missed pong. The client
    # goes silent (autoping=False, never closes); the session must finalize regardless.
    cfg = _cfg(tmp_path, ws_heartbeat_s=0.2)
    server, box, done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            await c.ws_connect(f"/meeting/{TOKEN}", autoping=False)
            await asyncio.wait_for(done.wait(), timeout=3)  # heartbeat-driven finalize
        assert box["session"].finalized is True
    finally:
        server.close()


@pytest.mark.asyncio
async def test_factory_failure_is_contained(tmp_path):
    # If the session factory raises (e.g. ActiveSession's makedirs hits a bad path), the handler
    # must contain it — no active-count leak, no wedge — and the server stays healthy for the next
    # connection.
    cfg = _cfg(tmp_path)

    def bad_factory(_cfg, _source):
        raise RuntimeError("boom")

    server = MeetingServer(cfg, session_factory=bad_factory)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}")
            msg = await asyncio.wait_for(ws.receive(), timeout=3)  # clean close, not a hang
            assert msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED)
        assert server._active == 0  # incremented + decremented cleanly, no leak
    finally:
        server.close()
