"""Unit tests for server-level PURE helpers (no sherpa/websockets runtime): the TCP
keep-alive setup that reaps half-open Voice PE sockets. WS server PINGs are off (the device
rejects them), so without this a silently-dead socket lingers ESTAB ~2h and inflates
active_connections; tuned keep-alive cuts detection to ~idle + intvl*cnt seconds."""
import asyncio
import socket
import types

from ambient_bridge import server as server_mod
from ambient_bridge.config import AmbientConfig
from ambient_bridge.server import _enable_tcp_keepalive


def test_enable_tcp_keepalive_sets_options():
    cfg = AmbientConfig()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        assert s.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 0  # off by default
        _enable_tcp_keepalive(s, cfg)
        assert s.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 1
        # The point of the fix: per-connection Linux keep-alive timers (minutes, not ~2h).
        # Mirror the impl's per-option hasattr guard (each is set independently).
        for opt, expected in (
            ("TCP_KEEPIDLE", cfg.keepalive_idle_s),
            ("TCP_KEEPINTVL", cfg.keepalive_intvl_s),
            ("TCP_KEEPCNT", cfg.keepalive_cnt),
        ):
            if hasattr(socket, opt):
                assert s.getsockopt(socket.IPPROTO_TCP, getattr(socket, opt)) == expected
    finally:
        s.close()


def test_enable_tcp_keepalive_none_is_noop():
    # transport.get_extra_info("socket") can be None on some transports — must not raise.
    _enable_tcp_keepalive(None, AmbientConfig())


# --- /marker control endpoint: branch logic (HTTP layer validated at E2E with curl) -------
# aiohttp.web is stubbed by conftest, so json_response doesn't exist — monkeypatch a capture
# shim and drive the unbound handler with a fake `self` (no heavy engine/sherpa init needed).
def _capture_json(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        server_mod.web, "json_response",
        lambda payload, status=200: seen.update(payload) or payload, raising=False)
    return seen


def test_handle_marker_no_session_is_graceful_noop(monkeypatch):
    seen = _capture_json(monkeypatch)
    fake = types.SimpleNamespace(_active_session=None)
    asyncio.run(server_mod.AmbientServer._handle_marker(fake, object()))
    assert seen == {"marked": False}  # stray press / passive mode → harmless, nothing marked


def test_handle_marker_with_session_marks_it(monkeypatch):
    seen = _capture_json(monkeypatch)
    calls = []
    sess = types.SimpleNamespace(add_marker=lambda: calls.append(1))
    fake = types.SimpleNamespace(_active_session=sess)
    asyncio.run(server_mod.AmbientServer._handle_marker(fake, object()))
    assert seen == {"marked": True}
    assert calls == [1]  # the live session was actually marked


# --- backpressure decouple: the WS read loop must NEVER block on per-frame processing -------
# (else the socket stalls, the device's ping goes unanswered, and its 10s pong-timeout drops +
# churns the ambient connection under audio load — the root cause this fix removes).

def test_enqueue_drop_oldest_not_full_keeps_messages_in_order():
    q = asyncio.Queue(maxsize=2)
    assert server_mod.AmbientServer._enqueue_drop_oldest(q, b"1") is False
    assert server_mod.AmbientServer._enqueue_drop_oldest(q, b"2") is False
    assert q.get_nowait() == b"1"
    assert q.get_nowait() == b"2"


def test_enqueue_drop_oldest_sheds_oldest_when_full():
    q = asyncio.Queue(maxsize=2)
    server_mod.AmbientServer._enqueue_drop_oldest(q, b"1")
    server_mod.AmbientServer._enqueue_drop_oldest(q, b"2")
    assert server_mod.AmbientServer._enqueue_drop_oldest(q, b"3") is True  # full → drop oldest
    assert [q.get_nowait(), q.get_nowait()] == [b"2", b"3"]  # b"1" shed; newest two kept, in order


class _FakePipeline:
    """Async feed/flush stub (no sherpa). Records feed order; counts utterances."""
    utterances = 0

    def __init__(self, fail_on: int | None = None) -> None:
        self.fed: list[bytes] = []
        self.flushed = False
        self._fail_on = fail_on

    async def feed(self, message: bytes) -> int:
        self.fed.append(message)
        if self._fail_on is not None and len(self.fed) == self._fail_on:
            raise RuntimeError("boom")
        return 1

    async def flush(self) -> int:
        self.flushed = True
        return 2


def _run_consumer(fake_self, pipeline, frames):
    q = asyncio.Queue()
    for f in frames:
        q.put_nowait(f)
    q.put_nowait(None)  # sentinel → drain done
    asyncio.run(server_mod.AmbientServer._consume_frames(fake_self, "src", pipeline, q))


def test_consume_frames_passive_feeds_in_order_then_flushes():
    fake = types.SimpleNamespace(_mode="passive", _utterances_total=0)
    pipe = _FakePipeline()
    _run_consumer(fake, pipe, [b"a", b"b", b"c"])
    assert pipe.fed == [b"a", b"b", b"c"]      # ordered, all drained off the queue
    assert pipe.flushed                        # finalize/flush on the sentinel (close)
    assert fake._utterances_total == 3 + 2     # 1 per feed + 2 from flush


def test_consume_frames_one_bad_frame_does_not_stop_capture():
    fake = types.SimpleNamespace(_mode="passive", _utterances_total=0)
    pipe = _FakePipeline(fail_on=2)            # the 2nd feed raises
    _run_consumer(fake, pipe, [b"a", b"b", b"c"])
    assert pipe.fed == [b"a", b"b", b"c"]      # continued past the error (3rd frame still fed)
    assert pipe.flushed                        # flush still runs
    assert fake._utterances_total == 2 + 2     # a + c succeeded; b raised; + flush 2


class _FakeWebSocket:
    """Minimal async-iterable WS: yields the given messages, then ends (connection closed)."""
    remote_address = ("192.0.2.57",)  # TEST-NET-1 (RFC 5737) — documentation placeholder
    transport = None

    def __init__(self, messages):
        self._messages = list(messages)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


def test_handler_decouples_read_from_processing_and_flushes_on_close():
    pipe = _FakePipeline()
    fake = types.SimpleNamespace(
        _cfg=AmbientConfig(),
        _engine=types.SimpleNamespace(new_pipeline=lambda source: pipe),
        _conn_stats=types.SimpleNamespace(on_connect=lambda: None, on_disconnect=lambda: None),
        _handler_tasks=set(), _active=0, _mode="passive", _utterances_total=0,
        _frames_dropped=0, _last_connection_ts=None, _speaker_id=None, _active_session=None,
        _on_control=lambda *a: None,
        _enqueue_drop_oldest=server_mod.AmbientServer._enqueue_drop_oldest,
    )
    fake._consume_frames = types.MethodType(server_mod.AmbientServer._consume_frames, fake)
    ws = _FakeWebSocket([b"a", b"b", "  {}  ", b"c"])  # a str control frame interleaved
    asyncio.run(server_mod.AmbientServer._handler(fake, ws))
    assert pipe.fed == [b"a", b"b", b"c"]   # all bytes processed off the read path, in order
    assert pipe.flushed                     # flushed on close
    assert fake._active == 0                 # decremented in finally
    assert fake._utterances_total == 3 + 2  # 3 feeds + flush
