"""Unit tests for the pure active-mode speaker-ID resolver (no SDK/model — injected callbacks)."""
import numpy as np

from ambient_bridge.active_speaker_id import ActivePcmRing, SpeakerResolver, recent_spans


def _frame(n_samples: int, val: int = 1000) -> bytes:
    return np.full(n_samples, val, dtype=np.int16).tobytes()


def test_ring_elapsed_is_cumulative_and_slice_returns_audio():
    r = ActivePcmRing(capacity_s=10.0, sample_rate=16000)
    r.append(_frame(16000))  # 1s
    r.append(_frame(16000))  # 1s
    assert abs(r.elapsed_s - 2.0) < 1e-6
    s = r.slice(0.5, 1.5)
    assert s is not None and len(s) == 16000
    assert s.dtype == np.float32 and abs(float(s[0]) - 1000 / 32768.0) < 1e-4  # int16→float32


def test_ring_evicts_past_capacity_but_clock_keeps_running():
    r = ActivePcmRing(capacity_s=2.0, sample_rate=16000)
    for _ in range(5):
        r.append(_frame(16000))  # 5s total, cap 2s
    assert abs(r.elapsed_s - 5.0) < 1e-6   # elapsed = cumulative clock, unaffected by eviction
    assert r.slice(0.0, 1.0) is None        # early audio evicted
    assert r.slice(4.0, 5.0) is not None     # recent audio retained


def test_ring_slice_none_for_future_or_inverted():
    r = ActivePcmRing(capacity_s=10.0)
    r.append(_frame(16000))
    assert r.slice(5.0, 6.0) is None   # beyond what's buffered
    assert r.slice(2.0, 1.0) is None   # inverted span


def _resolver():
    return SpeakerResolver(min_speaker_s=6.0, recheck_s=15.0, embed_window_s=8.0)


def test_recent_spans_clips_to_window_and_filters_speaker():
    spans = [("S1", 0.0, 5.0), ("S2", 5.0, 7.0), ("S1", 7.0, 20.0)]
    # window 8 ending at now=20 → lo=12; only the S1 run [7,20] overlaps [12,20]
    assert recent_spans("S1", spans, now_elapsed=20.0, window_s=8.0) == [(12.0, 20.0)]
    assert recent_spans("S2", spans, now_elapsed=20.0, window_s=8.0) == []  # S2 ended at 7 < lo


def test_no_assignment_until_min_recent_audio():
    r = _resolver()
    calls = []
    changed = r.resolve(
        [("S1", 0.0, 3.0)], now_elapsed=3.0,  # only 3s < 6s min
        embed_spans_fn=lambda s: calls.append(s) or "emb", match_fn=lambda e: ("user", 0.6))
    assert changed == set() and r.assigned == {}
    assert calls == []  # never even embedded — not enough audio


def test_assigns_user_when_matched():
    r = _resolver()
    changed = r.resolve(
        [("S1", 0.0, 8.0)], now_elapsed=8.0,  # 8s ≥ 6
        embed_spans_fn=lambda s: "emb", match_fn=lambda e: ("user", 0.6))
    assert changed == {"S1"} and r.assigned == {"S1": "user"}


def test_recheck_debounce_skips_reembed_then_rechecks():
    r = _resolver()
    embeds = []
    emb = lambda s: embeds.append(1) or "e"  # noqa: E731
    match = lambda e: ("user", 0.6)          # noqa: E731
    r.resolve([("S1", 0.0, 8.0)], now_elapsed=8.0, embed_spans_fn=emb, match_fn=match)
    # 2nd call at t=10 (<15s recheck) → skipped, no re-embed even with fresh audio
    r.resolve([("S1", 0.0, 8.0), ("S1", 8.0, 10.0)], now_elapsed=10.0, embed_spans_fn=emb, match_fn=match)
    assert len(embeds) == 1
    # 3rd call at t=25 (17s > 15s recheck) → re-embeds
    r.resolve([("S1", 0.0, 8.0), ("S1", 8.0, 25.0)], now_elapsed=25.0, embed_spans_fn=emb, match_fn=match)
    assert len(embeds) == 2


def test_revert_to_positional_when_no_longer_matches():
    r = _resolver()
    r.resolve([("S1", 0.0, 8.0)], now_elapsed=8.0, embed_spans_fn=lambda s: "e", match_fn=lambda e: ("user", 0.6))
    assert r.assigned == {"S1": "user"}
    # later S1 audio no longer matches (label reused by someone else) → REVERT
    changed = r.resolve(
        [("S1", 0.0, 8.0), ("S1", 20.0, 30.0)], now_elapsed=30.0,
        embed_spans_fn=lambda s: "e", match_fn=lambda e: (None, 0.1))
    assert changed == {"S1"} and r.assigned == {}


def test_embed_none_no_assignment_no_crash():
    r = _resolver()
    changed = r.resolve(
        [("S1", 0.0, 8.0)], now_elapsed=8.0,
        embed_spans_fn=lambda s: None, match_fn=lambda e: ("user", 0.6))
    assert changed == set() and r.assigned == {}


def test_two_speakers_resolved_at_their_own_turns():
    r = _resolver()
    # S1 speaks 0–8 → resolved as user at now=8
    r.resolve([("S1", 0.0, 8.0)], now_elapsed=8.0, embed_spans_fn=lambda s: "u", match_fn=lambda e: ("user", 0.6))
    assert r.assigned == {"S1": "user"}
    # S2 speaks 12–20 → resolved as non-user at now=20; S1 (silent + within recheck) kept as user
    r.resolve(
        [("S1", 0.0, 8.0), ("S2", 12.0, 20.0)], now_elapsed=20.0,
        embed_spans_fn=lambda s: "o", match_fn=lambda e: (None, 0.1))
    assert r.assigned == {"S1": "user"}
