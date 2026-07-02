"""Unit tests for the shadow STT-quality instrumentation (Phase 0).

Covers the two pure feature extractors that run on the 24/7 capture path. They MUST be
total functions — a malformed result or odd audio buffer must yield ``{}``, never raise,
because any exception here would kill ambient capture. sherpa_onnx/soxr are stubbed by
conftest, so this imports and runs off-edge / in CI.
"""
import numpy as np

from ambient_bridge import pipeline
from ambient_bridge.config import AmbientConfig


class _FakeResult:
    """Minimal stand-in for a sherpa OfflineRecognizerResult."""

    def __init__(self, ys_log_probs, tokens, lang="", emotion="", event=""):
        self.ys_log_probs = ys_log_probs
        self.tokens = tokens
        self.lang = lang
        self.emotion = emotion
        self.event = event


def test_asr_feats_stores_raw_logprobs_and_count():
    f = pipeline._asr_feats(_FakeResult([-0.2, -0.5, -1.9], ["A", "B", "C"]))
    assert f["ys_log_probs"] == [-0.2, -0.5, -1.9]   # raw, not pre-summarized
    assert f["n_tokens"] == 3
    assert "lang" not in f and "emotion" not in f and "event" not in f  # empty omitted


def test_asr_feats_rounds_logprobs_to_3dp():
    assert pipeline._asr_feats(_FakeResult([-0.123456], ["A"]))["ys_log_probs"] == [-0.123]


def test_asr_feats_keeps_populated_optional_fields():
    f = pipeline._asr_feats(_FakeResult([-0.1], ["X"], lang="en", emotion="neutral"))
    assert f["lang"] == "en"
    assert f["emotion"] == "neutral"
    assert "event" not in f


def test_asr_feats_empty_decode():
    f = pipeline._asr_feats(_FakeResult([], []))
    assert f == {"ys_log_probs": [], "n_tokens": 0}


def test_asr_feats_guard_never_raises_on_bad_result():
    class Bad:  # no ys_log_probs / tokens attrs → guard returns {}
        pass

    assert pipeline._asr_feats(Bad()) == {}


def test_audio_feats_basic_ranges():
    s = np.array([0.0, 0.5, -0.5, 0.5, -0.5], dtype=np.float32)
    f = pipeline._audio_feats(s)
    assert f["rms"] > 0.0
    assert 0.0 <= f["peak"] <= 1.0
    assert 0.0 <= f["zcr"] <= 1.0


def test_audio_feats_empty_samples_returns_empty():
    assert pipeline._audio_feats(np.array([], dtype=np.float32)) == {}


def test_audio_feats_single_sample_omits_zcr():
    f = pipeline._audio_feats(np.array([0.3], dtype=np.float32))
    assert "rms" in f and "peak" in f
    assert "zcr" not in f  # zcr needs >=2 samples


def test_audio_feats_guard_never_raises_on_bad_input():
    # A non-numeric input forces np.asarray(..., float32) to raise → guard returns {}.
    assert pipeline._audio_feats("not an array") == {}


def _patch_recognizer(monkeypatch):
    """Capture kwargs handed to the (stubbed) sherpa recognizer factory; avoid real model files."""
    captured = {}

    class _FakeRecognizer:
        @staticmethod
        def from_transducer(**kwargs):
            captured.update(kwargs)
            return object()

    monkeypatch.setattr(pipeline.sherpa_onnx, "OfflineRecognizer", _FakeRecognizer, raising=False)
    monkeypatch.setattr(pipeline, "_pick", lambda d, kind: f"{d}/{kind}.onnx")
    return captured


def test_engine_passes_decode_config_to_recognizer(monkeypatch):
    # default → modified_beam_search with max_active_paths=4 flows into from_transducer
    for k in ("AMBIENT_DECODING_METHOD", "AMBIENT_MAX_ACTIVE_PATHS"):
        monkeypatch.delenv(k, raising=False)
    captured = _patch_recognizer(monkeypatch)
    pipeline.AmbientEngine(AmbientConfig(), store=object())
    assert captured["decoding_method"] == "modified_beam_search"
    assert captured["max_active_paths"] == 4


def test_engine_decode_method_env_override(monkeypatch):
    # env can pin greedy_search (instant rollback) without a code change
    monkeypatch.setenv("AMBIENT_DECODING_METHOD", "greedy_search")
    monkeypatch.setenv("AMBIENT_MAX_ACTIVE_PATHS", "8")
    captured = _patch_recognizer(monkeypatch)
    pipeline.AmbientEngine(AmbientConfig(), store=object())
    assert captured["decoding_method"] == "greedy_search"
    assert captured["max_active_paths"] == 8


# --- ORT arena opt-out: provider plumbing (see ort_session.py) ------------------------------
# The recognizer + diar/embedding sessions are the variable-shape allocators whose BFC arena
# ratchets RSS; each must receive the provider string. VAD is deliberately EXCLUDED (fixed
# 512-sample inputs — nothing to fix, and it sits on the hot capture path).

def test_engine_passes_provider_default_cpu(monkeypatch):
    monkeypatch.delenv("AMBIENT_ORT_ARENA_OFF", raising=False)
    captured = _patch_recognizer(monkeypatch)
    pipeline.AmbientEngine(AmbientConfig(), store=object())
    assert captured["provider"] == "cpu"


def test_engine_passes_conf_provider_when_arena_off(monkeypatch, tmp_path):
    conf = tmp_path / "ort.conf"
    monkeypatch.setenv("AMBIENT_ORT_ARENA_OFF", "1")
    monkeypatch.setenv("AMBIENT_ORT_CONF_PATH", str(conf))
    captured = _patch_recognizer(monkeypatch)
    pipeline.AmbientEngine(AmbientConfig(), store=object())
    assert captured["provider"] == f"cpu:{conf}"
    assert "EnableCpuMemArena=0" in conf.read_text()


def _patch_diar_sherpa(monkeypatch):
    """Fake the sherpa diarization config classes; capture the provider each session gets."""
    captured = {}

    class _SegPyannote:
        def __init__(self, model):
            captured["seg_model"] = model

    class _SegModelCfg:
        def __init__(self, pyannote=None, num_threads=1, provider="cpu"):
            captured["seg_provider"] = provider

    class _EmbCfg:
        def __init__(self, model, num_threads=1, provider="cpu"):
            captured["emb_provider"] = provider

    class _ClustCfg:
        def __init__(self, num_clusters=-1, threshold=0.5):
            pass

    class _SdCfg:
        def __init__(self, segmentation=None, embedding=None, clustering=None,
                     min_duration_on=0.0, min_duration_off=0.0):
            pass

        def validate(self):
            return True

    class _Sd:
        def __init__(self, cfg):
            self.sample_rate = 16000

    for name, cls in (("OfflineSpeakerSegmentationPyannoteModelConfig", _SegPyannote),
                      ("OfflineSpeakerSegmentationModelConfig", _SegModelCfg),
                      ("SpeakerEmbeddingExtractorConfig", _EmbCfg),
                      ("FastClusteringConfig", _ClustCfg),
                      ("OfflineSpeakerDiarizationConfig", _SdCfg),
                      ("OfflineSpeakerDiarization", _Sd)):
        monkeypatch.setattr(pipeline.sherpa_onnx, name, cls, raising=False)
    monkeypatch.setattr(pipeline, "_autodetect_embedding", lambda d: "emb.onnx")
    return captured


def test_diar_engine_passes_provider_to_both_sessions(monkeypatch, tmp_path):
    conf = tmp_path / "ort.conf"
    monkeypatch.setenv("AMBIENT_ORT_ARENA_OFF", "1")
    monkeypatch.setenv("AMBIENT_ORT_CONF_PATH", str(conf))
    captured = _patch_diar_sherpa(monkeypatch)
    pipeline.DiarizationEngine(AmbientConfig())
    assert captured["seg_provider"] == f"cpu:{conf}"
    assert captured["emb_provider"] == f"cpu:{conf}"


def test_diar_engine_provider_default_cpu(monkeypatch):
    monkeypatch.delenv("AMBIENT_ORT_ARENA_OFF", raising=False)
    captured = _patch_diar_sherpa(monkeypatch)
    pipeline.DiarizationEngine(AmbientConfig())
    assert captured["seg_provider"] == "cpu"
    assert captured["emb_provider"] == "cpu"
