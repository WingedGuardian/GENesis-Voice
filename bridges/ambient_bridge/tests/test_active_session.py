"""Tests for ActiveSession failure-path behavior (no real SDK — start() bails before
touching AsyncClient on key-missing, and finalize() with no client never uses it)."""
import asyncio
import types

from ambient_bridge.active_session import ActiveSession, _diar_kwargs


def _cfg(outdir):
    return types.SimpleNamespace(
        active_output_dir=outdir,
        active_sm_key_path="/nonexistent/speechmatics.key",
        active_language="en",
        active_model="enhanced",
        active_max_delay=1.0,
        active_max_speakers=2,
        active_prefer_current_speaker=True,
        active_speaker_sensitivity=None,
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


def test_add_marker_no_session_writes_divider_at_zero(tmp_path):
    # No start() → _t0 is None → marker lands at 00:00:00, file flushed immediately.
    s = ActiveSession(_cfg(str(tmp_path)), source="test")
    s.add_marker()
    files = list(tmp_path.glob("*.md"))
    assert len(files) == 1
    assert "[00:00:00] --- marker ---" in files[0].read_text()


def test_add_marker_elapsed_measured_from_t0(tmp_path):
    import time
    s = ActiveSession(_cfg(str(tmp_path)), source="test")
    s._t0 = time.monotonic() - 5.0  # simulate a session that opened ~5s ago
    s.add_marker()
    assert "[00:00:05] --- marker ---" in next(tmp_path.glob("*.md")).read_text()


def _diarcfg(max_speakers=None, prefer=True, sensitivity=None):
    return types.SimpleNamespace(
        active_max_speakers=max_speakers,
        active_prefer_current_speaker=prefer,
        active_speaker_sensitivity=sensitivity,
    )


def test_diar_kwargs_auto_detect_default():
    # auto-detect: NO max_speakers key (SDK auto-detects); prefer_current_speaker on; no sensitivity
    assert _diar_kwargs(_diarcfg()) == {"prefer_current_speaker": True}


def test_diar_kwargs_with_cap_and_sensitivity():
    kw = _diar_kwargs(_diarcfg(max_speakers=5, prefer=True, sensitivity=0.4))
    assert kw == {"max_speakers": 5, "prefer_current_speaker": True, "speaker_sensitivity": 0.4}


def test_diar_kwargs_prefer_false_is_included():
    # False is a deliberate choice (not None) → still passed to the SDK
    assert _diar_kwargs(_diarcfg(prefer=False)) == {"prefer_current_speaker": False}


# --- speaker IDENTITY wiring (ring → embed → match → label) ---------------------------------
import numpy as np  # noqa: E402


class _FakeRegistry:
    """Stand-in for SpeakerIDRegistry: any non-empty audio embeds to a fixed vector; best_match
    returns the configured name. Lets us drive ActiveSession's resolve chain without sherpa."""

    def __init__(self, match_name="user"):
        self._match = match_name

    def has_user(self):
        return True

    def embed(self, samp):
        return np.array([1.0, 0.0, 0.0], dtype=np.float32) if len(samp) else None

    def mean_embedding(self, embs):
        return np.mean(embs, axis=0).astype(np.float32) if embs else None

    def best_match(self, emb, threshold):
        return (self._match, 0.6) if self._match else (None, 0.1)


def _cfg_sid(outdir):
    return types.SimpleNamespace(
        active_output_dir=outdir, active_sm_key_path="/nonexistent/speechmatics.key",
        active_language="en", active_model="enhanced", active_max_delay=1.0,
        active_max_speakers=None, active_prefer_current_speaker=True, active_speaker_sensitivity=None,
        active_speaker_id_enabled=True, active_speaker_ring_s=120.0, active_min_speaker_s=6.0,
        active_user_verify_threshold=0.35, active_resolve_interval_s=5.0, active_recheck_s=15.0,
        active_embed_window_s=8.0, active_user_display_name="You", user_speaker_name="user",
    )


def test_active_session_resolves_user_label(tmp_path):
    s = ActiveSession(_cfg_sid(str(tmp_path)), source="t", speaker_id=_FakeRegistry("user"))
    assert s._ring is not None and s._resolver is not None
    s._ring.append(np.full(16000 * 8, 500, dtype=np.int16).tobytes())  # 8s of audio
    changed = s._resolve_blocking([("S1", 0.0, "hello there")], now_elapsed=8.0)
    assert "S1" in changed
    s._apply_labels()
    assert s._acc.labels == {"S1": "You"}  # registry name 'user' → display 'You'


def test_active_session_speaker_id_off_is_pure_relay(tmp_path):
    # speaker_id=None → no ring/resolver; send_audio is a no-op relay (no client, no buffer)
    s = ActiveSession(_cfg_sid(str(tmp_path)), source="t", speaker_id=None)
    assert s._ring is None and s._resolver is None
    asyncio.run(s.send_audio(b"\x00\x00" * 100))  # must not raise
