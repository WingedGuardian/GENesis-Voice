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
import json

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    FunctionCallCancelFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from app.session_idle_manager import SessionIdleManager


class _FakeWebSocket:
    """Async stand-in for the transport input websocket.

    Records send/close calls in order so tests can assert the idle close first
    tells the device ``{"type":"disconnect"}`` and only THEN closes the socket.
    """

    def __init__(self):
        self.close_count = 0
        self.calls: list[str] = []   # ordered record of "send"/"close"
        self.sent: list[str] = []    # payloads passed to send()

    async def send(self, message):
        self.sent.append(message)
        self.calls.append("send")

    async def close(self):
        self.close_count += 1
        self.calls.append("close")


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
    # The disconnect-grace in ws_control awaits a real asyncio.sleep, which would
    # hang under the frozen monotonic clock above (asyncio's loop clock IS
    # time.monotonic). Make it instant — we verify the send-before-close ORDER,
    # not the wall-clock grace.
    monkeypatch.setattr("app.ws_control.asyncio.sleep", _noop_sleep)

    ws = _FakeWebSocket()
    transport = _FakeTransport(ws)
    mgr = SessionIdleManager(transport=transport, idle_timeout=idle_timeout)

    pushed: list = []

    async def fake_push(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append((frame, direction))

    mgr.push_frame = fake_push
    return mgr, ws, pushed, clock


async def _noop_sleep(*_a, **_k):
    return None


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


def test_idle_close_signals_device_before_closing(monkeypatch):
    """On idle, the manager must first send a ``{"type":"disconnect"}`` control
    frame and only THEN close the socket.

    The Voice PE firmware reconnects on a bare socket close but goes cleanly to
    idle (no reconnect) when it first receives that frame. Closing FIRST orphans
    the device — it reconnects into a torn-down session and spins forever. The
    send-before-close ORDER is the contract this test pins.
    """
    mgr, ws, _pushed, clock = _make_manager(monkeypatch, idle_timeout=45.0)
    _run(_arm_quiet(mgr))

    clock["t"] = 1000.0 + 46.0
    _run(mgr._check_idle_once())

    assert ws.sent, "expected a disconnect control frame to be sent before close"
    payload = json.loads(ws.sent[0])
    assert payload.get("type") == "disconnect"
    assert ws.calls == ["send", "close"]  # signal device first, THEN close
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


# ── disconnect guard: user_turn_since_last_bot_response ──────────────────────

def test_disconnect_guard_true_when_user_speaks_after_bot(monkeypatch):
    """User transcript AFTER the bot's last response → a real end-of-call."""
    mgr, _ws, _pushed, clock = _make_manager(monkeypatch)
    _feed(mgr, BotStartedSpeakingFrame(), direction=FrameDirection.UPSTREAM)
    clock["t"] = 1005.0
    _feed(mgr, TranscriptionFrame("goodbye", "", "2026-01-01T00:00:00Z"))
    assert mgr.user_turn_since_last_bot_response() is True


def test_disconnect_guard_false_for_echo_after_bot(monkeypatch):
    """The echo case: user turn, bot responds, then NO new user turn (just an
    echo interrupt). The model must NOT be allowed to hang up."""
    mgr, _ws, _pushed, clock = _make_manager(monkeypatch)
    _feed(mgr, TranscriptionFrame("how are you tonight", "", "t"))  # user @1000
    clock["t"] = 1002.0
    _feed(mgr, BotStartedSpeakingFrame(), direction=FrameDirection.UPSTREAM)  # bot @1002
    clock["t"] = 1004.0  # echo interrupt, no transcript
    assert mgr.user_turn_since_last_bot_response() is False


def test_disconnect_guard_ignores_empty_transcript(monkeypatch):
    """An empty/whitespace transcript (echo garbage) is not a real user turn."""
    mgr, _ws, _pushed, clock = _make_manager(monkeypatch)
    _feed(mgr, BotStartedSpeakingFrame(), direction=FrameDirection.UPSTREAM)
    clock["t"] = 1005.0
    _feed(mgr, TranscriptionFrame("   ", "", "t"))
    assert mgr.user_turn_since_last_bot_response() is False


def test_disconnect_guard_false_on_fresh_session(monkeypatch):
    """Before anyone speaks (both timers reset by arm), disconnect is refused."""
    mgr, _ws, _pushed, _clock = _make_manager(monkeypatch)
    _run(_arm_quiet(mgr))
    assert mgr.user_turn_since_last_bot_response() is False


# ── stuck-pending guard: bound a response/tool that never resolves ───────────

def test_stuck_pending_response_forces_close(monkeypatch):
    """An LLM response that goes 'pending' and never ends (OpenAI stalls with no
    response.done → no LLMFullResponseEndFrame) is force-closed after
    _max_pending_active so the device recovers instead of hanging."""
    mgr, ws, _pushed, clock = _make_manager(monkeypatch, idle_timeout=45.0)
    mgr._max_pending_active = 60.0
    _run(_arm_quiet(mgr))

    _feed(mgr, LLMFullResponseStartFrame())  # pending @1000, no End ever arrives
    # Past the cap. The response is 'active', so the normal idle path can't fire;
    # only the stuck-pending guard can close it.
    clock["t"] = 1000.0 + 61.0
    _run(mgr._check_idle_once())

    assert ws.close_count == 1
    assert ws.calls == ["send", "close"]          # signal device first, THEN close
    sent = json.loads(ws.sent[0])
    assert sent["type"] == "disconnect"
    assert sent["reason"] == "stuck_pending"


def test_stuck_pending_tool_forces_close(monkeypatch):
    """A tool call whose Result/Cancel never arrives is bounded the same way."""
    mgr, ws, _pushed, clock = _make_manager(monkeypatch, idle_timeout=45.0)
    mgr._max_pending_active = 60.0
    _run(_arm_quiet(mgr))

    _feed(mgr, FunctionCallInProgressFrame(
        function_name="recall", tool_call_id="stuck", arguments={}))
    clock["t"] = 1000.0 + 61.0
    _run(mgr._check_idle_once())

    assert ws.close_count == 1
    assert json.loads(ws.sent[0])["reason"] == "stuck_pending"


def test_pending_under_cap_keeps_alive(monkeypatch):
    """Below the cap, a pending response is normal in-flight work — not closed."""
    mgr, ws, _pushed, clock = _make_manager(monkeypatch, idle_timeout=45.0)
    mgr._max_pending_active = 60.0
    _run(_arm_quiet(mgr))

    _feed(mgr, LLMFullResponseStartFrame())
    clock["t"] = 1000.0 + 59.0  # under the cap
    _run(mgr._check_idle_once())
    assert ws.close_count == 0


def test_pending_resolved_resets_stuck_clock(monkeypatch):
    """When a pending response ends, the stuck clock clears — a later response
    gets a FRESH budget, so prior in-flight time never carries over."""
    mgr, ws, _pushed, clock = _make_manager(monkeypatch, idle_timeout=45.0)
    mgr._max_pending_active = 60.0
    _run(_arm_quiet(mgr))

    # First response runs 50s then ENDS cleanly.
    _feed(mgr, LLMFullResponseStartFrame())          # pending @1000
    clock["t"] = 1050.0
    _feed(mgr, LLMFullResponseEndFrame())            # clears pending @1050
    assert mgr._pending_active_since is None

    # A second response starts — its budget is fresh from 1050, not 1000.
    _feed(mgr, LLMFullResponseStartFrame())          # pending @1050
    clock["t"] = 1050.0 + 59.0                        # 59s into the 2nd → under cap
    _run(mgr._check_idle_once())
    assert ws.close_count == 0


def test_stuck_guard_ignores_speaking_only(monkeypatch):
    """The guard targets ONLY response/tool pending — a long user/bot SPEAKING
    stretch (no pending flag) is never force-closed, even far past the cap."""
    mgr, ws, _pushed, clock = _make_manager(monkeypatch, idle_timeout=45.0)
    mgr._max_pending_active = 60.0
    _run(_arm_quiet(mgr))

    _feed(mgr, UserStartedSpeakingFrame())           # active, but NOT pending
    clock["t"] = 1000.0 + 300.0                       # far past the cap
    _run(mgr._check_idle_once())
    assert ws.close_count == 0


def test_max_pending_active_default_is_180(monkeypatch):
    """The shipped default cap is 180s (well above the ~60s legit tool max)."""
    mgr, _ws, _pushed, _clock = _make_manager(monkeypatch)
    assert mgr._max_pending_active == 180.0


def test_pending_cleared_while_bot_speaking_resets_stuck_clock(monkeypatch):
    """Concurrent case: a response ENDS while the bot is still speaking. The
    stuck clock must clear (pending is gone) even though _is_active() stays True
    — so the guard must NOT fire on the lingering bot-speech."""
    mgr, ws, _pushed, clock = _make_manager(monkeypatch, idle_timeout=45.0)
    mgr._max_pending_active = 60.0
    _run(_arm_quiet(mgr))

    _feed(mgr, LLMFullResponseStartFrame())                              # pending @1000
    _feed(mgr, BotStartedSpeakingFrame(), direction=FrameDirection.UPSTREAM)
    clock["t"] = 1050.0
    _feed(mgr, LLMFullResponseEndFrame())   # pending ends; bot still speaking
    assert mgr._pending_active_since is None  # cleared despite _is_active() True
    assert mgr._bot_speaking is True

    # Past the cap, but no pending flag is set → speaking alone never force-closes.
    clock["t"] = 1050.0 + 61.0
    _run(mgr._check_idle_once())
    assert ws.close_count == 0
