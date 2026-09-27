"""Websocket server transport where a new client EVICTS the old one.

pipecat 1.4.0 (PR #4774) made the single-client websocket transport reject a new
client with close code 1013 while the previous socket still looks open. That is
wrong for this device: server pings are disabled (the Voice PE rejects PING frames,
see ``websocket_handler._disable_ws_pings``), so a socket left half-open by a device
reboot or a Wi-Fi drop is never detected, and the device's reconnects would be
refused until the idle manager closes the stale one. The firmware gives up retrying
long before that.

This restores pipecat 1.3.0's behaviour: close the old socket and take the new one.
The close is AWAITED, so the old socket's full disconnect (including the bridge's own
teardown) completes before the new client connects, and the reconnect starts a fresh
session. Awaiting it is load-bearing: a detached close leaves the output transport on
the closing socket. pipecat only clears its reference when it still points at that
socket, so the old handler cannot null out the new client.
"""

from loguru import logger
from pipecat.transports.websocket.server import (
    SingleClientWebsocketServerInputTransport,
    WebsocketServerTransport,
)


#: How long an evicted socket gets to answer the close handshake.
_EVICT_CLOSE_TIMEOUT_S = 1.0


class EvictingWebsocketServerInputTransport(SingleClientWebsocketServerInputTransport):
    """Input transport whose new connection replaces the current one."""

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
            await old.close()  # must stay awaited: see the module docstring
        await super()._client_handler(websocket)


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
