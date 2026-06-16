"""Tests for SessionManager context caching, focused on staleness.

Regression coverage for the reconnect-churn bug: the device opens many
sub-second reconnects, each re-caching the SAME conversation. The cache must
NOT treat that churn as fresh activity — otherwise a day-old context is
replayed forever (observed: "It's Saturday June 13" replayed into a June-15
session).
"""
from app import session_manager


class _FakeContext:
    """Minimal stand-in for pipecat's LLMContext (only get_messages is used)."""

    def __init__(self, n_messages: int):
        self._messages = [{"role": "user", "content": "x"} for _ in range(n_messages)]

    def get_messages(self):
        return self._messages


class _FakeService:
    """Stand-in for OpenAIRealtimeLLMService — caching reads ``_context``."""

    def __init__(self, n_messages: int):
        self._context = _FakeContext(n_messages)


def _frozen_clock(monkeypatch):
    """Patch session_manager.time.time to a mutable clock; return the dict."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(session_manager.time, "time", lambda: clock["t"])
    return clock


def test_reconnect_churn_does_not_keep_stale_context_alive(monkeypatch):
    """Re-caching with no NEW messages must not refresh the cache age.

    A context must still expire ``reuse_timeout`` after the last *new content*,
    no matter how many times the device reconnects in the meantime.
    """
    clock = _frozen_clock(monkeypatch)
    mgr = session_manager.SessionManager(reuse_timeout=300)

    # Real conversation cached at t=1000 (23 messages).
    mgr.cache_context_from_service("server", _FakeService(23))

    # Reconnect churn: repeated re-caches of the SAME 23 messages, each within
    # reuse_timeout of the previous, spanning well past reuse_timeout in total.
    for t in (1100.0, 1200.0, 1290.0, 1380.0, 1500.0):
        clock["t"] = t
        mgr.cache_context_from_service("server", _FakeService(23))

    # 500s since the last NEW content (t=1000) > 300s window. Despite the
    # churn, the stale context must be gone — not revived from "0.0s ago".
    assert mgr.get_cached_context("server") is None
    assert "server" not in mgr.context_caches


def test_new_content_refreshes_window_and_preserves_continuity(monkeypatch):
    """A real new turn (message count grows) DOES refresh the window, so an
    active multi-turn conversation keeps its context across reconnects."""
    clock = _frozen_clock(monkeypatch)
    mgr = session_manager.SessionManager(reuse_timeout=300)

    mgr.cache_context_from_service("server", _FakeService(23))  # t=1000
    clock["t"] = 1290.0
    # User spoke again → 25 messages → must refresh the cache window.
    mgr.cache_context_from_service("server", _FakeService(25))
    entry = mgr.context_caches["server"]
    assert entry.timestamp == 1290.0
    assert entry.message_count == 25
    # 100s after the new turn, still inside 300s → context is reused.
    clock["t"] = 1390.0
    assert mgr.get_cached_context("server") is not None


def test_reuse_within_window_unchanged(monkeypatch):
    """Existing behavior intact: a reconnect within reuse_timeout reuses context."""
    clock = _frozen_clock(monkeypatch)
    mgr = session_manager.SessionManager(reuse_timeout=300)
    mgr.cache_context_from_service("server", _FakeService(10))  # t=1000
    clock["t"] = 1200.0  # 200s < 300s
    assert mgr.get_cached_context("server") is not None


def test_fresh_conversation_after_expiry_is_not_pre_aged(monkeypatch):
    """After a context ages out, a brand-new conversation caches with its own
    fresh timestamp — it is not wrongly treated as stale by the count gate."""
    clock = _frozen_clock(monkeypatch)
    mgr = session_manager.SessionManager(reuse_timeout=300)
    mgr.cache_context_from_service("server", _FakeService(23))  # t=1000
    clock["t"] = 1400.0  # 400s later → expired on read (and deleted)
    assert mgr.get_cached_context("server") is None
    # New conversation (fewer messages) now caches fresh, not pre-aged.
    mgr.cache_context_from_service("server", _FakeService(4))
    entry = mgr.context_caches["server"]
    assert entry.timestamp == 1400.0
    assert entry.message_count == 4
    clock["t"] = 1500.0  # 100s later, within window
    assert mgr.get_cached_context("server") is not None


def test_expired_entry_never_served_even_if_not_yet_evicted(monkeypatch):
    """If a stale entry expires but is never evicted by a read, a new
    conversation with FEWER messages is suppressed by the count gate — but the
    expired entry is still never served (the read-time expiry check evicts it)."""
    clock = _frozen_clock(monkeypatch)
    mgr = session_manager.SessionManager(reuse_timeout=300)
    mgr.cache_context_from_service("server", _FakeService(23))  # t=1000

    # Expired (400s) but get_cached_context never called → entry not yet evicted.
    clock["t"] = 1400.0
    # New, shorter conversation caches directly: gate (4 <= 23) suppresses the
    # write, leaving the stale entry in the dict.
    mgr.cache_context_from_service("server", _FakeService(4))
    # The very next read must NOT serve the stale 23-message context — it's
    # past reuse_timeout, so it's evicted and None is returned.
    assert mgr.get_cached_context("server") is None
    assert "server" not in mgr.context_caches
