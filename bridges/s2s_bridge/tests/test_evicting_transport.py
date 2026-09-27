"""A reconnecting device must replace a stale connection, not be refused.

pipecat 1.4.0 changed the single-client websocket transport to REJECT a new client
(close 1013) while the old socket still looks open. With server pings disabled
(the device rejects PING frames), a socket left half-open by a device reboot or a
Wi-Fi drop is never detected, so the device's reconnects would be refused until
the idle manager closes the stale one. The bridge restores pipecat 1.3.0's
behaviour: a new connection evicts the old one.

Real sockets, real pipecat, the transport the bridge actually builds.
"""

import asyncio
import base64
import os
import socket
import time

import websockets
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask

from app.websocket_handler import WebSocketHandler


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _replace_connection():
    port = _free_port()
    handler = WebSocketHandler(host="127.0.0.1", port=port)
    transport = handler.create_transport()
    task = PipelineTask(
        Pipeline([transport.input(), transport.output()]),
        idle_timeout_secs=None,
        cancel_on_idle_timeout=False,
    )
    run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))
    try:
        await asyncio.sleep(1.0)
        first = await websockets.connect(f"ws://127.0.0.1:{port}/")  # a cooperative old client (answers the close)
        await asyncio.sleep(0.5)
        second = await websockets.connect(f"ws://127.0.0.1:{port}/")  # the device's reconnect
        await asyncio.sleep(1.0)
        assert transport.output()._websocket is transport.input()._websocket
        return first.state.name, second.state.name, second.close_code, transport
    finally:
        await task.cancel()
        try:
            await asyncio.wait_for(run, 5)
        except Exception:
            pass


def test_a_reconnect_evicts_the_stale_connection():
    first, second, second_code, transport = asyncio.run(_replace_connection())
    assert second == "OPEN", f"reconnect was refused (close code {second_code})"
    assert first == "CLOSED"
    # The bridge's own code (disconnect_tool, main) type-checks against this name.
    from pipecat.transports.websocket.server import WebsocketServerTransport

    assert isinstance(transport, WebsocketServerTransport)


def _raw_half_open(port: int) -> socket.socket:
    """Complete a websocket handshake on a raw socket, then never read again: to the
    server this is a device that vanished without closing (reboot, Wi-Fi drop)."""
    s = socket.create_connection(("127.0.0.1", port))
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall(
        (
            f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
    )
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(4096)
    return s


async def _replace_half_open():
    port = _free_port()
    transport = WebSocketHandler(host="127.0.0.1", port=port).create_transport()
    task = PipelineTask(
        Pipeline([transport.input(), transport.output()]),
        idle_timeout_secs=None,
        cancel_on_idle_timeout=False,
    )
    run = asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))
    stale = None
    try:
        await asyncio.sleep(1.0)
        stale = await asyncio.to_thread(_raw_half_open, port)
        await asyncio.sleep(0.5)
        start = time.monotonic()
        fresh = await websockets.connect(f"ws://127.0.0.1:{port}/")
        while time.monotonic() - start < 15:
            current = transport.input()._websocket
            if current is not None and current.remote_address == fresh.local_address:
                break
            await asyncio.sleep(0.05)
        return time.monotonic() - start, fresh.state.name
    finally:
        if stale is not None:
            stale.close()
        await task.cancel()
        try:
            await asyncio.wait_for(run, 5)
        except Exception:
            pass


def test_a_half_open_connection_is_replaced_quickly():
    """The library would wait 10s for a vanished peer to answer the close, leaving
    the reconnected device talking to nobody. The evicted socket gets 1s."""
    took, state = asyncio.run(_replace_half_open())
    assert state == "OPEN"
    assert took < 3.0, f"new client took {took:.1f}s to be served"
