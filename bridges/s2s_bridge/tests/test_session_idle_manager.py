"""Tests for SessionIdleManager — closes the client WS only on genuine idle.

The firmware used a naive "20s since the last bot audio" timer to hang up,
which fired mid-conversation (e.g. while the user was still talking, or while
a tool call was in flight). SessionIdleManager instead tracks real activity:
user speech, bot speech, a pending LLM response, and pending tool calls. It
closes the socket only after ``idle_timeout`` seconds with none of those
active.

These tests exercise the STATE LOGIC and the close decision directly via
``_check_idle_once()`` — no real asyncio sleep loop is involved, so they are
fast and deterministic. A mutable monotonic clock is monkeypatched.
"""
import asyncio

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    FunctionCallCancelFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from app.session_idle_manager import SessionIdleManager


class _FakeWebSocket:
    """Async-closable stand-in for the transport input websocket."""

    def __init__(self):
        self.close_count = 0

    async def close(self):
        self.close_count += 1


class _FakeInput:
    def __init__(self, websocket):
        self._websocket = websocket


class _FakeTransport:
    """Object whose ``.input()._websocket`` is a fake closable websocket."""

    def __init__(self, websocket):
        self._input = _FakeInput(websocket)

    def input(self):
        return self._input


def _make_manager(monkeypatch, idle_timeout=45.0):
    """Build a SessionIdleManager with a fake transport, captured push_frame,
    and a mutable monotonic clock. Returns (mgr, ws, pushed, clock)."""
    clock = {"t": 1000.0}
    monkeypatch.setattr("app.session_idle_manager.time.monotonic", lambda: clock["t"])

    ws = _FakeWebSocket()
    transport = _FakeTransport(ws)
    mgr = SessionIdleManager(transport=transport, idle_timeout=idle_timeout)

    pushed: list = []

    async def fake_push(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append((frame, direction))

    mgr.push_frame = fake_push
    return mgr, ws, pushed, clock


def _run(coro):
    asyncio.run(coro)


def _feed(mgr, frame, direction=FrameDirection.DOWNSTREAM):
    _run(mgr.process_frame(frame, direction))


async def _arm_quiet(mgr):
    """Arm (fresh idle baseline, ``_armed=True``) but cancel the watchdog task
    so tests drive ``_check_idle_once`` directly with no real sleeps.

    Must run inside a running loop because ``arm()`` schedules the watchdog
    task. We cancel only that task — we do NOT ``disarm()`` (that would set
    ``_armed=False`` and make ``_check_idle_once`` a no-op).
    """
    mgr.arm()
    watchdog = mgr._watchdog
    if watchdog is not None:
        watchdog.cancel()
        mgr._watchdog = None
        try:
            await watchdog
        except asyncio.CancelledError:
            pass


def test_user_speech_marks_active(monkeypatch):
    """UserStartedSpeaking → active; UserStopped → not active."""
    mgr, _ws, _pushed, _clock = _make_manager(monkeypatch)
    assert mgr._is_active() is False
    _feed(mgr, UserStartedSpeakingFrame())
    assert mgr._is_active() is True
    _feed(mgr, UserStoppedSpeakingFrame())
    assert mgr._is_active() is False


def test_pending_tool_marks_active(monkeypatch):
    """A tool call in progress keeps the session active until it resolves."""
    mgr, _ws, _pushed, _clock = _make_manager(monkeypatch)
    _feed(mgr, FunctionCallInProgressFrame(
        function_name="recall", tool_call_id="x", arguments={}))
    assert mgr._is_active() is True
    _feed(mgr, FunctionCallResultFrame(
        function_name="recall", tool_call_id="x", arguments={}, result="ok"))
    assert mgr._is_active() is False


def test_pending_tool_cancel_clears_active(monkeypatch):
    """A cancelled tool call also clears the pending set."""
    mgr, _ws, _pushed, _clock = _make_manager(monkeypatch)
    _feed(mgr, FunctionCallInProgressFrame(
        function_name="recall", tool_call_id="y", arguments={}))
    assert mgr._is_active() is True
    _feed(mgr, FunctionCallCancelFrame(function_name="recall", tool_call_id="y"))
    assert mgr._is_active() is False


def test_response_pending_marks_active(monkeypatch):
    """LLMFullResponseStart → active until End."""
    mgr, _ws, _pushed, _clock = _make_manager(monkeypatch)
    _feed(mgr, LLMFullResponseStartFrame())
    assert mgr._is_active() is True
    _feed(mgr, LLMFullResponseEndFrame())
    assert mgr._is_active() is False


def test_bot_speech_marks_active(monkeypatch):
    """BotStartedSpeaking → active until BotStopped."""
    mgr, _ws, _pushed, _clock = _make_manager(monkeypatch)
    _feed(mgr, BotStartedSpeakingFrame(), direction=FrameDirection.UPSTREAM)
    assert mgr._is_active() is True
    _feed(mgr, BotStoppedSpeakingFrame(), direction=FrameDirection.UPSTREAM)
    assert mgr._is_active() is False


def test_idle_past_timeout_closes_once(monkeypatch):
    """With no active state and the timeout elapsed since last activity, the
    idle check closes the client websocket exactly once."""
    mgr, ws, _pushed, clock = _make_manager(monkeypatch, idle_timeout=45.0)
    _run(_arm_quiet(mgr))  # fresh baseline at t=1000; watchdog cancelled

    # No activity; advance past the timeout.
    clock["t"] = 1000.0 + 46.0
    _run(mgr._check_idle_once())
    assert ws.close_count == 1


def test_closing_guard_prevents_double_close(monkeypatch):
    """Once closing, a second idle check must NOT call ws.close() again."""
    mgr, ws, _pushed, clock = _make_manager(monkeypatch, idle_timeout=45.0)
    _run(_arm_quiet(mgr))

    clock["t"] = 1100.0
    _run(mgr._check_idle_once())
    assert ws.close_count == 1
    # A second check while already closing must be a no-op.
    clock["t"] = 1200.0
    _run(mgr._check_idle_once())
    assert ws.close_count == 1


def test_active_state_keeps_alive_past_timeout(monkeypatch):
    """If any activity is in flight, the idle check does NOT close even after
    the timeout has elapsed."""
    mgr, ws, _pushed, clock = _make_manager(monkeypatch, idle_timeout=45.0)
    _run(_arm_quiet(mgr))

    # User is still speaking — active.
    _feed(mgr, UserStartedSpeakingFrame())
    clock["t"] = 1000.0 + 100.0  # way past timeout
    _run(mgr._check_idle_once())
    assert ws.close_count == 0


def test_activity_refreshes_idle_window(monkeypatch):
    """A frame touches last-activity, so the idle window restarts from it."""
    mgr, ws, _pushed, clock = _make_manager(monkeypatch, idle_timeout=45.0)
    _run(_arm_quiet(mgr))

    # 40s in, a user turn completes (touches activity).
    clock["t"] = 1040.0
    _feed(mgr, UserStartedSpeakingFrame())
    _feed(mgr, UserStoppedSpeakingFrame())  # now idle again, but activity touched

    # 44s after that touch — still inside the window → no close.
    clock["t"] = 1040.0 + 44.0
    _run(mgr._check_idle_once())
    assert ws.close_count == 0

    # 46s after the touch — past the window, idle → close.
    clock["t"] = 1040.0 + 46.0
    _run(mgr._check_idle_once())
    assert ws.close_count == 1


def test_frames_are_passed_through(monkeypatch):
    """SessionIdleManager is a pure observer — every frame is pushed onward."""
    mgr, _ws, pushed, _clock = _make_manager(monkeypatch)
    f1 = UserStartedSpeakingFrame()
    f2 = LLMFullResponseStartFrame()
    _feed(mgr, f1)
    _feed(mgr, f2)
    assert [p[0] for p in pushed] == [f1, f2]


def test_not_armed_does_not_close(monkeypatch):
    """If never armed (no active session), an idle check is a no-op."""
    mgr, ws, _pushed, clock = _make_manager(monkeypatch, idle_timeout=45.0)
    # Not armed.
    clock["t"] = 2000.0
    _run(mgr._check_idle_once())
    assert ws.close_count == 0
