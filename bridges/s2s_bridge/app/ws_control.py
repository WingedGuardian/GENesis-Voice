"""Shared WebSocket control helpers for the S2S voice bridge.

Any server-initiated close of the client socket must FIRST tell the Voice PE
firmware the session is over, then close. The firmware reconnects on a *bare*
socket close (it reads the dropped connection as transient and retries), but
transitions cleanly to idle — no reconnect — when it first receives a
``{"type":"disconnect"}`` text frame. Closing without that frame orphans the
device: it reconnects into a torn-down session and spins forever.

This helper is the single place that pairing lives, used by both the idle
manager and the model-invoked disconnect tool.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Brief pause so the device receives and processes the disconnect text frame
# before the close frame arrives — both travel on the same socket, and the
# firmware must run its disconnect handler before the connection drops.
_DISCONNECT_GRACE_S = 0.1


async def send_disconnect_then_close(ws: Any, reason: str) -> None:
    """Tell the device the session is ending, then close its websocket.

    Sends ``{"type":"disconnect","reason":<reason>}`` — which the firmware reads
    as an explicit disconnect (go idle, do not reconnect) — waits briefly for the
    device to process it, then closes. The close runs in ``finally`` so it happens
    even if the send raises or the task is cancelled mid-send (``CancelledError``
    is a ``BaseException`` and skips the ``except``): the socket is never left open
    for the firmware to spin-reconnect against.
    """
    if ws is None:
        return
    try:
        await ws.send(json.dumps({"type": "disconnect", "reason": reason}))
        await asyncio.sleep(_DISCONNECT_GRACE_S)
    except Exception as exc:  # a failed send must not prevent the close
        logger.warning("Could not send disconnect frame (reason=%s): %s", reason, exc)
    finally:
        await ws.close()
