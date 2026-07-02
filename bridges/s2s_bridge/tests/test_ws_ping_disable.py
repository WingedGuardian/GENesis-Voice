"""Tests for the WS-server ping disable.

The Voice PE's minimal WebSocket stack rejects server PING control frames — it closes
the connection with a `1002 (protocol error) invalid opcode`, killing the conversation
(observed live: a long session died this way; the sibling ambient bridge documents and
disables the same on the same device). pipecat 1.3.0's WebsocketServerParams exposes no
ping control and calls `websocket_serve(handler, host, port)` with the websockets default
`ping_interval=20`, so the bridge wraps that module-level callable to force pings OFF.
These tests pin that behavior against pipecat's real module attribute.
"""
import pipecat.transports.websocket.server as pcws

from app.websocket_handler import _disable_ws_pings


def test_disable_ws_pings_forces_none(monkeypatch):
    """After _disable_ws_pings(), the module's websocket_serve forwards
    ping_interval=None and ping_timeout=None to the underlying serve, and returns
    its result unchanged (it's used as an async context manager)."""
    captured = {}

    def fake_serve(handler, host, port, **kwargs):
        captured.update(kwargs)
        return "server-obj"

    monkeypatch.setattr(pcws, "websocket_serve", fake_serve)

    _disable_ws_pings()
    result = pcws.websocket_serve("handler", "0.0.0.0", 8080)

    assert result == "server-obj"
    assert captured["ping_interval"] is None
    assert captured["ping_timeout"] is None


def test_forces_none_even_if_caller_sets_them(monkeypatch):
    """The device CANNOT handle pings — an explicit caller value must not re-enable
    them (so this is a hard force, not a setdefault)."""
    captured = {}

    def fake_serve(handler, host, port, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(pcws, "websocket_serve", fake_serve)

    _disable_ws_pings()
    pcws.websocket_serve("handler", "0.0.0.0", 8080, ping_interval=20, ping_timeout=20)

    assert captured["ping_interval"] is None
    assert captured["ping_timeout"] is None


def test_idempotent(monkeypatch):
    """Calling it twice does not double-wrap; behavior is unchanged."""
    calls = {"n": 0, "kwargs": None}

    def fake_serve(handler, host, port, **kwargs):
        calls["n"] += 1
        calls["kwargs"] = kwargs

    monkeypatch.setattr(pcws, "websocket_serve", fake_serve)

    _disable_ws_pings()
    first_wrapper = pcws.websocket_serve
    _disable_ws_pings()  # second call must be a no-op
    assert pcws.websocket_serve is first_wrapper  # not re-wrapped

    pcws.websocket_serve("handler", "0.0.0.0", 8080)
    assert calls["n"] == 1  # called through exactly once, not nested
    assert calls["kwargs"]["ping_interval"] is None


def test_preserves_positional_args(monkeypatch):
    """handler/host/port pass through unchanged."""
    seen = {}

    def fake_serve(handler, host, port, **kwargs):
        seen["args"] = (handler, host, port)

    monkeypatch.setattr(pcws, "websocket_serve", fake_serve)

    _disable_ws_pings()
    pcws.websocket_serve("H", "1.2.3.4", 9999)

    assert seen["args"] == ("H", "1.2.3.4", 9999)
