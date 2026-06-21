"""Unit tests for server-level PURE helpers (no sherpa/websockets runtime): the TCP
keep-alive setup that reaps half-open Voice PE sockets. WS server PINGs are off (the device
rejects them), so without this a silently-dead socket lingers ESTAB ~2h and inflates
active_connections; tuned keep-alive cuts detection to ~idle + intvl*cnt seconds."""
import socket

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
