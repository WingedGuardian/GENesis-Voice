"""Config tests for the meeting bridge — token auth helpers + env-driven defaults.

Pure (no aiohttp / speechmatics), mirrors omi_bridge's config idiom.
"""

from meeting_bridge.config import MeetingConfig


def test_token_candidates_filters_empty():
    cfg = MeetingConfig(ingest_token="cur", ingest_token_previous="")
    assert cfg.token_candidates() == ("cur",)
    cfg2 = MeetingConfig(ingest_token="cur", ingest_token_previous="old")
    assert cfg2.token_candidates() == ("cur", "old")


def test_token_candidates_empty_is_fail_closed():
    # No token configured -> no candidates -> nothing authenticates.
    cfg = MeetingConfig(ingest_token="", ingest_token_previous="")
    assert cfg.token_candidates() == ()


def test_sm_key_path_defaults_to_shared_ambient_key(monkeypatch):
    # The meeting bridge reuses the ambient bridge's existing Speechmatics key by default,
    # rather than provisioning a second one.
    monkeypatch.delenv("MEETING_SM_KEY_PATH", raising=False)
    cfg = MeetingConfig()
    assert cfg.sm_key_path.endswith("/.ambient-active/speechmatics.key")


def test_output_dir_default_and_override(monkeypatch):
    monkeypatch.delenv("MEETING_OUTPUT_DIR", raising=False)
    assert MeetingConfig().output_dir.endswith("/meeting-sessions")
    monkeypatch.setenv("MEETING_OUTPUT_DIR", "/tmp/mtg")
    assert MeetingConfig().output_dir == "/tmp/mtg"


def test_diarization_defaults_are_meeting_tuned(monkeypatch):
    # Meeting-appropriate defaults (segment more) — the OPPOSITE of the ambient bridge's
    # near-field anti-over-split defaults (prefer_current=True, sensitivity=None).
    monkeypatch.delenv("MEETING_PREFER_CURRENT_SPEAKER", raising=False)
    monkeypatch.delenv("MEETING_SPEAKER_SENSITIVITY", raising=False)
    cfg = MeetingConfig()
    assert cfg.prefer_current_speaker is False
    assert cfg.speaker_sensitivity == 0.6


def test_diarization_env_overrides(monkeypatch):
    monkeypatch.setenv("MEETING_PREFER_CURRENT_SPEAKER", "true")
    monkeypatch.setenv("MEETING_SPEAKER_SENSITIVITY", "0.3")
    cfg = MeetingConfig()
    assert cfg.prefer_current_speaker is True
    assert cfg.speaker_sensitivity == 0.3


def test_speaker_sensitivity_none_sentinels_defer_to_speechmatics(monkeypatch):
    # '', 'auto', 'none' → None so _diar_kwargs omits it and Speechmatics uses its own default.
    monkeypatch.setenv("MEETING_SPEAKER_SENSITIVITY", "none")
    assert MeetingConfig().speaker_sensitivity is None
    monkeypatch.setenv("MEETING_SPEAKER_SENSITIVITY", "")
    assert MeetingConfig().speaker_sensitivity is None


def test_prefer_current_speaker_falsey_strings(monkeypatch):
    for falsey in ("0", "false", "no"):
        monkeypatch.setenv("MEETING_PREFER_CURRENT_SPEAKER", falsey)
        assert MeetingConfig().prefer_current_speaker is False
