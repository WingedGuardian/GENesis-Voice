"""Pure normalizer: OMI real-time webhook transcript segments -> ``ambient_transcripts`` rows.

No I/O, no third-party imports (stdlib only) — safe to unit-test in isolation. The wire
format is verified against a live capture AND OMI's backend sender source
(``backend/utils/webhooks.py``): the dev real-time webhook POSTs an OBJECT
``{"segments": [...], "session_id": "<uid>"}`` (a bare array is tolerated as a fallback);
``session_id`` is the ACCOUNT uid (stable, NOT per-conversation); segment fields are
snake_case (``speaker_id``, ``is_user``, ...) — casing variants are tolerated defensively so a
backend drift degrades to "still parses", never "drops real speech". Each segment carries a
stable UUID ``id`` (the dedup key) and conversation-relative ``start``/``end`` seconds.

Rows are written into the SHARED ``ambient_transcripts`` store via ``AmbientStore.insert``:
  * ``provenance`` is left to the store default (``ambient_overheard``) — OMI IS ambient
    overheard audio, and using the same provenance as the Voice PE keeps OMI rows visible to
    any consumer (the future edge attention engine) that filters on it. The DEVICE is
    distinguished by ``source=omi-<uid>``, not provenance.
  * ``meta`` is a dict (the store JSON-encodes it) with NO ``audio`` block: OMI is text-only,
    so the engine takes the text-only clarity path — no fabricated ``rms=0`` loudness penalty.
  * ``is_user`` (OMI's device-side attribution) is applied post-insert via ``set_identity``;
    ``speaker_label`` is left NULL (OMI isn't run through our diarization pipeline). OMI's raw
    ``speaker`` string is preserved in ``meta.omi`` instead.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class NormalizedRow:
    """One ``ambient_transcripts`` row derived from an OMI segment.

    ``segment_id`` is NOT a table column — it is the OMI segment uuid, carried alongside for
    delivery-idempotent dedup (``seen_segments``). ``meta`` is a dict (JSON-encoded by the
    store). ``is_user`` (0/1/None) is applied via ``set_identity`` after insert, not by
    ``insert`` itself; ``speaker_name`` rides along for that call.
    """

    segment_id: str | None
    ts: str
    text: str
    duration_s: float
    source: str
    meta: dict
    is_user: int | None
    speaker_name: str | None


def _first(seg: dict, *keys, default=None):
    """Return the first present key's value — casing/naming tolerance."""
    for k in keys:
        if k in seg:
            return seg[k]
    return default


def _as_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def parse_payload(data) -> tuple[str | None, list[dict]]:
    """Extract ``(session_id, segments)`` from an already-decoded JSON body.

    Tolerates the real object shape and a bare-array fallback. ``session_id`` is
    informational only (auth decides on the query ``uid``); a mismatch is logged by the
    caller. Non-dict segment entries are dropped.
    """
    if isinstance(data, dict):
        session_id = data.get("session_id")
        raw_segs = data.get("segments") or []
    elif isinstance(data, list):
        session_id = None
        raw_segs = data
    else:
        return None, []
    segments = [s for s in raw_segs if isinstance(s, dict)]
    return session_id, segments


def normalize_segments(segments, *, uid: str, epoch0: float) -> list[NormalizedRow]:
    """Map OMI segments -> ``NormalizedRow`` list using ``epoch0`` for timestamps.

    ``epoch0`` is the per-uid anchor (see ``decide_anchor``): the wall-clock epoch that OMI's
    conversation-relative ``start=0`` corresponds to. Empty/whitespace segments are skipped;
    duration is clamped non-negative.
    """
    source = f"omi-{uid}"
    rows: list[NormalizedRow] = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start = _as_float(seg.get("start"))
        end = _as_float(seg.get("end"))
        is_user_raw = _first(seg, "is_user", "isUser")
        is_user = None if is_user_raw is None else int(bool(is_user_raw))
        meta = {
            "omi": {
                "uid": uid,
                "segment_id": seg.get("id"),
                "speaker": seg.get("speaker"),  # raw OMI label (e.g. "SPEAKER_1"); column stays NULL
                "speaker_id": _first(seg, "speaker_id", "speakerId"),
                "person_id": _first(seg, "person_id", "personId"),
                "stt_provider": _first(seg, "stt_provider", "sttProvider"),
                # speech-profile match ran device-side -> an is_user-reliability hint for a
                # later graduation step (a verdict from a profiled segment is more trustworthy).
                "speech_profile_processed": _first(
                    seg, "speech_profile_processed", "speechProfileProcessed"
                ),
            },
            "asr_feats": {"n_tokens": len(text.split())},
        }
        rows.append(
            NormalizedRow(
                segment_id=seg.get("id"),
                ts=datetime.fromtimestamp(epoch0 + start, UTC).isoformat(),
                text=text,
                duration_s=max(0.0, end - start),
                source=source,
                meta=meta,
                is_user=is_user,
                speaker_name=None,
            )
        )
    return rows


def decide_anchor(
    current_epoch0: float | None,
    batch_max_end: float,
    recv_ts: float,
    tolerance: float,
) -> float:
    """Return the ``epoch0`` to use for a batch (pure; the state layer persists it).

    Keep the existing anchor while this batch's predicted wall-clock
    (``current_epoch0 + batch_max_end``) lands within ``tolerance`` of the actual receive
    time. Otherwise — no anchor yet, or the prediction is off by more than ``tolerance`` (a
    conversation rollover, a downtime gap, or device thrash) — re-anchor so this batch's last
    utterance lands at ``recv_ts``. Self-correcting: any re-anchor puts timestamps back at the
    wall-clock of speech.
    """
    if current_epoch0 is None:
        return recv_ts - batch_max_end
    predicted = current_epoch0 + batch_max_end
    if abs(predicted - recv_ts) > tolerance:
        return recv_ts - batch_max_end
    return current_epoch0
