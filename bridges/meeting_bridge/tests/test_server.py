"""Server tests for the meeting bridge (aiohttp TestClient, in-loop).

The cloud session is dependency-injected via a fake factory, so these exercise the server's
own responsibilities — path-token auth on both the capture page and the WebSocket, binary-PCM
relay to the session, marker control frames, graceful finalize on close, the oversize-frame
guard, and the health JSON — WITHOUT needing speechmatics-rt / numpy.
"""

import asyncio
import json
import struct

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

    def __init__(self, done: asyncio.Event, idx: int = 0):
        self._done = done
        self.frames: list[bytes] = []
        self.markers = 0
        self.started = False
        self.finalized = False
        self.path = f"/fake/session-{idx}.md"

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
    """Return (server, box, done).

    ``box['sessions']`` lists every FakeSession built on the connection, in order (the VAD lifecycle
    can open more than one per connection); ``box['session']`` is the most recent; ``done`` fires on
    each finalize.
    """
    box = {"sessions": []}
    done = asyncio.Event()

    def factory(_cfg, source):
        s = FakeSession(done, idx=len(box["sessions"]))
        box["sessions"].append(s)
        box["session"] = s
        box["source"] = source
        box["cfg"] = _cfg  # capture the per-connection cfg to assert the model override
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
async def test_model_query_override_applied(tmp_path):
    # The client picks the Speechmatics model per session via ?model=; a valid value reaches the
    # session factory as cfg.model (default is "enhanced").
    cfg = _cfg(tmp_path, model="enhanced")
    server, box, done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}?model=standard")
            await ws.send_bytes(b"\x01\x02")
            await ws.close()
            await asyncio.wait_for(done.wait(), timeout=2)
        assert box["cfg"].model == "standard"
    finally:
        server.close()


@pytest.mark.asyncio
async def test_model_query_invalid_falls_back_to_default(tmp_path):
    # A bogus/hostile ?model= is never trusted raw — it falls back to the configured default.
    cfg = _cfg(tmp_path, model="enhanced")
    server, box, done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}?model=hackzor")
            await ws.send_bytes(b"\x01\x02")
            await ws.close()
            await asyncio.wait_for(done.wait(), timeout=2)
        assert box["cfg"].model == "enhanced"
    finally:
        server.close()


@pytest.mark.asyncio
async def test_oversize_frame_guarded(tmp_path):
    cfg = _cfg(tmp_path, max_frame_bytes=8)
    server, box, done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}")
            await ws.send_bytes(b"x" * 64)  # over the 8-byte cap → WS ERROR tears the conn down
            msg = await asyncio.wait_for(ws.receive(), timeout=3)  # clean close, not a hang
            assert msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED)
        # Lazy creation: an oversize frame trips before any audio is relayed, so no billed cloud
        # session is ever opened, and the connection closes cleanly with no active-count leak.
        assert box["sessions"] == []
        assert server._active == 0
    finally:
        server.close()


@pytest.mark.asyncio
async def test_health_unauth_is_minimal(tmp_path):
    # The unauthenticated /health is reachable through the public Funnel, so it must expose ONLY
    # liveness — never session counts, activity timestamps, or the pid.
    cfg = _cfg(tmp_path)
    server, _box, _done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            r = await c.get("/health")
            assert r.status == 200
            data = await r.json()
            assert data["alive"] is True and "ts" in data
            for leaked in ("active_sessions", "sessions_total", "frames", "bytes", "last_frame_ts", "pid"):
                assert leaked not in data
    finally:
        server.close()


@pytest.mark.asyncio
async def test_health_full_requires_token(tmp_path):
    # Full operational metrics live behind the token: good token → full payload; bad token → 403.
    cfg = _cfg(tmp_path)
    server, _box, _done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            ok = await c.get(f"/health/{TOKEN}")
            assert ok.status == 200
            data = await ok.json()
            assert "sessions_total" in data and "pid" in data

            bad = await c.get("/health/wrong-token")
            assert bad.status == 403
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
async def test_ws_heartbeat_finalizes_vanished_peer(tmp_path):
    # A phone that opened a session then vanished without a WS close (screen lock / tab kill / wifi
    # handoff) must still get its cloud session finalized — the heartbeat pings and force-closes on
    # a missed pong. Send one frame to open the session (default threshold=0 → every frame is
    # speech), then go silent with autoping off so no pong is answered.
    cfg = _cfg(tmp_path, ws_heartbeat_s=0.2)
    server, box, done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}", autoping=False)
            await ws.send_bytes(b"\x01\x02")  # opens a session
            await asyncio.wait_for(done.wait(), timeout=3)  # heartbeat-driven finalize
        assert box["session"].finalized is True
    finally:
        server.close()


@pytest.mark.asyncio
async def test_factory_failure_is_contained(tmp_path):
    # If the session factory raises when first needed (the first speech frame), the handler must
    # contain it — no active-count leak, no wedge — and the server stays healthy for the next conn.
    cfg = _cfg(tmp_path)

    def bad_factory(_cfg, _source):
        raise RuntimeError("boom")

    server = MeetingServer(cfg, session_factory=bad_factory)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}")
            await ws.send_bytes(b"\x01\x02")  # triggers the lazy factory → raises → contained
            msg = await asyncio.wait_for(ws.receive(), timeout=3)  # clean close, not a hang
            assert msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED)
        assert server._active == 0  # incremented + decremented cleanly, no leak
    finally:
        server.close()


# ── VAD-driven session lifecycle ─────────────────────────────────────────────
def _pcm(*samples: int) -> bytes:
    """Little-endian PCM16 frame from int16 samples (for driving the energy gate)."""
    return struct.pack(f"<{len(samples)}h", *samples)


@pytest.mark.asyncio
async def test_vad_disabled_default_single_session_relays_all(tmp_path):
    # Default threshold=0 → gating OFF: one session spans the whole connection and every frame is
    # relayed, even a near-silent one (legacy behavior preserved).
    cfg = _cfg(tmp_path)
    assert cfg.vad_threshold == 0
    server, box, done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}")
            await ws.send_bytes(_pcm(2000))
            await ws.send_bytes(_pcm(0))  # near-silent, but gate is off → still relayed
            await ws.close()
            await asyncio.wait_for(done.wait(), timeout=2)
        assert len(box["sessions"]) == 1  # exactly one cloud session for the connection
        assert box["session"].frames == [_pcm(2000), _pcm(0)]
        assert server._frames_gated == 0
    finally:
        server.close()


@pytest.mark.asyncio
async def test_vad_quiet_frames_never_open_a_session(tmp_path):
    # With the gate armed, sub-threshold frames at the START of a connection open NO cloud session
    # (nothing to transcribe / bill) and are counted as gated.
    cfg = _cfg(tmp_path, vad_threshold=5000)
    server, box, done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}")
            for _ in range(3):
                await ws.send_bytes(_pcm(200))  # peak 200 << 5000 → silence
            await ws.close()
        assert box["sessions"] == []  # never opened a billed session on pure silence
        assert server._frames_gated == 3
        assert server._frames == 0
    finally:
        server.close()


@pytest.mark.asyncio
async def test_vad_speech_opens_session_and_relays(tmp_path):
    # A loud (speech) frame opens a session and is relayed; the connection's one meeting finalizes
    # on close.
    cfg = _cfg(tmp_path, vad_threshold=1000)
    server, box, done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}")
            await ws.send_bytes(_pcm(8000))  # peak 8000 >= 1000 → speech
            await ws.close()
            await asyncio.wait_for(done.wait(), timeout=2)
        assert len(box["sessions"]) == 1
        assert box["session"].frames == [_pcm(8000)]
        assert box["session"].finalized is True
    finally:
        server.close()


@pytest.mark.asyncio
async def test_marker_opens_session_when_none_active(tmp_path):
    # A marker must never be dropped into a silence gap: with the gate armed and no session open, a
    # marker control frame opens a session so the mark lands.
    cfg = _cfg(tmp_path, vad_threshold=5000)
    server, box, done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}")
            await ws.send_str(json.dumps({"type": "marker"}))  # no audio yet → still opens a session
            await ws.close()
            await asyncio.wait_for(done.wait(), timeout=2)
        assert len(box["sessions"]) == 1
        assert box["session"].markers == 1
    finally:
        server.close()


@pytest.mark.asyncio
async def test_vad_segments_two_sessions_across_silence(tmp_path):
    # Speech → sustained silence → speech opens a SECOND cloud session (one file per meeting). Uses
    # a tiny silence_close_s + a real gap (same timing style as the heartbeat test); the pure
    # open/close/reopen decision logic is covered deterministically in test_vad.py.
    cfg = _cfg(tmp_path, vad_threshold=1000, vad_hangover_s=0.0, silence_close_s=0.05)
    server, box, done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}")
            await ws.send_bytes(_pcm(8000))  # meeting 1: speech opens session 0
            await asyncio.sleep(0.12)  # silence gap > silence_close_s
            await ws.send_bytes(_pcm(50))  # silent frame → finalizes session 0
            await ws.send_bytes(_pcm(8000))  # meeting 2: speech opens session 1
            await ws.close()
            await asyncio.wait_for(done.wait(), timeout=2)
        assert len(box["sessions"]) == 2  # two meetings → two cloud sessions / two files
        assert box["sessions"][0].finalized is True  # first meeting closed on silence
        assert box["sessions"][1].finalized is True  # second meeting closed on teardown
        assert box["sessions"][0].frames == [_pcm(8000)]
        assert box["sessions"][1].frames == [_pcm(8000)]
    finally:
        server.close()
