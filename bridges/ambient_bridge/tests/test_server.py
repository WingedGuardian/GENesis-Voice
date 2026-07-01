"""Unit tests for server-level PURE helpers (no sherpa/websockets runtime): the TCP
keep-alive setup that reaps half-open Voice PE sockets. WS server PINGs are off (the device
rejects them), so without this a silently-dead socket lingers ESTAB ~2h and inflates
active_connections; tuned keep-alive cuts detection to ~idle + intvl*cnt seconds."""
import asyncio
import os
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


# --- memory observability: parent + diar-child RSS in the health JSON --------------------------
# The MALLOC_ARENA_MAX=2 leak fix is process-wide, and diar runs in a spawn CHILD post
# subprocess-isolation — so its RSS is invisible to the parent's own footprint. Track both, via
# /proc/<pid>/statm (Linux, no psutil dep). Best-effort: a bad pid or odd pool → null, never raise.

def test_rss_mb_reads_own_process():
    mb = server_mod._rss_mb(os.getpid())
    assert isinstance(mb, float) and mb > 0  # this test process has a real resident set


def test_rss_mb_bad_pid_is_none():
    # A pid that cannot exist -> None, never raises (the health writer must not fail on this).
    assert server_mod._rss_mb(2**31 - 1) is None


def test_memory_snapshot_no_pool_reports_parent_only():
    fake = types.SimpleNamespace(_diar_pool=None)
    snap = server_mod.AmbientServer._memory_snapshot(fake)
    assert isinstance(snap["rss_parent_mb"], float) and snap["rss_parent_mb"] > 0
    assert snap["rss_diar_child_mb"] is None                 # no diar child -> null, not 0
    assert snap["rss_total_mb"] == snap["rss_parent_mb"]     # total == parent when childless


def test_memory_snapshot_sums_diar_child_from_executor_processes():
    # Mimic ProcessPoolExecutor's private {pid: Process} worker map with a real pid so /proc is
    # actually read; using that map (not a ppid scan) excludes the spawn resource_tracker sibling.
    fake_pool = types.SimpleNamespace(_processes={os.getpid(): object()})
    fake = types.SimpleNamespace(_diar_pool=fake_pool)
    snap = server_mod.AmbientServer._memory_snapshot(fake)
    assert snap["rss_parent_mb"] > 0
    assert snap["rss_diar_child_mb"] > 0
    assert snap["rss_total_mb"] == round(snap["rss_parent_mb"] + snap["rss_diar_child_mb"], 1)


def test_memory_snapshot_pool_missing_processes_is_safe():
    # A pool object without a usable _processes map must not raise -> child stays null.
    fake = types.SimpleNamespace(_diar_pool=types.SimpleNamespace())
    snap = server_mod.AmbientServer._memory_snapshot(fake)
    assert snap["rss_diar_child_mb"] is None
    assert snap["rss_parent_mb"] > 0


def test_memory_snapshot_pool_processes_none_is_safe():
    # ProcessPoolExecutor sets _processes = None on shutdown — must not raise (None -> {} guard).
    fake = types.SimpleNamespace(_diar_pool=types.SimpleNamespace(_processes=None))
    snap = server_mod.AmbientServer._memory_snapshot(fake)
    assert snap["rss_diar_child_mb"] is None
    assert snap["rss_parent_mb"] > 0


def test_memory_snapshot_dead_child_pid_is_filtered():
    # A _processes entry whose pid has exited (transient, before the pool manager reaps it) must
    # yield rss_child=None via the None-filter, never raise or count it as 0.
    fake_pool = types.SimpleNamespace(_processes={2**31 - 1: object()})
    fake = types.SimpleNamespace(_diar_pool=fake_pool)
    snap = server_mod.AmbientServer._memory_snapshot(fake)
    assert snap["rss_diar_child_mb"] is None                  # dead pid filtered
    assert snap["rss_total_mb"] == snap["rss_parent_mb"]      # total falls back to parent-only


def test_memory_snapshot_total_preserves_zero_reading(monkeypatch):
    # A 0.0 RSS reading (a zombie's statm reads all zeros) must NOT be dropped from the total as
    # if absent — rss_total_mb stays a float, never collapsing to int via a truthiness `x or 0`.
    monkeypatch.setattr(server_mod, "_rss_mb", lambda pid: 0.0)
    fake = types.SimpleNamespace(_diar_pool=None)
    snap = server_mod.AmbientServer._memory_snapshot(fake)
    assert snap["rss_parent_mb"] == 0.0
    assert isinstance(snap["rss_total_mb"], float) and snap["rss_total_mb"] == 0.0
