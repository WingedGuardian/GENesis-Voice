"""Tests for ws_control.send_disconnect_then_close.

Server-initiated closes of the client socket must FIRST send the device a
``{"type":"disconnect"}`` text frame and only THEN close — otherwise the Voice
PE firmware reads the bare close as a dropped connection and reconnects into a
torn-down session (spinning forever). These tests pin: the order, the payload
shape (which the firmware string-matches), None-safety, and that a failed send
never prevents the close (no socket leak).
"""
import asyncio
import json

import pytest

from app import ws_control


class _WS:
    """Records send/close ordering; ``send`` can be made to raise."""

    def __init__(self, send_exc=None):
        self.calls: list[str] = []
        self.sent: list[str] = []
        self._send_exc = send_exc

    async def send(self, message):
        if self._send_exc is not None:
            raise self._send_exc
        self.sent.append(message)
        self.calls.append("send")

    async def close(self):
        self.calls.append("close")


async def _noop_sleep(*_a, **_k):
    return None


def _patch_sleep(monkeypatch):
    monkeypatch.setattr("app.ws_control.asyncio.sleep", _noop_sleep)


def test_sends_disconnect_then_closes(monkeypatch):
    _patch_sleep(monkeypatch)
    ws = _WS()
    asyncio.run(ws_control.send_disconnect_then_close(ws, reason="idle"))
    assert ws.calls == ["send", "close"]  # signal device first, THEN close
    assert json.loads(ws.sent[0]) == {"type": "disconnect", "reason": "idle"}


def test_payload_matches_firmware_string_match(monkeypatch):
    """The firmware string-matches ``"type": "disconnect"`` (with the space that
    json.dumps emits). Guard that the wire bytes actually contain it."""
    _patch_sleep(monkeypatch)
    ws = _WS()
    asyncio.run(ws_control.send_disconnect_then_close(ws, reason="idle"))
    assert '"type": "disconnect"' in ws.sent[0]


def test_none_ws_is_noop():
    # No websocket (already gone) — must not raise.
    asyncio.run(ws_control.send_disconnect_then_close(None, reason="x"))


def test_send_failure_still_closes(monkeypatch):
    """If the disconnect send fails (socket half-dead), the close must still
    happen so the connection never leaks."""
    _patch_sleep(monkeypatch)
    ws = _WS(send_exc=RuntimeError("socket gone"))
    asyncio.run(ws_control.send_disconnect_then_close(ws, reason="idle"))
    assert ws.calls == ["close"]  # send raised, close still ran


def test_cancelled_send_still_closes(monkeypatch):
    """If the send is cancelled, the close must still run and the cancellation
    must propagate. ``CancelledError`` is a ``BaseException`` so it skips the
    ``except Exception`` — only the ``finally`` close keeps the socket from being
    left open for the firmware to spin-reconnect against."""
    _patch_sleep(monkeypatch)
    ws = _WS(send_exc=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ws_control.send_disconnect_then_close(ws, reason="idle"))
    assert ws.calls == ["close"]  # close ran despite cancellation
