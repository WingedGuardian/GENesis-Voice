"""Unit tests for the asr_status policy (pure; no socket)."""

from types import SimpleNamespace

from meeting_bridge.asr_status import asr_state


def _state(**kw):
    base = dict(
        session=None,
        session_opened_ts=0.0,
        dormant=False,
        last_loud_ts=0.0,
        last_speech_sent_ts=0.0,
        now=100.0,
        stall_s=20.0,
    )
    base.update(kw)
    return asr_state(**base)


def test_idle_without_session():
    assert _state() == "idle"


def test_dormant_only_while_speech_is_arriving():
    assert _state(dormant=True, last_loud_ts=98.0) == "dormant"
    assert _state(dormant=True, last_loud_ts=50.0) == "idle"


def test_transcribing_with_recent_asr_activity():
    s = SimpleNamespace(last_activity=95.0)
    assert _state(session=s, session_opened_ts=10.0, last_speech_sent_ts=99.0) == "transcribing"


def test_young_session_is_not_stalled():
    s = SimpleNamespace(last_activity=None)
    assert _state(session=s, session_opened_ts=90.0, last_speech_sent_ts=99.0) == "transcribing"


def test_stalled_when_speech_flows_but_asr_is_silent():
    s = SimpleNamespace(last_activity=None)
    assert _state(session=s, session_opened_ts=50.0, last_speech_sent_ts=99.0) == "stalled"


def test_quiet_room_with_open_session_is_not_stalled():
    s = SimpleNamespace(last_activity=60.0)
    assert _state(session=s, session_opened_ts=50.0, last_speech_sent_ts=70.0) == "transcribing"


def test_error_when_session_has_no_cloud_connection():
    s = SimpleNamespace(last_activity=None, cloud_connected=False)
    assert _state(session=s, session_opened_ts=99.0) == "error"
