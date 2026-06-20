"""Unit tests for the speaker-ID registry's PURE logic (no ONNX).

The embedding path needs sherpa-onnx (edge-only) and is validated at E2E; here we
build registries via ``__new__`` (skipping the extractor) and test the centroid /
scoring / persistence / window-classification math.
"""
import threading

import numpy as np

from speaker_id import SpeakerIDRegistry


def _reg(voiceprints=None, dim=3, user="user"):
    r = SpeakerIDRegistry.__new__(SpeakerIDRegistry)
    r._voiceprints = dict(voiceprints or {})
    r._user_name = user
    r._dim = dim
    r._sr = 16000
    r._lock = threading.Lock()
    return r


def _unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / float(np.linalg.norm(v))


def test_mean_embedding_normalizes_and_averages():
    m = SpeakerIDRegistry.mean_embedding([_unit([1, 0, 0]), _unit([0, 1, 0])])
    assert abs(float(np.linalg.norm(m)) - 1.0) < 1e-5
    assert np.allclose(m, [0.7071, 0.7071, 0], atol=1e-3)


def test_mean_embedding_empty_or_all_none():
    assert SpeakerIDRegistry.mean_embedding([]) is None
    assert SpeakerIDRegistry.mean_embedding([None]) is None


def test_score_and_verify():
    r = _reg({"user": _unit([1, 0, 0])})
    assert r.score(_unit([1, 0, 0])) > 0.999          # identical → ~1.0
    assert abs(r.score(_unit([0, 1, 0]))) < 1e-6       # orthogonal → ~0
    assert r.score(None) == -1.0                       # no embedding
    assert r.score(_unit([1, 0, 0]), name="ghost") == -1.0   # unknown speaker
    assert r.verify(_unit([1, 0, 0]), 0.35) is True
    assert r.verify(_unit([0, 1, 0]), 0.35) is False


def test_save_load_roundtrip(tmp_path):
    p = str(tmp_path / "reg.json")
    r = _reg({"user": _unit([0.6, 0.8, 0])})
    r._persist_path = p
    r._save()
    r2 = _reg()
    r2._persist_path = p
    r2._load()
    assert r2.names() == ["user"]
    assert np.allclose(r2._voiceprints["user"], _unit([0.6, 0.8, 0]), atol=1e-6)


def test_load_dim_mismatch_ignored(tmp_path):
    p = str(tmp_path / "reg.json")
    r = _reg({"user": _unit([0.6, 0.8, 0])}, dim=3)
    r._persist_path = p
    r._save()
    r2 = _reg(dim=512)            # model dim changed → stored voiceprints are stale
    r2._persist_path = p
    r2._load()
    assert r2.names() == []


def test_classify_direct_and_cluster_anchored():
    r = _reg({"user": _unit([1, 0, 0])})
    user_long = _unit([0.95, 0.05, 0])   # clear user, long → direct
    user_short = _unit([0.5, 0.5, 0])    # noisy short user, same cluster as user_long
    other_long = _unit([0, 1, 0])        # other speaker, long → direct
    v = r.classify_window(
        [user_long, user_short, other_long], [4.0, 1.0, 4.0], [0, 0, 1],
        threshold=0.35, min_embed_s=3.0,
    )
    assert v[0] == (True, "direct")
    assert v[2] == (False, "direct")
    # short user utt inherits cluster 0's centroid (anchored by user_long) → user
    assert v[1] == (True, "cluster")


def test_classify_all_short_cluster_recovers():
    r = _reg({"user": _unit([1, 0, 0])})
    v = r.classify_window(
        [_unit([0.8, 0.2, 0]), _unit([0.7, 0.3, 0])], [1.0, 1.2], [0, 0],
        threshold=0.35, min_embed_s=3.0,
    )
    assert all(m == "cluster" for _, m in v)
    assert all(is_u is True for is_u, _ in v)   # averaged short embeddings → user


def test_classify_no_embedding_is_null():
    r = _reg({"user": _unit([1, 0, 0])})
    assert r.classify_window([None], [4.0], [0], threshold=0.35, min_embed_s=3.0) == [(None, None)]
