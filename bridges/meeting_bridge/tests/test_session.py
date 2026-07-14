"""Factory-wiring tests for the default meeting session.

Proves the meeting bridge's own config (diarization knobs, output dir, backend swap) actually
reaches the built session's ``AmbientConfig``. ``ActiveSession`` pulls in ``speechmatics.rt`` /
``numpy`` at import — heavy and absent from the test venv — so we stub JUST that class via
``sys.modules`` and let the factory build against the REAL ``ambient_bridge.config.AmbientConfig``.
That real config is the point: ``dataclasses.replace`` raises if a mapped field name is wrong, so a
green test is proof the ``active_*`` fields exist and receive the meeting values.
"""

import sys
import types

from meeting_bridge.config import MeetingConfig


def _build_with_stub_active_session(monkeypatch, cfg, source="phone"):
    captured = {}

    class _FakeActiveSession:
        def __init__(self, ambient_cfg, source, speaker_id=None):
            captured["cfg"] = ambient_cfg
            captured["source"] = source
            captured["speaker_id"] = speaker_id
            self.path = "/tmp/fake.md"

    fake_mod = types.ModuleType("ambient_bridge.active_session")
    fake_mod.ActiveSession = _FakeActiveSession
    monkeypatch.setitem(sys.modules, "ambient_bridge.active_session", fake_mod)

    from meeting_bridge.session import default_session_factory

    session = default_session_factory(cfg, source=source)
    return session, captured


def test_factory_maps_meeting_diarization_defaults(monkeypatch):
    for var in ("MEETING_PREFER_CURRENT_SPEAKER", "MEETING_SPEAKER_SENSITIVITY", "MEETING_MAX_SPEAKERS"):
        monkeypatch.delenv(var, raising=False)
    session, captured = _build_with_stub_active_session(monkeypatch, MeetingConfig())
    cfg = captured["cfg"]
    # Meeting-tuned diarization reached the ambient config the cloud session is built from.
    assert cfg.active_prefer_current_speaker is False
    assert cfg.active_speaker_sensitivity == 0.6
    assert cfg.active_max_speakers is None  # auto-detect
    # Positional diarization, ONNX-free (enrolled-name relabel is a follow-on).
    assert cfg.active_speaker_id_enabled is False
    assert captured["speaker_id"] is None
    assert captured["source"] == "phone"
    assert session.path == "/tmp/fake.md"


def test_factory_passes_through_overrides(monkeypatch):
    cfg = MeetingConfig(
        prefer_current_speaker=True,
        speaker_sensitivity=0.25,
        max_speakers=4,
        output_dir="/tmp/mtg-out",
        model="standard",
    )
    _, captured = _build_with_stub_active_session(monkeypatch, cfg)
    ac = captured["cfg"]
    assert ac.active_prefer_current_speaker is True
    assert ac.active_speaker_sensitivity == 0.25
    assert ac.active_max_speakers == 4
    assert ac.active_output_dir == "/tmp/mtg-out"
    assert ac.active_model == "standard"


def test_factory_maps_none_sensitivity(monkeypatch):
    # None must survive to the ambient config so _diar_kwargs omits it (Speechmatics default).
    cfg = MeetingConfig(speaker_sensitivity=None)
    _, captured = _build_with_stub_active_session(monkeypatch, cfg)
    assert captured["cfg"].active_speaker_sensitivity is None
