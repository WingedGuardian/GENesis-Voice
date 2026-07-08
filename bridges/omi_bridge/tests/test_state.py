"""Unit tests for OmiState: advisory dedup (idempotency + segment-id) + per-uid anchor.

All age logic is driven by a caller-supplied ``now`` so tests never touch the wall clock.
"""
from omi_bridge.state import IDEMPOTENCY_TTL_S, SEEN_SEGMENTS_TTL_S, OmiState


def _state(tmp_path):
    return OmiState(tmp_path / "omi_state.db")


# ── idempotency-key dedup (bonus layer; may be absent in prod) ──────────────
def test_idempotency_empty_key_never_duplicate(tmp_path):
    st = _state(tmp_path)
    # The live capture had NO Idempotency-Key header — an absent key must never dedup.
    assert st.is_duplicate_delivery(None, now=100.0) is False
    assert st.is_duplicate_delivery("", now=100.0) is False


def test_idempotency_first_then_repeat(tmp_path):
    st = _state(tmp_path)
    assert st.is_duplicate_delivery("k1", now=100.0) is False  # first sight records
    assert st.is_duplicate_delivery("k1", now=101.0) is True   # retry within TTL


def test_idempotency_expires(tmp_path):
    st = _state(tmp_path)
    assert st.is_duplicate_delivery("k1", now=100.0) is False
    later = 100.0 + IDEMPOTENCY_TTL_S + 1
    assert st.is_duplicate_delivery("k1", now=later) is False  # expired -> not a dup


# ── segment-id dedup (PRIMARY: every real segment carries a stable uuid) ─────
def test_seen_segments_roundtrip(tmp_path):
    st = _state(tmp_path)
    assert st.seen_segment_ids(["a", "b"], now=100.0) == set()  # nothing recorded yet
    st.record_segment_ids(["a", "b"], now=100.0)
    assert st.seen_segment_ids(["a", "b", "c"], now=101.0) == {"a", "b"}


def test_seen_segments_ignores_empty_ids(tmp_path):
    st = _state(tmp_path)
    st.record_segment_ids([None, "", "real"], now=100.0)
    assert st.seen_segment_ids([None, "", "real"], now=101.0) == {"real"}


def test_seen_segments_expire(tmp_path):
    st = _state(tmp_path)
    st.record_segment_ids(["a"], now=100.0)
    later = 100.0 + SEEN_SEGMENTS_TTL_S + 1
    assert st.seen_segment_ids(["a"], now=later) == set()  # past horizon -> unseen


# ── per-uid anchor ──────────────────────────────────────────────────────────
def test_anchor_absent_then_set(tmp_path):
    st = _state(tmp_path)
    assert st.get_anchor("uid1") is None
    st.set_anchor("uid1", 833.5, 166.5, now=1000.0)
    assert st.get_anchor("uid1") == (833.5, 166.5)


def test_anchor_is_per_uid(tmp_path):
    st = _state(tmp_path)
    st.set_anchor("uidA", 10.0, 1.0, now=1.0)
    st.set_anchor("uidB", 20.0, 2.0, now=1.0)
    assert st.get_anchor("uidA") == (10.0, 1.0)
    assert st.get_anchor("uidB") == (20.0, 2.0)


def test_state_persists_across_reopen(tmp_path):
    st = _state(tmp_path)
    st.record_segment_ids(["x"], now=100.0)
    st.set_anchor("uid1", 5.0, 1.0, now=100.0)
    st.close()
    st2 = OmiState(tmp_path / "omi_state.db")
    assert st2.seen_segment_ids(["x"], now=101.0) == {"x"}
    assert st2.get_anchor("uid1") == (5.0, 1.0)
