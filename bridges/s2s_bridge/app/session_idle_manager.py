"""Session idle manager — close the client WS only on genuine idle.

The Voice PE firmware used a naive "20 s since the last bot audio" timer to
hang up. That fires mid-conversation: while the user is still talking, while a
tool call is in flight, or while the LLM is composing a response (none of
which produce bot audio yet). This processor replaces that with real
activity tracking on the output side of the pipeline:

* user speech (``UserStartedSpeakingFrame`` / ``UserStoppedSpeakingFrame``),
* bot speech (``BotStartedSpeakingFrame`` / ``BotStoppedSpeakingFrame``),
* a pending LLM response (``LLMFullResponseStartFrame`` / ...End),
* pending tool calls (``FunctionCallInProgressFrame`` until a matching
  ``FunctionCallResultFrame`` / ``FunctionCallCancelFrame``).

It closes the client websocket only after ``idle_timeout`` seconds elapse with
none of those active. The watchdog loop is intentionally thin: the idle
decision lives in ``_check_idle_once`` so it can be unit-tested without real
sleeps.
"""
import asyncio
import logging
import time

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    FunctionCallCancelFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.ws_control import send_disconnect_then_close

logger = logging.getLogger(__name__)


class SessionIdleManager(FrameProcessor):
    """Track conversation activity and close the client WS on genuine idle.

    Pure observer: every frame is pushed onward unchanged.
    """

    def __init__(self, transport, idle_timeout: float, **kwargs):
        super().__init__(**kwargs)
        self._transport = transport
        self._idle_timeout = idle_timeout
        self._user_speaking = False
        self._bot_speaking = False
        self._response_pending = False
        self._pending_tools: set = set()
        self._last_activity = time.monotonic()
        # Turn-tracking for the disconnect guard: a real user turn (non-empty
        # transcript) must follow the bot's last response for disconnect_client
        # to be honoured. Reset per session in arm().
        self._last_user_transcript_t = 0.0
        self._last_bot_response_t = 0.0
        self._watchdog: asyncio.Task | None = None
        self._closing = False
        self._armed = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, UserStartedSpeakingFrame):
            self._user_speaking = True
            self._touch()
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._user_speaking = False
            self._touch()
        elif isinstance(frame, TranscriptionFrame):
            # A completed user transcript = a real user turn. VAD alone is
            # unreliable here (the bot's own echo trips it), so the disconnect
            # guard keys off when the user last actually said something.
            if frame.text and frame.text.strip():
                self._last_user_transcript_t = time.monotonic()
            self._touch()
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            self._last_bot_response_t = time.monotonic()
            self._touch()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            self._touch()
        elif isinstance(frame, LLMFullResponseStartFrame):
            self._response_pending = True
            self._touch()
        elif isinstance(frame, LLMFullResponseEndFrame):
            self._response_pending = False
            self._touch()
        elif isinstance(frame, FunctionCallInProgressFrame):
            self._pending_tools.add(frame.tool_call_id)
            self._touch()
        elif isinstance(frame, (FunctionCallResultFrame, FunctionCallCancelFrame)):
            self._pending_tools.discard(frame.tool_call_id)
            self._touch()

        await self.push_frame(frame, direction)

    def _touch(self) -> None:
        """Record the current time as the last activity."""
        self._last_activity = time.monotonic()

    def _is_active(self) -> bool:
        """True if any user/bot speech, pending response, or pending tool."""
        return (
            self._user_speaking
            or self._bot_speaking
            or self._response_pending
            or bool(self._pending_tools)
        )

    def user_turn_since_last_bot_response(self) -> bool:
        """True if a real user turn (non-empty transcript) arrived after the
        bot's most recent spoken response began.

        The disconnect guard uses this: the model should only end the call in
        response to the user actually saying something — not after a spurious
        echo false-interrupt that left it in an empty-turn state.
        """
        return self._last_user_transcript_t > self._last_bot_response_t

    def arm(self) -> None:
        """Reset to a fresh idle baseline and start the watchdog.

        Called when a client connects. Clears any state carried over from a
        previous session and starts the idle clock from now.
        """
        self._user_speaking = False
        self._bot_speaking = False
        self._response_pending = False
        self._pending_tools = set()
        self._last_user_transcript_t = 0.0
        self._last_bot_response_t = 0.0
        self._closing = False
        self._armed = True
        self._touch()
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.create_task(self._watchdog_loop())
        logger.info("⏱️ Session idle manager armed (timeout=%.0fs)", self._idle_timeout)

    def disarm(self) -> None:
        """Stop tracking idle (client disconnected) and cancel the watchdog."""
        self._armed = False
        if self._watchdog is not None and not self._watchdog.done():
            self._watchdog.cancel()
        self._watchdog = None

    async def _watchdog_loop(self) -> None:
        """Thin loop — the decision lives in ``_check_idle_once``."""
        while True:
            await asyncio.sleep(1.0)
            if not self._armed:
                return
            await self._check_idle_once()

    async def _check_idle_once(self) -> None:
        """One idle evaluation. Closes the WS if armed, inactive, and the
        idle timeout has elapsed since the last activity. Idempotent: the
        ``_closing`` guard makes a second call a no-op."""
        if not self._armed:
            return
        if self._is_active():
            self._touch()
            return
        if time.monotonic() - self._last_activity >= self._idle_timeout:
            await self._close_idle()

    async def _close_idle(self) -> None:
        """Signal the device the session is over, then close the client WS once.

        The bare ``ws.close()`` this used to do made the firmware reconnect into
        a torn-down session (spinning forever); ``send_disconnect_then_close``
        first tells the device to go idle so it stops cleanly instead.
        """
        if self._closing:
            return
        self._closing = True
        ws = self._transport.input()._websocket
        if ws is not None:
            logger.info(
                "💤 Session idle for %.0fs (no speech, response, or pending tool) "
                "— signalling device + closing client WS",
                self._idle_timeout,
            )
            await send_disconnect_then_close(ws, reason="idle")
