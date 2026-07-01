"""Unit tests for the diarization process-isolation split (the parent/child boundary).

The heavy sherpa work is faked; these check the marshaling contract that makes the WS-churn fix
correct: the CHILD (``process_window``) diarizes + embeds and honours ``do_speaker_id``; the PARENT
(``_finish_window``) classifies the child's embeddings against its voiceprints + writes — and still
writes diar labels even with speaker-ID off.
"""
import types

import numpy as np

from ambient_bridge import diar_subprocess
from ambient_bridge import server as server_mod


class _FakeEngine:
    def __init__(self, segs):
        self._segs = segs

    def process(self, raw):
        return self._segs


class _FakeEmbedder:
    """embed() returns a vector for long-enough audio, None otherwise (mirrors the real contract)."""
    def __init__(self):
        self.calls = []

    def embed(self, samples):
        self.calls.append(len(samples))
        return np.ones(4, dtype=np.float32) if len(samples) >= 2 else None


# --- CHILD: process_window ---------------------------------------------------

def test_process_window_skips_embedding_when_speaker_id_off(monkeypatch):
    monkeypatch.setattr(diar_subprocess, "_ENGINE", _FakeEngine([(0.0, 1.0, 0)]))
    emb = _FakeEmbedder()
    monkeypatch.setattr(diar_subprocess, "_EMBEDDER", emb)
    segs, embeddings = diar_subprocess.process_window(
        np.zeros(16000, dtype=np.float32), [(1, 0.0, 1.0)], 16000, False)
    assert segs == [(0.0, 1.0, 0)]
    assert embeddings is None       # do_speaker_id False → no wasted embeds
    assert emb.calls == []          # embedder never touched


def test_process_window_embeds_each_span(monkeypatch):
    monkeypatch.setattr(diar_subprocess, "_ENGINE", _FakeEngine([(0.0, 2.0, 0), (2.0, 4.0, 1)]))
    emb = _FakeEmbedder()
    monkeypatch.setattr(diar_subprocess, "_EMBEDDER", emb)
    raw = np.zeros(16000 * 5, dtype=np.float32)
    segs, embeddings = diar_subprocess.process_window(raw, [(1, 0.0, 2.0), (2, 2.0, 4.0)], 16000, True)
    assert segs == [(0.0, 2.0, 0), (2.0, 4.0, 1)]
    assert len(embeddings) == 2 and all(e is not None for e in embeddings)
    assert emb.calls == [32000, 32000]   # each 2.0s span sliced at 16k → embedded


def test_process_window_no_embedder_loaded_returns_none(monkeypatch):
    # Defensive: speaker-ID disabled at init (embed model absent) → _EMBEDDER is None. Even if the
    # parent asks for it, the child must not crash — it returns segs + None embeddings.
    monkeypatch.setattr(diar_subprocess, "_ENGINE", _FakeEngine([(0.0, 1.0, 0)]))
    monkeypatch.setattr(diar_subprocess, "_EMBEDDER", None)
    segs, embeddings = diar_subprocess.process_window(
        np.zeros(16000, dtype=np.float32), [(1, 0.0, 1.0)], 16000, True)
    assert segs == [(0.0, 1.0, 0)]
    assert embeddings is None


# --- PARENT: _finish_window --------------------------------------------------

class _FakeStore:
    def __init__(self):
        self.identities = []
    def set_identity(self, row_id, *, speaker_name, is_user, method):
        self.identities.append((row_id, speaker_name, is_user, method))


class _FakeReg:
    def has_user(self):
        return True
    def classify_window(self, embeddings, durations, clusters, *, threshold, min_embed_s):
        # First utterance = the user (direct), the rest unresolved (None) — the second must NOT write.
        return [("user", True, "direct")] + [(None, None, None)] * (len(embeddings) - 1)


def test_finish_window_classifies_child_embeddings_and_writes_only_decided():
    store = _FakeStore()
    fake = types.SimpleNamespace(
        _store=store,
        _speaker_id=_FakeReg(),
        _assign_labels=lambda window, segs: None,          # diar-label path tested elsewhere
        _overlap_cluster=lambda s, e, segs: 0,
        _cfg=types.SimpleNamespace(user_verify_threshold=0.35, min_embed_s=3.0),
    )
    window = types.SimpleNamespace(window_idx=1, spans=[(10, 0.0, 4.0), (11, 4.0, 5.0)])
    server_mod.AmbientServer._finish_window(fake, window, [(0.0, 4.0, 0)], [object(), object()])
    assert store.identities == [(10, "user", True, "direct")]   # only the is_user-not-None verdict


def test_finish_window_writes_labels_even_with_no_embeddings():
    calls = {"assign": 0}
    fake = types.SimpleNamespace(
        _speaker_id=None,
        _assign_labels=lambda window, segs: calls.__setitem__("assign", calls["assign"] + 1),
    )
    window = types.SimpleNamespace(window_idx=1, spans=[(10, 0.0, 1.0)])
    server_mod.AmbientServer._finish_window(fake, window, [(0.0, 1.0, 0)], None)
    assert calls["assign"] == 1   # diar labels still written; classify cleanly skipped
