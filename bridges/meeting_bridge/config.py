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
