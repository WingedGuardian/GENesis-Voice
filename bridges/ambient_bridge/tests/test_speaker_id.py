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
    r._vp_lock = threading.Lock()
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


def test_best_match_argmax_threshold():
    r = _reg({"user": _unit([1, 0, 0]), "alice": _unit([0, 1, 0])})
    name, score = r.best_match(_unit([0.9, 0.1, 0]), 0.35)   # closest to user
    assert name == "user" and score > 0.9
    name, _ = r.best_match(_unit([0.1, 0.9, 0]), 0.35)       # closest to alice
    assert name == "alice"
    name, score = r.best_match(_unit([0, 0, 1]), 0.35)       # matches nobody ≥ threshold
    assert name is None and score < 0.35
    assert r.best_match(None, 0.35) == (None, -1.0)          # no embedding


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
    assert v[0] == ("user", True, "direct")
    assert v[2] == (None, False, "direct")    # orthogonal to user → matches nobody → name None
    # short user utt inherits cluster 0's centroid (anchored by user_long) → user
    assert v[1] == ("user", True, "cluster")


def test_classify_all_short_cluster_recovers():
    r = _reg({"user": _unit([1, 0, 0])})
    v = r.classify_window(
        [_unit([0.8, 0.2, 0]), _unit([0.7, 0.3, 0])], [1.0, 1.2], [0, 0],
        threshold=0.35, min_embed_s=3.0,
    )
    assert all(m == "cluster" for _, _, m in v)
    assert all(name == "user" and is_u is True for name, is_u, _ in v)  # averaged short → user


def test_classify_no_embedding_is_null():
    r = _reg({"user": _unit([1, 0, 0])})
    assert r.classify_window([None], [4.0], [0], threshold=0.35, min_embed_s=3.0) == [(None, None, None)]


def test_classify_none_cluster_short_stays_null():
    # A short utt with no diar cluster (clusters=None) must NOT be averaged with other
    # gap utts — it stays NULL (no direct verdict possible, no cluster to inherit).
    r = _reg({"user": _unit([1, 0, 0])})
    v = r.classify_window([_unit([0.8, 0.2, 0])], [1.0], [None], threshold=0.35, min_embed_s=3.0)
    assert v == [(None, None, None)]


def test_classify_none_cluster_long_is_direct():
    # A long utt still gets a DIRECT verdict even with no diar cluster (direct path
    # does not depend on clustering).
    r = _reg({"user": _unit([1, 0, 0])})
    v = r.classify_window([_unit([0.9, 0.1, 0])], [4.0], [None], threshold=0.35, min_embed_s=3.0)
    assert v == [("user", True, "direct")]


def test_registry_passes_provider_to_extractor_config(monkeypatch, tmp_path):
    # The ORT arena opt-out must reach the registry's embedding session (both the parent's
    # active-ID registry and the diar child's embed-only registry construct through here).
    import speaker_id as sid_mod
    captured = {}

    class _Cfg:
        def __init__(self, model, num_threads=1, provider="cpu"):
            captured["provider"] = provider

        def validate(self):
            return True

    class _Ext:
        dim = 4

        def __init__(self, cfg):
            pass

    monkeypatch.setattr(sid_mod.sherpa_onnx, "SpeakerEmbeddingExtractorConfig", _Cfg, raising=False)
    monkeypatch.setattr(sid_mod.sherpa_onnx, "SpeakerEmbeddingExtractor", _Ext, raising=False)
    SpeakerIDRegistry("m.onnx", persist_path=str(tmp_path / "reg.json"),
                      provider="cpu:/x/ort.conf")
    assert captured["provider"] == "cpu:/x/ort.conf"
