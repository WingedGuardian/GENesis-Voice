"""Cloud session factory for the meeting bridge.

The bridge relays a phone's PCM to a cloud transcription session. The DEFAULT session reuses the
ambient bridge's proven ``ActiveSession`` (Speechmatics real-time streaming + live diarization +
live-updating ``.md``), so the meeting bridge inherits a deployed, E2E-tested path rather than a
new integration. The factory is injectable so the server unit-tests with a fake (no cloud SDK).

Imports of ``speechmatics.rt`` / ``numpy`` are deferred into the factory body — importing this
module (and therefore the server) stays dependency-light.
"""

from __future__ import annotations

from typing import Protocol


class MeetingSession(Protocol):
    """The slice of ``ActiveSession`` the server drives.

    OPTIONAL liveness attributes (read via ``getattr``, so a backend may omit them — omitting both
    just disables the transcript-idle close for that backend):

    - ``last_activity: float | None`` — monotonic ts of the last ASR speech evidence (non-empty
      partial or committed final); the primary transcript-idle signal.
    - ``turns: int`` — committed-turn count; the fallback signal.
    """

    path: str

    async def start(self) -> None: ...
    async def send_audio(self, frame: bytes) -> None: ...
    def add_marker(self) -> None: ...
    async def finalize(self) -> None: ...


def default_session_factory(cfg, source: str) -> MeetingSession:
    """Build an ``ActiveSession`` from the meeting config.

    We construct an ``AmbientConfig`` (which carries all the ``active_*`` fields ActiveSession
    reads) and override only what the meeting bridge owns: the output dir, the Speechmatics key
    path, and the diarization knobs. ``speaker_id=None`` keeps it positional (S1/S2) and the venv
    lean (no ONNX) — enrolled-name relabel is a follow-on.
    """
    import dataclasses

    from ambient_bridge.active_session import ActiveSession
    from ambient_bridge.config import AmbientConfig

    ambient_cfg = dataclasses.replace(
        AmbientConfig(),
        active_output_dir=cfg.output_dir,
        active_sm_key_path=cfg.sm_key_path,
        active_language=cfg.language,
        active_model=cfg.model,
        active_max_delay=cfg.max_delay,
        active_max_speakers=cfg.max_speakers,
        active_prefer_current_speaker=cfg.prefer_current_speaker,
        active_speaker_sensitivity=cfg.speaker_sensitivity,
        active_speaker_id_enabled=False,
    )
    return ActiveSession(ambient_cfg, source=source, speaker_id=None)
