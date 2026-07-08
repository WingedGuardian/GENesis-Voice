"""Unit tests for the pure OMI transcript normalizer.

The segment SHAPE here mirrors a real captured OMI real-time-transcript payload
(object body ``{segments, session_id}``; snake_case fields; a stable UUID ``id``;
``start``/``end`` as conversation-relative seconds; ``translations`` /
``speech_profile_processed`` extras) — but every VALUE is synthetic. The real
account uid and any overheard text never enter the repo.
"""
from datetime import UTC, datetime

from omi_bridge.normalize import (
    NormalizedRow,
    decide_anchor,
    normalize_segments,
    parse_payload,
)

_UID = "test-omi-uid"


def _seg(**over):
    """A segment shaped like the real capture; override fields per test."""
    seg = {
        "id": "5d08d88f-6b2d-4736-9744-755634bb55cf",
        "text": "however you have to decide",
        "speaker": "SPEAKER_1",
        "speaker_id": 1,
        "is_user": False,
        "person_id": None,
        "start": 165.5,
        "end": 166.5,
        "translations": [],
        "speech_profile_processed": True,
        "stt_provider": None,
    }
    seg.update(over)
    return seg


# ── parse_payload ──────────────────────────────────────────────────────────
def test_parse_payload_object_body():
    # The real dev webhook body: an object with segments + session_id (== account uid).
    session_id, segs = parse_payload({"segments": [_seg()], "session_id": _UID})
    assert session_id == _UID
    assert len(segs) == 1 and segs[0]["id"] == "5d08d88f-6b2d-4736-9744-755634bb55cf"


def test_parse_payload_bare_array_fallback():
    # Defensive: some paths / docs show a bare array with no session_id.
    session_id, segs = parse_payload([_seg(), _seg(id="x")])
    assert session_id is None
    assert len(segs) == 2


def test_parse_payload_rejects_junk_and_non_dict_segments():
    assert parse_payload("nonsense") == (None, [])
    _, segs = parse_payload({"segments": [_seg(), "not-a-dict", 42], "session_id": _UID})
    assert len(segs) == 1  # non-dict entries dropped


# ── normalize_segments ─────────────────────────────────────────────────────
def test_normalize_maps_fields_and_anchors_ts():
    epoch0 = 1_000_000.0
    rows = normalize_segments([_seg(start=10.0, end=12.5)], uid=_UID, epoch0=epoch0)
    assert len(rows) == 1
    r = rows[0]
    assert isinstance(r, NormalizedRow)
    assert r.source == f"omi-{_UID}"
    assert r.is_user == 0  # False -> 0
    assert r.duration_s == 2.5
    assert r.segment_id == "5d08d88f-6b2d-4736-9744-755634bb55cf"
    # ts is epoch0 + start, ISO8601 UTC
    assert r.ts == datetime.fromtimestamp(epoch0 + 10.0, UTC).isoformat()


def test_normalize_meta_has_omi_fields_and_no_audio_block():
    rows = normalize_segments([_seg()], uid=_UID, epoch0=0.0)
    meta = rows[0].meta  # a dict; AmbientStore.insert JSON-encodes it
    assert "audio" not in meta  # text-only clarity path — no fabricated rms penalty
    assert meta["omi"]["segment_id"] == "5d08d88f-6b2d-4736-9744-755634bb55cf"
    assert meta["omi"]["speaker"] == "SPEAKER_1"  # raw OMI label preserved (column stays NULL)
    assert meta["omi"]["speaker_id"] == 1
    assert meta["omi"]["person_id"] is None
    assert meta["omi"]["speech_profile_processed"] is True  # is_user-reliability hint
    assert meta["omi"]["uid"] == _UID
    assert meta["asr_feats"]["n_tokens"] == len("however you have to decide".split())


def test_normalize_camelcase_tolerance():
    # Defensive: if a backend variant emits camelCase, still parse it.
    rows = normalize_segments(
        [{"id": "a", "text": "hi there", "speakerId": 3, "isUser": True, "start": 1.0, "end": 2.0}],
        uid=_UID,
        epoch0=0.0,
    )
    assert rows[0].meta["omi"]["speaker_id"] == 3
    assert rows[0].is_user == 1


def test_normalize_skips_empty_text():
    rows = normalize_segments(
        [_seg(text="   "), _seg(text=""), _seg(text="real words", id="keep")],
        uid=_UID,
        epoch0=0.0,
    )
    assert len(rows) == 1 and rows[0].segment_id == "keep"


def test_normalize_clamps_negative_duration():
    rows = normalize_segments([_seg(start=5.0, end=4.0)], uid=_UID, epoch0=0.0)
    assert rows[0].duration_s == 0.0


# ── decide_anchor ──────────────────────────────────────────────────────────
def test_decide_anchor_first_batch_sets_last_utterance_at_recv():
    # No anchor yet -> epoch0 chosen so the batch's last end lands at recv_ts.
    assert decide_anchor(None, 166.5, 1000.0, 60.0) == 1000.0 - 166.5


def test_decide_anchor_keeps_when_within_tolerance():
    epoch0 = 833.5
    # predicted = 833.5 + 170.0 = 1003.5, recv 1003.4 -> within 60s -> keep
    assert decide_anchor(epoch0, 170.0, 1003.4, 60.0) == epoch0


def test_decide_anchor_reanchors_beyond_tolerance():
    # A long gap (conversation rollover / downtime) drifts the prediction -> re-anchor.
    assert decide_anchor(833.5, 500.0, 2000.0, 60.0) == 2000.0 - 500.0
