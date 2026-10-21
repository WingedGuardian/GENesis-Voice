"""Server tests for the meeting bridge (aiohttp TestClient, in-loop).

The cloud session is dependency-injected via a fake factory, so these exercise the server's
own responsibilities — path-token auth on both the capture page and the WebSocket, binary-PCM
relay to the session, marker control frames, graceful finalize on close, the oversize-frame
guard, and the health JSON — WITHOUT needing speechmatics-rt / numpy.
"""

import asyncio
import json
import struct
import time

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
        # Most tests drive the lifecycle with one-sample frames; pin the legacy open-on-first-loud-
        # frame rule here. The opening hysteresis has its own tests with realistic 100 ms frames.
        vad_open_speech_s=0.0,
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
        # ASR-evidence signals the server polls for the transcript-idle close, mirroring the real
        # ActiveSession: `last_activity` (monotonic ts of the last non-empty partial or final — the
        # primary signal) and `turns` (committed-turn count — the fallback). Tests bump these to
        # simulate Speechmatics hearing speech.
        self.turns = 0
        self.last_activity: float | None = None

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


async def _wait_for_session(box, timeout=1.0):
    """Await until the server has opened a FakeSession (the factory runs server-side, so a session
    isn't visible the instant the client's send_bytes returns)."""
    for _ in range(int(timeout / 0.005)):
        if box.get("session") is not None:
            return box["session"]
        await asyncio.sleep(0.005)
    raise AssertionError("session was never opened")


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
async def test_ws_negotiates_audio_receipt_ack_and_counts_all_received_pcm(tmp_path):
    cfg = _cfg(tmp_path, vad_threshold=5000)
    server, _box, _done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}")
            await ws.send_str(json.dumps({"type": "hello", "capabilities": ["audio_ack_v1"]}))
            hello = await asyncio.wait_for(ws.receive_json(), timeout=2)
            assert hello == {"type": "hello", "capabilities": ["audio_ack_v1"]}

            quiet = _pcm(0, 0, 0, 0)
            await ws.send_bytes(quiet)
            ack = await asyncio.wait_for(ws.receive_json(), timeout=2)
            assert ack == {"type": "audio_ack", "bytes": len(quiet)}
            await ws.close()
    finally:
        server.close()


@pytest.mark.asyncio
async def test_ws_does_not_send_ack_without_capability_negotiation(tmp_path):
    cfg = _cfg(tmp_path)
    server, _box, _done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}")
            await ws.send_bytes(b"\x01\x02")
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.receive_json(), timeout=0.05)
            await ws.close()
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


# ── transcript-idle close + dormant re-arm (the post-meeting billed-noise fix) ──────────────────
# silence_close_s is kept comfortably larger than transcript_idle_close_s so back-to-back frames
# can't accidentally re-arm dormancy mid-assertion; real gaps drive the tiny thresholds (same style
# as test_vad_segments_two_sessions_across_silence).


@pytest.mark.asyncio
async def test_transcript_idle_closes_and_goes_dormant(tmp_path):
    # A session whose committed transcript goes quiet (turns stops climbing) finalizes even though the
    # mic stays LOUD — and continuing loud "noise" then opens NO new session (dormant), so we stop
    # billing the post-meeting tail instead of running until disconnect.
    cfg = _cfg(tmp_path, vad_threshold=1000, vad_hangover_s=0.0, silence_close_s=0.5, transcript_idle_close_s=0.05)
    server, box, done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}")
            await ws.send_bytes(_pcm(8000))  # opens session 0 (turns stays 0 = no transcript)
            await asyncio.sleep(0.12)  # > transcript_idle_close_s with no new turn
            await ws.send_bytes(_pcm(8000))  # loud, but transcript idle → finalize + dormant
            await ws.send_bytes(_pcm(8000))  # loud noise while dormant → must NOT open a new session
            await ws.close()
            await asyncio.wait_for(done.wait(), timeout=2)
        assert len(box["sessions"]) == 1  # dormant suppressed reopening on noise
        assert box["sessions"][0].finalized is True
        assert server._sessions_idle_closed == 1
    finally:
        server.close()


@pytest.mark.asyncio
async def test_transcript_activity_keeps_session_open(tmp_path):
    # While Speechmatics keeps committing turns, the idle timer resets each turn → no premature close.
    cfg = _cfg(tmp_path, vad_threshold=1000, vad_hangover_s=0.0, silence_close_s=0.5, transcript_idle_close_s=0.08)
    server, box, done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}")
            await ws.send_bytes(_pcm(8000))  # opens session 0
            session = await _wait_for_session(box)
            for turn in range(1, 4):  # bump turns each step, gaps < idle → timer keeps resetting
                session.turns = turn
                await asyncio.sleep(0.03)
                await ws.send_bytes(_pcm(8000))
            await ws.close()
            await asyncio.wait_for(done.wait(), timeout=2)
        assert len(box["sessions"]) == 1  # never idle-closed mid-meeting
        assert server._sessions_idle_closed == 0
        assert box["sessions"][0].finalized is True  # closed on teardown, not idle
    finally:
        server.close()


@pytest.mark.asyncio
async def test_dormant_rearms_after_silence_opens_new_session(tmp_path):
    # After a transcript-idle close, a genuinely quiet room (silence_close_s) re-arms the connection so
    # a real NEXT meeting opens a fresh session.
    cfg = _cfg(tmp_path, vad_threshold=1000, vad_hangover_s=0.0, silence_close_s=0.1, transcript_idle_close_s=0.05)
    server, box, done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}")
            await ws.send_bytes(_pcm(8000))  # session 0
            await asyncio.sleep(0.12)
            await ws.send_bytes(_pcm(8000))  # idle-close → dormant (resets the silence timer)
            await asyncio.sleep(0.25)  # quiet for > silence_close_s
            await ws.send_bytes(_pcm(50))  # quiet frame → re-arm (no session opens on silence)
            await ws.send_bytes(_pcm(8000))  # real next meeting → session 1
            await ws.close()
            await asyncio.wait_for(done.wait(), timeout=2)
        assert len(box["sessions"]) == 2
        assert box["sessions"][1].frames == [_pcm(8000)]
        assert server._sessions_idle_closed == 1
    finally:
        server.close()


@pytest.mark.asyncio
async def test_marker_rearms_dormant_connection(tmp_path):
    # A marker is an explicit "new meeting starts here" — it ends dormancy and opens a fresh session
    # even while the room is still noisy (no silence gap needed).
    cfg = _cfg(tmp_path, vad_threshold=1000, vad_hangover_s=0.0, silence_close_s=0.5, transcript_idle_close_s=0.05)
    server, box, done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}")
            await ws.send_bytes(_pcm(8000))  # session 0
            await asyncio.sleep(0.12)
            await ws.send_bytes(_pcm(8000))  # idle-close → dormant
            await ws.send_str(json.dumps({"type": "marker"}))  # marker → re-arm + open session 1
            await ws.close()
            await asyncio.wait_for(done.wait(), timeout=2)
        assert len(box["sessions"]) == 2
        assert box["sessions"][1].markers == 1
    finally:
        server.close()


@pytest.mark.asyncio
async def test_transcript_idle_disabled_keeps_single_session(tmp_path):
    # transcript_idle_close_s=0 disables the idle close: a loud-but-silent session keeps relaying (the
    # pre-fix behavior), proving the knob is opt-out.
    cfg = _cfg(tmp_path, vad_threshold=1000, vad_hangover_s=0.0, silence_close_s=0.5, transcript_idle_close_s=0.0)
    server, box, done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}")
            await ws.send_bytes(_pcm(8000))
            await asyncio.sleep(0.12)
            await ws.send_bytes(_pcm(8000))  # no idle close → same session keeps billing
            await ws.close()
            await asyncio.wait_for(done.wait(), timeout=2)
        assert len(box["sessions"]) == 1
        assert server._sessions_idle_closed == 0
        assert box["sessions"][0].frames == [_pcm(8000), _pcm(8000)]
    finally:
        server.close()


@pytest.mark.asyncio
async def test_gate_disabled_ignores_transcript_idle(tmp_path):
    # With the gate OFF (threshold 0, legacy one-session-per-connection), the transcript-idle machinery
    # is inert regardless of its setting — every frame relays into the single session.
    cfg = _cfg(tmp_path, vad_threshold=0, transcript_idle_close_s=0.05)
    server, box, done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}")
            await ws.send_bytes(_pcm(8000))
            await asyncio.sleep(0.12)
            await ws.send_bytes(_pcm(50))  # gate off → still relayed; no idle close
            await ws.close()
            await asyncio.wait_for(done.wait(), timeout=2)
        assert len(box["sessions"]) == 1
        assert server._sessions_idle_closed == 0
        assert box["sessions"][0].frames == [_pcm(8000), _pcm(50)]
    finally:
        server.close()


@pytest.mark.asyncio
async def test_partial_activity_prevents_idle_close(tmp_path):
    # ASR partial evidence (quiet/far-field speech Speechmatics hears but never commits as a final —
    # the 7/16 real-meeting failure) counts as liveness: with turns frozen at 0 but `last_activity`
    # fresh, the session must NOT idle-close. Once the evidence goes stale, it closes as before.
    cfg = _cfg(tmp_path, vad_threshold=1000, vad_hangover_s=0.0, silence_close_s=0.5, transcript_idle_close_s=0.08)
    server, box, done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}")
            await ws.send_bytes(_pcm(8000))  # opens session 0
            session = await _wait_for_session(box)
            for _ in range(4):  # fresh partial evidence each step, turns never move
                session.last_activity = time.monotonic()
                await asyncio.sleep(0.03)
                await ws.send_bytes(_pcm(8000))
            assert len(box["sessions"]) == 1
            assert server._sessions_idle_closed == 0  # partials kept it open past 4x the idle window
            await asyncio.sleep(0.12)  # evidence goes stale (> transcript_idle_close_s)
            await ws.send_bytes(_pcm(8000))  # loud, but no fresh evidence → idle-close + dormant
            await ws.send_bytes(_pcm(8000))  # still dormant → no reopen
            await ws.close()
            await asyncio.wait_for(done.wait(), timeout=2)
        assert server._sessions_idle_closed == 1
        assert box["sessions"][0].finalized is True
        assert len(box["sessions"]) == 1
    finally:
        server.close()


# ── opening hysteresis + pre-roll (realistic 100 ms frames) ─────────────────
FRAME_100MS = 3200  # bytes of 16 kHz PCM16


def _frame(peak: int) -> bytes:
    """A 100 ms frame whose peak sample is ``peak`` (the rest silence)."""
    return struct.pack("<h", peak) + b"\x00" * (FRAME_100MS - 2)


@pytest.mark.asyncio
async def test_lone_spike_does_not_open_a_session(tmp_path):
    cfg = _cfg(tmp_path, vad_threshold=500, vad_open_speech_s=0.3, vad_open_window_s=1.5)
    server, box, _done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}")
            await ws.send_bytes(_frame(700))  # one click just over threshold
            for _ in range(20):
                await ws.send_bytes(_frame(300))  # room noise below threshold
            await ws.close()
        await asyncio.sleep(0.05)
        assert box["sessions"] == []
    finally:
        server.close()


@pytest.mark.asyncio
async def test_sustained_speech_opens_with_preroll(tmp_path):
    cfg = _cfg(tmp_path, vad_threshold=500, vad_open_speech_s=0.3, vad_open_window_s=1.5)
    server, box, _done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}")
            quiet = _frame(100)
            words = [_frame(4000), _frame(4100), _frame(4200)]
            await ws.send_bytes(quiet)
            for w in words:
                await ws.send_bytes(w)
            session = await _wait_for_session(box)
            await ws.close()
        await asyncio.sleep(0.05)
        # The held-back quiet lead-in and the first two speech frames are pre-rolled, then the
        # frame that confirmed speech: nothing before the open is lost.
        assert session.frames == [quiet, *words]
    finally:
        server.close()


# ── capability negotiation + asr_status ──────────────────────────────────────
@pytest.mark.asyncio
async def test_hello_reply_lists_only_requested_supported_capabilities(tmp_path):
    cfg = _cfg(tmp_path)
    server, _box, _done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}")
            await ws.send_str(json.dumps({"type": "hello", "capabilities": ["asr_status_v1", "future_x"]}))
            hello = await asyncio.wait_for(ws.receive_json(), timeout=2)
            assert hello == {"type": "hello", "capabilities": ["asr_status_v1"]}
            await ws.close()
    finally:
        server.close()


@pytest.mark.asyncio
async def test_asr_status_idle_when_quiet_and_no_session(tmp_path):
    cfg = _cfg(tmp_path, vad_threshold=500)
    server, _box, _done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}")
            await ws.send_str(json.dumps({"type": "hello", "capabilities": ["audio_ack_v1", "asr_status_v1"]}))
            await asyncio.wait_for(ws.receive_json(), timeout=2)  # hello
            await ws.send_bytes(_frame(100))
            got = [await asyncio.wait_for(ws.receive_json(), timeout=2) for _ in range(2)]
            assert {"type": "asr_status", "state": "idle"} in got
            assert any(m.get("type") == "audio_ack" for m in got)
            await ws.close()
    finally:
        server.close()


@pytest.mark.asyncio
async def test_asr_status_error_when_session_has_no_cloud_connection(tmp_path):
    cfg = _cfg(tmp_path)  # gate off: the first frame opens a session
    box = {"sessions": []}
    done = asyncio.Event()

    def factory(_cfg, _source):
        s = FakeSession(done)
        s.cloud_connected = False
        box["sessions"].append(s)
        return s

    server = MeetingServer(cfg, session_factory=factory)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}")
            await ws.send_str(json.dumps({"type": "hello", "capabilities": ["asr_status_v1"]}))
            await asyncio.wait_for(ws.receive_json(), timeout=2)
            await ws.send_bytes(_frame(4000))  # opens the session; status reflects it immediately
            first = await asyncio.wait_for(ws.receive_json(), timeout=2)
            assert first == {"type": "asr_status", "state": "error"}
            await ws.close()
    finally:
        server.close()


# ── empty-transcript cleanup + close reasons ─────────────────────────────────
class _FileSession(FakeSession):
    """A fake that writes a real transcript header, like ActiveSession does on start."""

    def __init__(self, done, path, turns=0, activity=None):
        super().__init__(done)
        self.path = str(path)
        self.turns = turns
        self.last_activity = activity

    async def start(self):
        await super().start()
        with open(self.path, "w") as fh:
            fh.write("# Active listen header\n")


async def _run_one_session(tmp_path, **session_kw):
    cfg = _cfg(tmp_path)
    path = tmp_path / "t.md"
    done = asyncio.Event()
    server = MeetingServer(cfg, session_factory=lambda _c, _s: _FileSession(done, path, **session_kw))
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}")
            await ws.send_bytes(_frame(4000))
            await ws.close()
        await asyncio.wait_for(done.wait(), timeout=2)
        await asyncio.sleep(0.05)
    finally:
        server.close()
    return server, path


@pytest.mark.asyncio
async def test_session_that_heard_nothing_leaves_no_file(tmp_path):
    server, path = await _run_one_session(tmp_path)
    assert not path.exists()
    assert server._health_payload()["empty_transcripts_removed"] == 1


@pytest.mark.asyncio
async def test_session_with_turns_keeps_its_file(tmp_path):
    _server, path = await _run_one_session(tmp_path, turns=1)
    assert path.exists()


@pytest.mark.asyncio
async def test_session_with_asr_activity_keeps_its_file(tmp_path):
    _server, path = await _run_one_session(tmp_path, activity=123.0)
    assert path.exists()


@pytest.mark.asyncio
async def test_close_reason_is_logged(tmp_path, caplog):
    cfg = _cfg(tmp_path)
    server, _box, _done = _server_with_fake(cfg)
    caplog.set_level("INFO", logger="meeting_bridge.server")
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}")
            await ws.send_bytes(_frame(4000))
            await ws.close(code=1000, message=b"client stop")
        await asyncio.sleep(0.1)
    finally:
        server.close()
    closed = [r.getMessage() for r in caplog.records if "connection closed" in r.getMessage()]
    assert closed and "reason=client closed normally (code 1000)" in closed[-1]


@pytest.mark.asyncio
async def test_marker_open_then_speech_is_forwarded_without_hysteresis_loss(tmp_path):
    cfg = _cfg(tmp_path, vad_threshold=500, vad_open_speech_s=0.3, vad_open_window_s=1.5)
    server, box, _done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}")
            await ws.send_str(json.dumps({"type": "marker"}))
            session = await _wait_for_session(box)
            first_words = [_frame(4000), _frame(4100)]
            for w in first_words:
                await ws.send_bytes(w)
            await ws.close()
        await asyncio.sleep(0.05)
        assert session.frames == first_words
    finally:
        server.close()


@pytest.mark.asyncio
async def test_noise_during_dormancy_cannot_bypass_hysteresis_after_rearm(tmp_path):
    cfg = _cfg(
        tmp_path,
        vad_threshold=500,
        vad_open_speech_s=0.3,
        vad_open_window_s=1.5,
        transcript_idle_close_s=0.05,
        silence_close_s=0.2,
        vad_hangover_s=0.0,
    )
    server, box, _done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}")
            for _ in range(3):  # sustained speech opens session 0 (ASR never hears words)
                await ws.send_bytes(_frame(4000))
            await _wait_for_session(box)
            await asyncio.sleep(0.1)
            await ws.send_bytes(_frame(4000))  # idle-close fires → dormant
            for _ in range(3):  # noise at the threshold while dormant arms the gate's run
                await ws.send_bytes(_frame(600))
            await asyncio.sleep(0.25)
            await ws.send_bytes(_frame(100))  # quiet long enough → close (no session) + re-arm
            await ws.send_bytes(_frame(100))
            await ws.send_bytes(_frame(700))  # one click after re-arm must NOT open a session
            await ws.close()
        await asyncio.sleep(0.05)
        assert len(box["sessions"]) == 1
    finally:
        server.close()


@pytest.mark.asyncio
async def test_a_second_hello_never_drops_receipts(tmp_path):
    cfg = _cfg(tmp_path)
    server, _box, _done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}")
            await ws.send_str(json.dumps({"type": "hello", "capabilities": ["audio_ack_v1"]}))
            await asyncio.wait_for(ws.receive_json(), timeout=2)
            await ws.send_str(json.dumps({"type": "hello", "capabilities": ["asr_status_v1"]}))
            await asyncio.wait_for(ws.receive_json(), timeout=2)
            await ws.send_bytes(_frame(100))
            got = [await asyncio.wait_for(ws.receive_json(), timeout=2) for _ in range(2)]
            assert any(m.get("type") == "audio_ack" for m in got)
            await ws.close()
    finally:
        server.close()


@pytest.mark.asyncio
async def test_preroll_advances_the_session_clock_for_markers(tmp_path):
    cfg = _cfg(tmp_path, vad_threshold=500, vad_open_speech_s=0.3, vad_open_window_s=1.5)
    box = {"sessions": []}
    done = asyncio.Event()

    class ClockSession(FakeSession):
        advanced = 0.0

        def advance_clock(self, seconds):
            self.advanced += seconds

    def factory(_cfg, _source):
        s = ClockSession(done)
        box["sessions"].append(s)
        box["session"] = s
        return s

    server = MeetingServer(cfg, session_factory=factory)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}")
            for p in (100, 4000, 4000, 4000):
                await ws.send_bytes(_frame(p))
            session = await _wait_for_session(box)
            await ws.close()
        await asyncio.sleep(0.05)
        # three held frames (quiet + two speech) were pre-rolled: 0.3 s of audio
        assert abs(session.advanced - 0.3) < 1e-6
        assert server._health_payload()["frames"] == 4  # pre-roll counted as forwarded
    finally:
        server.close()


@pytest.mark.asyncio
async def test_stall_is_reported_even_with_the_gate_off(tmp_path):
    cfg = _cfg(tmp_path, asr_stall_s=0.05)  # gate off (legacy)
    server, _box, _done = _server_with_fake(cfg)
    try:
        async with await _client(server) as c:
            ws = await c.ws_connect(f"/meeting/{TOKEN}")
            await ws.send_str(json.dumps({"type": "hello", "capabilities": ["asr_status_v1"]}))
            await asyncio.wait_for(ws.receive_json(), timeout=2)
            await ws.send_bytes(_frame(4000))
            await asyncio.wait_for(ws.receive_json(), timeout=2)  # transcribing (fresh session)
            await asyncio.sleep(2.05)
            await ws.send_bytes(_frame(4000))
            later = await asyncio.wait_for(ws.receive_json(), timeout=2)
            assert later == {"type": "asr_status", "state": "stalled"}
            await ws.close()
    finally:
        server.close()
