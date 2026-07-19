"""Tests for GenesisToolService.sync_conversation — cumulative-turns persist.

The bridge's cached context grows monotonically for the whole process lifetime.
sync_conversation filters it to user/assistant turns and POSTs the cumulative
list to the core /v1/voice/conversation route under a session id that rotates on
a long gap between persists (so each transcript maps to one human conversation).
These tests pin: the endpoint + payload shape, the gap-split session scoping, the
durable log-before-POST, 4xx-is-permanent, and the local fallback on failure.
"""

import asyncio
import logging

import httpx

from app.genesis_tool_service import GenesisToolService


def _msgs(n):
    """n alternating user/assistant context messages (bridge dict shape)."""
    out = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append({"role": role, "content": f"turn {i}"})
    return out


class _FakeResp:
    def __init__(self, payload=None):
        self._payload = payload if payload is not None else {"status": "ok", "appended": 0}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _http_error(code):
    """An httpx.HTTPStatusError carrying ``code`` (raised in place of a 2xx)."""
    req = httpx.Request("POST", "http://genesis/v1/voice/conversation")
    resp = httpx.Response(code, request=req)
    return httpx.HTTPStatusError(f"HTTP {code}", request=req, response=resp)


def _client_factory(behaviors):
    """Fake httpx.AsyncClient whose .post replays ``behaviors`` in order (an
    Exception is raised, a _FakeResp is returned; last behavior repeats).
    Records call count and the last POST's url + json for assertions."""
    calls = {"n": 0, "last_url": None, "last_json": None}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, *a, **k):
            calls["n"] += 1
            calls["last_url"] = url
            calls["last_json"] = k.get("json")
            i = calls["n"] - 1
            b = behaviors[min(i, len(behaviors) - 1)]
            if isinstance(b, BaseException):
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


def _svc(tmp_path, split=900.0):
    return GenesisToolService(
        "http://genesis",
        token="tok",
        fallback_dir=str(tmp_path),
        session_split_seconds=split,
    )


def _fake_clock(monkeypatch, start=1000.0):
    clock = [start]
    monkeypatch.setattr("app.genesis_tool_service.time.time", lambda: clock[0])
    return clock


# ── endpoint + payload shape ─────────────────────────────────────────


def test_posts_cumulative_turns_to_conversation_route(monkeypatch, tmp_path):
    factory = _patch_client(monkeypatch, [_FakeResp({"status": "ok", "appended": 2})])
    svc = _svc(tmp_path)
    result = asyncio.run(svc.sync_conversation(_msgs(2), satellite_id="pe-1"))

    assert result == {"status": "ok", "appended": 2}
    assert factory.calls["last_url"] == "http://genesis/v1/voice/conversation"
    body = factory.calls["last_json"]
    assert body["satellite_id"] == "pe-1"
    assert body["session_id"]  # non-empty
    # role/text turns — NOT the old "User:/Genesis:" labelled blob
    assert body["turns"] == [
        {"role": "user", "text": "turn 0"},
        {"role": "assistant", "text": "turn 1"},
    ]


def test_filters_non_user_assistant_and_nonstr_content(monkeypatch, tmp_path):
    factory = _patch_client(monkeypatch, [_FakeResp()])
    svc = _svc(tmp_path)
    messages = [
        {"role": "developer", "content": "system preamble"},
        {"role": "user", "content": "hi"},
        # assistant tool-call message: content is a list, not a str → dropped
        {"role": "assistant", "content": [{"type": "text", "text": "structured"}]},
        {"role": "assistant", "content": "hello"},
        {"role": "tool", "content": "tool result"},
        {"role": "user", "content": "   "},  # blank → dropped
    ]
    asyncio.run(svc.sync_conversation(messages))
    assert factory.calls["last_json"]["turns"] == [
        {"role": "user", "text": "hi"},
        {"role": "assistant", "text": "hello"},
    ]


def test_no_turns_returns_none_without_post(monkeypatch, tmp_path):
    factory = _patch_client(monkeypatch, [_FakeResp()])
    svc = _svc(tmp_path)
    result = asyncio.run(svc.sync_conversation([{"role": "system", "content": "x"}]))
    assert result is None
    assert factory.calls["n"] == 0


# ── gap-split session scoping ────────────────────────────────────────


def test_same_session_id_within_threshold(monkeypatch, tmp_path):
    clock = _fake_clock(monkeypatch)
    factory = _patch_client(monkeypatch, [_FakeResp()])
    svc = _svc(tmp_path, split=900.0)

    asyncio.run(svc.sync_conversation(_msgs(2)))
    sid1 = factory.calls["last_json"]["session_id"]

    clock[0] += 100  # < threshold: same conversation continues
    asyncio.run(svc.sync_conversation(_msgs(4)))
    body = factory.calls["last_json"]
    assert body["session_id"] == sid1
    # base stays 0 within an epoch → full cumulative list is re-sent (the core
    # dedups by line count); the delta is what actually lands.
    assert len(body["turns"]) == 4


def test_gap_rotates_session_and_bases_turns(monkeypatch, tmp_path):
    clock = _fake_clock(monkeypatch)
    factory = _patch_client(monkeypatch, [_FakeResp()])
    svc = _svc(tmp_path, split=900.0)

    asyncio.run(svc.sync_conversation(_msgs(4)))  # epoch 1: 4 turns
    sid1 = factory.calls["last_json"]["session_id"]

    clock[0] += 1000  # > threshold: previous conversation ended
    asyncio.run(svc.sync_conversation(_msgs(6)))  # cumulative 6; epoch 2
    body = factory.calls["last_json"]
    assert body["session_id"] != sid1
    # base = 4 (turns before the gap) → only the 2 post-gap turns are sent
    assert body["turns"] == [
        {"role": "user", "text": "turn 4"},
        {"role": "assistant", "text": "turn 5"},
    ]


def test_shrink_below_base_rotates_and_warns(monkeypatch, tmp_path, caplog):
    clock = _fake_clock(monkeypatch)
    factory = _patch_client(monkeypatch, [_FakeResp()])
    svc = _svc(tmp_path, split=900.0)

    asyncio.run(svc.sync_conversation(_msgs(4)))  # epoch 1
    clock[0] += 1000
    asyncio.run(svc.sync_conversation(_msgs(6)))  # gap → base = 4
    sid2 = factory.calls["last_json"]["session_id"]

    clock[0] += 100  # no gap this time; feed a list shorter than base (4)
    with caplog.at_level("WARNING", logger="app.genesis_tool_service"):
        asyncio.run(svc.sync_conversation(_msgs(2)))
    assert any("shrank below session base" in r.message for r in caplog.records)
    body = factory.calls["last_json"]
    assert body["session_id"] != sid2  # rotated to a clean session
    assert len(body["turns"]) == 2  # base reset to 0


def test_churn_after_gap_with_no_new_turns_skips_post(monkeypatch, tmp_path):
    clock = _fake_clock(monkeypatch)
    factory = _patch_client(monkeypatch, [_FakeResp()])
    svc = _svc(tmp_path, split=900.0)

    asyncio.run(svc.sync_conversation(_msgs(4)))  # n == 1
    clock[0] += 1000  # gap → base = 4
    asyncio.run(svc.sync_conversation(_msgs(4)))  # no new turns → pending empty
    assert factory.calls["n"] == 1  # second call made NO POST


def test_new_instance_starts_independent_session(monkeypatch, tmp_path):
    factory = _patch_client(monkeypatch, [_FakeResp(), _FakeResp()])
    s1 = _svc(tmp_path)
    asyncio.run(s1.sync_conversation(_msgs(3)))
    sid1 = factory.calls["last_json"]["session_id"]

    s2 = _svc(tmp_path)  # a bridge restart: fresh state
    asyncio.run(s2.sync_conversation(_msgs(3)))
    body = factory.calls["last_json"]
    assert body["session_id"] != sid1  # no shared/leaked state
    assert len(body["turns"]) == 3  # base 0, full list


# ── durability: log-before-POST, retry, fallback ─────────────────────


def test_logs_new_turns_before_post(monkeypatch, caplog, tmp_path):
    _patch_client(monkeypatch, [_FakeResp()])
    svc = _svc(tmp_path)
    with caplog.at_level(logging.INFO, logger="app.genesis_tool_service"):
        asyncio.run(svc.sync_conversation([{"role": "user", "content": "what time is it"}]))
    blob = "\n".join(r.message for r in caplog.records)
    assert "what time is it" in blob


def test_4xx_is_permanent_no_retry_writes_fallback(monkeypatch, tmp_path):
    factory = _patch_client(monkeypatch, [_http_error(400)])
    svc = _svc(tmp_path)
    result = asyncio.run(svc.sync_conversation(_msgs(2)))
    assert result is None
    assert factory.calls["n"] == 1  # 4xx not retried
    assert len(list(tmp_path.glob("*.txt"))) == 1


def test_5xx_retried_then_fallback(monkeypatch, tmp_path):
    factory = _patch_client(monkeypatch, [_http_error(503)])
    svc = _svc(tmp_path)
    result = asyncio.run(svc.sync_conversation(_msgs(2)))
    assert result is None
    assert factory.calls["n"] == 3  # retried to exhaustion
    assert len(list(tmp_path.glob("*.txt"))) == 1


def test_transient_network_error_retried_then_succeeds(monkeypatch, tmp_path):
    factory = _patch_client(
        monkeypatch,
        [httpx.ReadError("boom"), _FakeResp({"status": "ok", "appended": 2})],
    )
    svc = _svc(tmp_path)
    result = asyncio.run(svc.sync_conversation(_msgs(2)))
    assert result == {"status": "ok", "appended": 2}
    assert factory.calls["n"] == 2
    assert list(tmp_path.glob("*.txt")) == []


def test_fallback_file_contains_turn_text(monkeypatch, tmp_path):
    _patch_client(monkeypatch, [_http_error(400)])
    svc = _svc(tmp_path)
    asyncio.run(svc.sync_conversation([{"role": "user", "content": "remember the milk"}]))
    saved = list(tmp_path.glob("*.txt"))[0].read_text(encoding="utf-8")
    assert "remember the milk" in saved


def test_success_first_try_writes_no_fallback(monkeypatch, tmp_path):
    factory = _patch_client(monkeypatch, [_FakeResp({"status": "ok", "appended": 2})])
    svc = _svc(tmp_path)
    result = asyncio.run(svc.sync_conversation(_msgs(2)))
    assert result == {"status": "ok", "appended": 2}
    assert factory.calls["n"] == 1
    assert list(tmp_path.glob("*.txt")) == []
