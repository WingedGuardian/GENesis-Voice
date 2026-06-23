"""Unit tests for the active-mode transcript accumulator (no SDK / no network)."""
from ambient_bridge.active_transcript import TranscriptAccumulator, _fmt_ts, words_to_runs


def _final(words):
    # words: list of (content, speaker, type, start_time)
    return {
        "metadata": {"transcript": " ".join(w[0] for w in words)},
        "results": [
            {"type": t, "start_time": st, "alternatives": [{"content": c, "speaker": spk}]}
            for (c, spk, t, st) in words
        ],
    }


def test_fmt_ts():
    assert _fmt_ts(0) == "00:00:00"
    assert _fmt_ts(3661) == "01:01:01"
    assert _fmt_ts(-5) == "00:00:00"


def test_runs_group_by_speaker_and_attach_punctuation():
    runs = words_to_runs([
        {"type": "word", "start_time": 1.0, "alternatives": [{"content": "hello", "speaker": "S1"}]},
        {"type": "word", "start_time": 1.5, "alternatives": [{"content": "there", "speaker": "S1"}]},
        {"type": "punctuation", "start_time": 1.8, "alternatives": [{"content": ".", "speaker": "S1"}]},
        {"type": "word", "start_time": 2.0, "alternatives": [{"content": "hi", "speaker": "S2"}]},
    ])
    assert runs == [("S1", 1.0, "hello there."), ("S2", 2.0, "hi")]


def test_missing_speaker_falls_back_to_qmark():
    runs = words_to_runs([{"type": "word", "start_time": 0.0, "alternatives": [{"content": "x"}]}])
    assert runs == [("?", 0.0, "x")]


def test_leading_punctuation_dropped_not_a_run():
    runs = words_to_runs([
        {"type": "punctuation", "start_time": 0.0, "alternatives": [{"content": ".", "speaker": "S1"}]},
        {"type": "word", "start_time": 0.5, "alternatives": [{"content": "Hello", "speaker": "S1"}]},
    ])
    assert runs == [("S1", 0.5, "Hello")]


def test_add_final_and_render():
    acc = TranscriptAccumulator(title="T")
    acc.add_final(_final([("Hello", "S1", "word", 1.0), ("world", "S1", "word", 1.4)]))
    acc.add_final(_final([("Hi", "S2", "word", 3.0)]))
    out = acc.render()
    assert out.startswith("# T")
    assert "[00:00:01] **S1**: Hello world" in out
    assert "[00:00:03] **S2**: Hi" in out


def test_partial_is_provisional_and_superseded_by_final():
    acc = TranscriptAccumulator()
    acc.set_partial({"metadata": {"transcript": "in progress"}})
    assert "_… in progress_" in acc.render()
    acc.add_final(_final([("done", "S1", "word", 5.0)]))
    rendered = acc.render()
    assert "in progress" not in rendered
    assert "**S1**: done" in rendered


def test_consecutive_same_speaker_finals_merge_into_one_turn():
    acc = TranscriptAccumulator()
    acc.add_final(_final([("one", "S1", "word", 1.0)]))
    acc.add_final(_final([("two", "S1", "word", 2.0)]))
    assert acc.render().count("**S1**") == 1
    assert "one two" in acc.render()
