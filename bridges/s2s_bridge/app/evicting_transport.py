"""Websocket server transport where a new client EVICTS the old one.

pipecat 1.4.0 (PR #4774) made the single-client websocket transport reject a new
client with close code 1013 while the previous socket still looks open. That is
wrong for this device: server pings are disabled (the Voice PE rejects PING frames,
see ``websocket_handler._disable_ws_pings``), so a socket left half-open by a device
reboot or a Wi-Fi drop is never detected, and the device's reconnects would be
refused until the idle manager closes the stale one. The firmware gives up retrying
long before that.

This restores pipecat 1.3.0's behaviour: close the old socket and take the new one.

ORDERING is the whole difficulty. The old connection's handler tears down in its own
task after its receive loop ends, and that teardown calls
``output.set_client_connection(None)``, which clears (and closes) WHATEVER the output
transport currently holds. ``old.close()`` returning says nothing about when that
task gets there. So the eviction waits for the old handler to FINISH, bounded,
before the new client is installed; otherwise a late teardown closes the device's
fresh connection (pipecat server.py ``_on_client_disconnected``).
"""

import asyncio

from loguru import logger
from pipecat.transports.websocket.server import (
    SingleClientWebsocketServerInputTransport,
    WebsocketServerTransport,
)


#: How long an evicted socket gets to answer the close handshake.
_EVICT_CLOSE_TIMEOUT_S = 1.0
#: How long a reconnect waits for the evicted connection's teardown to finish. The
#: teardown is local work (clearing the output, the bridge's disconnect handler);
#: past this, something is stuck, and a device left without service is worse.
_EVICT_TEARDOWN_TIMEOUT_S = 5.0


class EvictingWebsocketServerInputTransport(SingleClientWebsocketServerInputTransport):
    """Input transport whose new connection replaces the current one."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Set when a connection's handler has fully finished, teardown included.
        self._handler_done: dict[int, asyncio.Event] = {}

    async def _client_handler(self, websocket):
        old = self._websocket
        if old is not None and old is not websocket:
            logger.warning(
                f"Replacing client {getattr(old, 'remote_address', '?')} with "
                f"{getattr(websocket, 'remote_address', '?')} (new connection evicts old)"
            )
            # Cleared first so the parent's "already connected" check passes.
            self._websocket = None
            # A half-open peer never answers the close; the library default would
            # hold the new client for 10s. Scoped to the evicted socket only.
            old.close_timeout = _EVICT_CLOSE_TIMEOUT_S
            await old.close()
            # Wait for the old handler's teardown, which clears the output socket.
            done = self._handler_done.get(id(old))
            if done is not None:
                try:
                    await asyncio.wait_for(done.wait(), timeout=_EVICT_TEARDOWN_TIMEOUT_S)
                except TimeoutError:
                    logger.warning(
                        "Evicted client's teardown did not finish in %.0fs; installing the "
                        "new client anyway", _EVICT_TEARDOWN_TIMEOUT_S,
                    )
        done = asyncio.Event()
        self._handler_done[id(websocket)] = done
        try:
            await super()._client_handler(websocket)
        finally:
            done.set()
            self._handler_done.pop(id(websocket), None)


class EvictingWebsocketServerTransport(WebsocketServerTransport):
    """``WebsocketServerTransport`` using :class:`EvictingWebsocketServerInputTransport`.

    Subclasses the name the bridge type-checks against, so ``isinstance`` checks in
    ``disconnect_tool`` keep passing.
    """

    def input(self) -> EvictingWebsocketServerInputTransport:
        if not self._input:
            self._input = EvictingWebsocketServerInputTransport(
                self, self._host, self._port, self._params, self._callbacks, name=self._input_name
            )
        return self._input
