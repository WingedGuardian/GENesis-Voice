"""Tests for GenesisToolService.store_conversation — durable transcript persist.

A 4-turn voice conversation was assembled but LOST when the single POST to
Genesis raised httpx.ReadError on an abrupt client disconnect (only the turn
count was logged, never the text). These tests pin the durability contract:
the full transcript is logged BEFORE the network call, the POST retries
transient errors, and on persistent failure the transcript is written to a
local fallback file so it is never lost.
"""
import asyncio
import logging

import httpx
import pytest

from app.genesis_tool_service import GenesisToolService

MESSAGES = [
    {"role": "user", "content": "what's on the menu today?"},
    {"role": "assistant", "content": "Salmon and rice."},
]


class _FakeResp:
    def __init__(self, payload=None):
        self._payload = payload if payload is not None else {"ok": True}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _client_factory(behaviors):
    """Fake httpx.AsyncClient class whose .post replays ``behaviors`` in order
    (an Exception is raised, a _FakeResp is returned). The last behavior repeats.
    Exposes ``.calls`` (a dict with ``n``) for assertions."""
    calls = {"n": 0}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            i = calls["n"]
            calls["n"] += 1
            b = behaviors[min(i, len(behaviors) - 1)]
            if isinstance(b, BaseException):  # incl. CancelledError (not an Exception)
                raise b
            return b

    _FakeClient.calls = calls
    return _FakeClient


async def _noop_sleep(*_a, **_k):
    return None


def _patch_client(monkeypatch, behaviors):
    factory = _client_factory(behaviors)
    monkeypatch.setattr("app.genesis_tool_service.httpx.AsyncClient", factory)
    monkeypatch.setattr("app.genesis_tool_service.asyncio.sleep", _noop_sleep)
    return factory


def test_logs_full_transcript_text_before_post(monkeypatch, caplog, tmp_path):
    """The complete transcript (every turn's text) is logged, so it survives in
    journald even if the POST later fails."""
    _patch_client(monkeypatch, [_FakeResp({"id": "m1"})])
    svc = GenesisToolService("http://genesis", fallback_dir=str(tmp_path))
    with caplog.at_level(logging.INFO, logger="app.genesis_tool_service"):
        result = asyncio.run(svc.store_conversation(MESSAGES))
    assert result == {"id": "m1"}
    blob = "\n".join(r.message for r in caplog.records)
    assert "what's on the menu today?" in blob
    assert "Salmon and rice." in blob


def test_retries_transient_error_then_succeeds(monkeypatch, tmp_path):
    """A transient httpx.ReadError is retried; a later success returns its body
    and writes NO fallback file."""
    factory = _patch_client(
        monkeypatch,
        [httpx.ReadError("boom"), httpx.ReadError("boom"), _FakeResp({"id": "ok"})],
    )
    svc = GenesisToolService("http://genesis", fallback_dir=str(tmp_path))
    result = asyncio.run(svc.store_conversation(MESSAGES))
    assert result == {"id": "ok"}
    assert factory.calls["n"] == 3
    assert list(tmp_path.glob("*")) == []


def test_fallback_file_written_on_persistent_failure(monkeypatch, tmp_path):
    """When every attempt fails, the transcript is saved to a local file (never
    lost) and the call returns None rather than raising."""
    factory = _patch_client(monkeypatch, [httpx.ReadError("down")])
    svc = GenesisToolService("http://genesis", fallback_dir=str(tmp_path))
    result = asyncio.run(svc.store_conversation(MESSAGES))
    assert result is None
    assert factory.calls["n"] == 3  # retried to exhaustion
    files = list(tmp_path.glob("*.txt"))
    assert len(files) == 1
    saved = files[0].read_text(encoding="utf-8")
    assert "what's on the menu today?" in saved
    assert "Salmon and rice." in saved


def test_success_first_try_writes_no_fallback(monkeypatch, tmp_path):
    factory = _patch_client(monkeypatch, [_FakeResp({"id": "ok"})])
    svc = GenesisToolService("http://genesis", fallback_dir=str(tmp_path))
    result = asyncio.run(svc.store_conversation(MESSAGES))
    assert result == {"id": "ok"}
    assert factory.calls["n"] == 1
    assert list(tmp_path.glob("*")) == []


def test_no_turns_returns_none_without_post(monkeypatch, tmp_path):
    factory = _patch_client(monkeypatch, [_FakeResp()])
    svc = GenesisToolService("http://genesis", fallback_dir=str(tmp_path))
    result = asyncio.run(svc.store_conversation([{"role": "system", "content": "x"}]))
    assert result is None
    assert factory.calls["n"] == 0
    assert list(tmp_path.glob("*")) == []


def test_cancelled_error_writes_fallback_then_reraises(monkeypatch, tmp_path):
    """The abrupt-disconnect path raises asyncio.CancelledError (a BaseException
    that skips ordinary excepts). It must still save the transcript locally, then
    re-raise so the cancellation propagates."""
    _patch_client(monkeypatch, [asyncio.CancelledError()])
    svc = GenesisToolService("http://genesis", fallback_dir=str(tmp_path))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(svc.store_conversation(MESSAGES))
    files = list(tmp_path.glob("*.txt"))
    assert len(files) == 1
    assert "Salmon and rice." in files[0].read_text(encoding="utf-8")


def test_non_httpx_error_retried_then_fallback(monkeypatch, tmp_path):
    """A non-httpx error (e.g. OSError / 'Event loop is closed') during teardown
    must NOT escape the retry/fallback — it's caught, retried, then saved."""
    factory = _patch_client(monkeypatch, [OSError("socket gone")])
    svc = GenesisToolService("http://genesis", fallback_dir=str(tmp_path))
    result = asyncio.run(svc.store_conversation(MESSAGES))
    assert result is None
    assert factory.calls["n"] == 3
    assert len(list(tmp_path.glob("*.txt"))) == 1
