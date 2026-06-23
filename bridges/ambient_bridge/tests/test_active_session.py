"""Tests for ActiveSession failure-path behavior (no real SDK — start() bails before
touching AsyncClient on key-missing, and finalize() with no client never uses it)."""
import asyncio
import types

from ambient_bridge.active_session import ActiveSession


def _cfg(outdir):
    return types.SimpleNamespace(
        active_output_dir=outdir,
        active_sm_key_path="/nonexistent/speechmatics.key",
        active_language="en",
        active_model="enhanced",
        active_max_delay=1.0,
        active_max_speakers=2,
    )


def test_start_key_missing_writes_visible_error(tmp_path):
    s = ActiveSession(_cfg(str(tmp_path)), source="test")
    asyncio.run(s.start())  # key missing → no client, but a VISIBLE error in the transcript
    files = list(tmp_path.glob("*.md"))
    assert len(files) == 1
    txt = files[0].read_text()
    assert "key missing" in txt
    assert s._client is None


def test_finalize_is_idempotent(tmp_path):
    s = ActiveSession(_cfg(str(tmp_path)), source="test")
    asyncio.run(s.finalize())  # no client → flush + CLOSED log
    asyncio.run(s.finalize())  # second call is a no-op (guarded) — no error, no double log
    assert len(list(tmp_path.glob("*.md"))) == 1
