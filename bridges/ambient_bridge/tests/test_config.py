"""Unit tests for env parsing of the active-mode diarization knobs (auto-detect + tuning)."""
from ambient_bridge.config import (
    AmbientConfig,
    _env_bool,
    _env_float_or_none,
    _env_int_or_none,
)


def test_env_int_or_none_auto_sentinels(monkeypatch):
    for sentinel in ("", "0", "auto", "AUTO", "none"):
        monkeypatch.setenv("X_MS", sentinel)
        assert _env_int_or_none("X_MS", 2) is None  # 'auto'/0/empty → no cap
    monkeypatch.setenv("X_MS", "5")
    assert _env_int_or_none("X_MS", 2) == 5
    monkeypatch.delenv("X_MS", raising=False)
    assert _env_int_or_none("X_MS", 2) == 2  # unset → default


def test_env_bool(monkeypatch):
    for v in ("0", "false", "no", ""):
        monkeypatch.setenv("X_B", v)
        assert _env_bool("X_B", True) is False
    monkeypatch.setenv("X_B", "1")
    assert _env_bool("X_B", False) is True
    monkeypatch.delenv("X_B", raising=False)
    assert _env_bool("X_B", True) is True  # unset → default


def test_env_float_or_none(monkeypatch):
    monkeypatch.setenv("X_F", "auto")
    assert _env_float_or_none("X_F", 0.5) is None
    monkeypatch.setenv("X_F", "0.4")
    assert _env_float_or_none("X_F", 0.5) == 0.4
    monkeypatch.delenv("X_F", raising=False)
    assert _env_float_or_none("X_F", None) is None  # unset → default


def test_active_diar_defaults_auto_detect(monkeypatch):
    # clean env → auto-detect (None) + prefer_current_speaker on + sensitivity SDK-default (None)
    for k in ("AMBIENT_ACTIVE_MAX_SPEAKERS", "AMBIENT_ACTIVE_PREFER_CURRENT_SPEAKER",
              "AMBIENT_ACTIVE_SPEAKER_SENSITIVITY"):
        monkeypatch.delenv(k, raising=False)
    c = AmbientConfig()
    assert c.active_max_speakers is None
    assert c.active_prefer_current_speaker is True
    assert c.active_speaker_sensitivity is None


def test_active_max_speakers_env_cap(monkeypatch):
    monkeypatch.setenv("AMBIENT_ACTIVE_MAX_SPEAKERS", "5")
    assert AmbientConfig().active_max_speakers == 5


def test_active_speaker_id_defaults(monkeypatch):
    for k in ("AMBIENT_ACTIVE_SPEAKER_ID_ENABLED", "AMBIENT_ACTIVE_USER_DISPLAY",
              "AMBIENT_ACTIVE_MIN_SPEAKER_S", "AMBIENT_ACTIVE_RECHECK_S",
              "AMBIENT_ACTIVE_USER_VERIFY_THRESHOLD", "AMBIENT_ACTIVE_SPEAKER_RING_S"):
        monkeypatch.delenv(k, raising=False)
    c = AmbientConfig()
    assert c.active_speaker_id_enabled is True
    assert c.active_user_display_name == "You"
    assert c.active_min_speaker_s == 6.0
    assert c.active_recheck_s == 10.0
    assert c.active_user_verify_threshold == 0.35
    assert c.active_speaker_ring_s == 120.0


def test_active_user_display_env_override(monkeypatch):
    monkeypatch.setenv("AMBIENT_ACTIVE_USER_DISPLAY", "Jay")
    assert AmbientConfig().active_user_display_name == "Jay"
