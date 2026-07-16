"""Meeting bridge configuration — env-driven, standalone (no genesis imports).

Runs in its OWN venv on the Voice Edge box. Mirrors ``ambient_bridge/config.py``'s env-helper
style and ``omi_bridge``'s path-token auth idiom. The diarization knobs map onto the ambient
bridge's ``ActiveSession`` (Speechmatics) at session-build time (see ``session.py``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int_or_none(name: str) -> int | None:
    v = os.environ.get(name)
    if v is None or not v.strip():
        return None
    return int(v)


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() not in ("0", "false", "no", "")


def _env_float_or_none(name: str, default: float | None) -> float | None:
    """Float env, or None to defer to Speechmatics' own default. '', 'auto', 'none' → None."""
    v = os.environ.get(name)
    if v is None:
        return default
    v = v.strip().lower()
    return None if v in ("", "auto", "none") else float(v)


@dataclass(frozen=True)
class MeetingConfig:
    # --- HTTP/WS ingress ---
    # Loopback by default: only the Tailscale Funnel (localhost) should reach this, never the LAN
    # directly. The public door is Funnel, terminated on the edge. One aiohttp app serves both the
    # capture page (GET /capture/<token>) and the audio WebSocket (GET /meeting/<token>) so the
    # phone opens a same-origin wss (no CORS / mixed-content) through a single Funnel port.
    host: str = field(default_factory=lambda: _env("MEETING_HTTP_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("MEETING_HTTP_PORT", "8790")))
    # Reject a single binary audio frame larger than this. Phone frames are a few KB of PCM;
    # 256 KiB is already absurdly generous and caps a malformed/hostile frame.
    max_frame_bytes: int = field(default_factory=lambda: int(_env("MEETING_MAX_FRAME_BYTES", str(1 << 18))))
    # WS heartbeat: aiohttp pings the phone every N seconds and force-closes on a missed pong, so a
    # silently-vanished peer (screen lock, tab kill, wifi handoff mid-capture) is detected in seconds
    # and the cloud session is finalized/closed — instead of leaking an open, billed Speechmatics
    # session with a stuck active-count. The phone browser honors WS ping/pong (unlike the Voice PE,
    # for which the ambient bridge disables pings). 0 disables (not recommended).
    ws_heartbeat_s: float = field(default_factory=lambda: float(_env("MEETING_WS_HEARTBEAT_S", "20")))

    # --- auth ---
    # Secret path token: the browser opens the wss with the token in the URL path
    # (`/meeting/<token>`) — it can't set custom WS headers. `_PREVIOUS` allows zero-downtime
    # rotation. Empty => nothing authenticates (fail closed).
    ingest_token: str = field(default_factory=lambda: _env("MEETING_INGEST_TOKEN", ""))
    ingest_token_previous: str = field(default_factory=lambda: _env("MEETING_INGEST_TOKEN_PREVIOUS", ""))

    # --- Speechmatics / diarization (mapped onto ActiveSession) ---
    # Reuse the ambient bridge's EXISTING Speechmatics key by default rather than provisioning a
    # second one — the meeting bridge shares the same edge box.
    sm_key_path: str = field(
        default_factory=lambda: _env("MEETING_SM_KEY_PATH", os.path.expanduser("~/.ambient-active/speechmatics.key"))
    )
    language: str = field(default_factory=lambda: _env("MEETING_LANGUAGE", "en"))
    model: str = field(default_factory=lambda: _env("MEETING_MODEL", "enhanced"))
    max_delay: float = field(default_factory=lambda: float(_env("MEETING_MAX_DELAY", "1.0")))
    # None => Speechmatics auto-detects the speaker count (no cap) — right for an unknown 1:1/small.
    max_speakers: int | None = field(default_factory=lambda: _env_int_or_none("MEETING_MAX_SPEAKERS"))
    # Diarization defaults are MEETING-tuned (multi-speaker → segment more), the opposite of the
    # ambient bridge's near-field anti-over-split posture (prefer_current=True, sensitivity=None).
    # A meeting session inherits from AmbientConfig (session.py) and would otherwise UNDER-segment —
    # long merged mega-turns, as seen on the first real ~2h capture. These override it:
    #   prefer_current_speaker=False → let Speechmatics re-attribute to a different speaker at a
    #     boundary instead of sticking with the current one (ambient uses True to avoid over-split).
    #   speaker_sensitivity=0.6 → lean toward splitting a new speaker (higher = more speakers).
    # First calibration from one noisy capture — env-tunable per room; dial down / set prefer=true
    # if a real meeting OVER-splits (spurious S-flips). None on sensitivity ⇒ Speechmatics default.
    prefer_current_speaker: bool = field(default_factory=lambda: _env_bool("MEETING_PREFER_CURRENT_SPEAKER", False))
    speaker_sensitivity: float | None = field(
        default_factory=lambda: _env_float_or_none("MEETING_SPEAKER_SENSITIVITY", 0.6)
    )

    # --- VAD-driven session lifecycle (energy gate over the incoming PCM) ---
    # Peak absolute int16 amplitude at/above which a frame counts as SPEECH. 0 DISABLES gating:
    # every frame is "speech", so one cloud session spans the whole connection (legacy behavior).
    # >0 turns on session-per-meeting — a session opens on speech, silent frames are dropped (never
    # billed by Speechmatics), and the session finalizes after `silence_close_s` of silence, so each
    # meeting lands in its own transcript. Default 0 (off) so a deploy is behavior-neutral; the value
    # is calibrated from the peak-amplitude instrumentation on a real capture before being enabled.
    vad_threshold: int = field(default_factory=lambda: int(_env("MEETING_VAD_THRESHOLD", "0")))
    # Keep forwarding this long after the last above-threshold frame, so a mid-utterance dip or the
    # tail of a word isn't clipped by the gate.
    vad_hangover_s: float = field(default_factory=lambda: float(_env("MEETING_VAD_HANGOVER_S", "0.4")))
    # Finalize the open cloud session after this much continuous silence (one meeting ends). MUST stay
    # below Speechmatics' real-time idle/no-audio timeout (measured at E2E) so we close deliberately
    # rather than the cloud dropping us. Small = more files (a long pause splits a meeting); large =
    # risks the idle timeout. Default 45s: above normal in-meeting pauses, below likely idle limits.
    silence_close_s: float = field(default_factory=lambda: float(_env("MEETING_SILENCE_CLOSE_S", "45")))
    # Finalize a session after this many seconds with NO new ASR SPEECH EVIDENCE — a non-empty
    # partial or a committed final (ActiveSession.last_activity; committed-turn count is the fallback
    # for backends without it) — even while mic energy stays above threshold. The energy gate can't
    # tell ambient room noise from speech, so in a noisy room (a real ~98min capture stayed open
    # ~80min past the meeting on background noise) a session runs — and bills Speechmatics — long
    # after the words stop. Partials matter: quiet/far-field speech often never commits a FINAL (a
    # real meeting was cut mid-way when gated on turns alone), but Speechmatics still emits partials
    # for it — so a hard-to-hear live meeting doesn't read as "over". After it fires the connection
    # goes DORMANT (no new session on mere noise) until the room falls silent for `silence_close_s`,
    # a marker is pressed, or the phone reconnects. Default 300s. Lower to trim the tail further,
    # raise if real meetings get split. 0 disables. Only active when the gate is armed
    # (vad_threshold>0) — legacy one-session mode is unaffected.
    transcript_idle_close_s: float = field(
        default_factory=lambda: float(_env("MEETING_TRANSCRIPT_IDLE_CLOSE_S", "300"))
    )
    # Periodic peak/pass/gate summary interval (seconds) for threshold calibration; 0 disables.
    vad_log_interval_s: float = field(default_factory=lambda: float(_env("MEETING_VAD_LOG_INTERVAL_S", "30")))

    # --- output ---
    # Distinct from the ambient bridge's ~/listen-sessions so meeting transcripts are isolated.
    output_dir: str = field(
        default_factory=lambda: _env("MEETING_OUTPUT_DIR", os.path.expanduser("~/meeting-sessions"))
    )

    # --- health / observability ---
    health_path: str = field(
        default_factory=lambda: _env("MEETING_HEALTH", os.path.expanduser("~/meeting_health.json"))
    )
    health_interval_s: int = field(default_factory=lambda: int(_env("MEETING_HEALTH_INTERVAL_S", "60")))

    # --- pluggable cloud backend ---
    # "module.path:callable" resolved at startup to the session factory (cfg, source) -> session.
    # Default = Speechmatics via the ambient bridge's ActiveSession; swappable (e.g. a future
    # Deepgram backend) without a code change. Flexibility > lock-in.
    session_factory_path: str = field(
        default_factory=lambda: _env("MEETING_SESSION_FACTORY", "meeting_bridge.session:default_session_factory")
    )

    # ── auth helpers (pure) ────────────────────────────────────────────────
    def token_candidates(self) -> tuple[str, ...]:
        """Non-empty accepted tokens (current + previous), for a constant-time compare.

        Empty when no token is configured -> nothing authenticates (fail closed).
        """
        return tuple(t for t in (self.ingest_token, self.ingest_token_previous) if t)


def load_config() -> MeetingConfig:
    return MeetingConfig()
