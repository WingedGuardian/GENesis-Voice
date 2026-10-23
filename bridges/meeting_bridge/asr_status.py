"""What the bridge tells an ``asr_status_v1`` client about transcription, every couple of seconds.

Delivery receipts (``audio_ack_v1``) prove bytes arrived; they say nothing about whether those
bytes are being turned into text. This module answers that second question from evidence the
server already has, so a client can show an honest "transcribing" signal:

- ``idle``         no cloud session, and nothing is being suppressed: the room is quiet.
- ``dormant``      speech is arriving but deliberately NOT transcribed (a transcript-idle close
                   parked the connection until the room falls silent). Clients show it as a
                   warning, never as healthy.
- ``transcribing`` a session is open and the ASR has produced evidence recently, or the session is
                   younger than the stall window.
- ``stalled``      speech is being forwarded but the ASR has produced nothing for ``stall_s``.
- ``error``        a session is open but has no live cloud connection (for example a missing key).

Pure: time and state are passed in, so the policy is unit-tested without a socket.
"""

from __future__ import annotations

# Speech forwarded within this many seconds counts as "audio is flowing now".
RECENT_SPEECH_S = 5.0


def asr_state(
    *,
    session: object | None,
    session_opened_ts: float,
    dormant: bool,
    last_loud_ts: float,
    last_speech_sent_ts: float,
    now: float,
    stall_s: float,
) -> str:
    if session is None:
        if dormant and now - last_loud_ts < RECENT_SPEECH_S:
            return "dormant"
        return "idle"
    if getattr(session, "cloud_connected", True) is False:
        return "error"
    activity = getattr(session, "last_activity", None)
    evidence = max(activity or 0.0, session_opened_ts)
    speech_now = now - last_speech_sent_ts < RECENT_SPEECH_S
    if speech_now and now - evidence >= stall_s:
        return "stalled"
    return "transcribing"
