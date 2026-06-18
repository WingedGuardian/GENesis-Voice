"""Tests for the disconnect_client guard in create_disconnect_tool_handler.

The model once hung up mid-conversation (`disconnect_client`) after a spurious
echo false-interrupt, with no real user turn. The guard refuses a disconnect
when its ``should_honor`` predicate is False, so the session stays open (the
idle timeout ends it cleanly) instead of yanking the device offline. These
tests pin: refuse vs proceed, and backward-compat when no guard is supplied.
"""
import asyncio

from app.disconnect_tool import create_disconnect_tool_handler


class _FakeParams:
    """Stand-in for pipecat FunctionCallParams."""

    def __init__(self, arguments):
        self.function_name = "disconnect_client"
        self.arguments = arguments
        self.results: list[str] = []

    async def result_callback(self, msg):
        self.results.append(msg)


def _run(handler, params):
    asyncio.run(handler(params))


def test_guard_refuses_when_predicate_false():
    """should_honor=False → the disconnect is refused; the model is told we're
    staying connected, and the close path is never taken."""
    handler = create_disconnect_tool_handler(None, should_honor=lambda: False)
    params = _FakeParams({"reason": "conversation_ended"})
    _run(handler, params)
    assert len(params.results) == 1
    assert "staying connected" in params.results[0].lower()
    assert "disconnected" not in params.results[0].lower()


def test_guard_allows_when_predicate_true():
    """should_honor=True → proceeds past the guard into the disconnect path."""
    handler = create_disconnect_tool_handler(None, should_honor=lambda: True)
    params = _FakeParams({"reason": "user_requested_stop"})
    _run(handler, params)
    assert any("disconnect" in r.lower() for r in params.results)
    assert not any("staying connected" in r.lower() for r in params.results)


def test_no_guard_is_backward_compatible():
    """No should_honor (legacy call) → proceeds, never refuses."""
    handler = create_disconnect_tool_handler(None)
    params = _FakeParams({"reason": "user_requested_stop"})
    _run(handler, params)
    assert any("disconnect" in r.lower() for r in params.results)
