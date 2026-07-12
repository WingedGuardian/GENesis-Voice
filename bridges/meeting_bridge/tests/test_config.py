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
