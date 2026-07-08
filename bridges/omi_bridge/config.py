"""OMI bridge configuration — env-driven, standalone (no genesis imports).

Runs in its OWN venv on the Voice Edge box. Depends only on aiohttp + stdlib.
Do NOT import ``genesis.*`` here. Mirrors ``ambient_bridge/config.py``'s env-helper style.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_list(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Comma-separated env -> tuple of trimmed, non-empty items (order preserved)."""
    v = os.environ.get(name)
    if v is None:
        return default
    return tuple(item.strip() for item in v.split(",") if item.strip())


@dataclass(frozen=True)
class OmiConfig:
    # --- HTTP ingress ---
    # Loopback by default: only the Tailscale Funnel (localhost) should reach this receiver,
    # never the LAN directly. The public door is Funnel, terminated on the edge.
    host: str = field(default_factory=lambda: _env("OMI_HTTP_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("OMI_HTTP_PORT", "8788")))
    # Reject bodies larger than this (413) before parsing. A real batch is a few KB; 1 MiB is
    # already absurdly generous and caps a malformed/hostile payload.
    max_body_bytes: int = field(default_factory=lambda: int(_env("OMI_MAX_BODY_BYTES", str(1 << 20))))

    # --- auth ---
    # Secret path token: OMI's dev webhook can't send custom headers, so the token rides the URL
    # (`/omi/<token>/ingest`). `_PREVIOUS` allows zero-downtime rotation. uid allowlist is a second
    # factor; empty => uid check skipped (token is the primary gate).
    secret_token: str = field(default_factory=lambda: _env("OMI_INGEST_SECRET_TOKEN", ""))
    secret_token_previous: str = field(default_factory=lambda: _env("OMI_INGEST_SECRET_TOKEN_PREVIOUS", ""))
    uid_allowlist: tuple[str, ...] = field(default_factory=lambda: _env_list("OMI_UID_ALLOWLIST"))

    # --- storage (SHARED ambient.db with the ambient bridge) ---
    db_path: str = field(default_factory=lambda: _env("OMI_DB", os.path.expanduser("~/ambient.db")))
    state_db_path: str = field(default_factory=lambda: _env("OMI_STATE_DB", os.path.expanduser("~/omi_state.db")))
    ttl_hours: float = field(default_factory=lambda: float(_env("OMI_TTL_HOURS", "48")))
    row_ceiling: int = field(default_factory=lambda: int(_env("OMI_ROW_CEILING", "200000")))
    purge_interval_s: int = field(default_factory=lambda: int(_env("OMI_PURGE_INTERVAL_S", "3600")))

    # --- anchoring ---
    # Keep the per-uid wall-clock anchor while a batch's predicted time lands within this many
    # seconds of receipt; otherwise re-anchor (conversation rollover / downtime gap / device thrash).
    anchor_tolerance_s: float = field(default_factory=lambda: float(_env("OMI_ANCHOR_TOLERANCE_S", "60")))

    # --- health / observability ---
    health_path: str = field(default_factory=lambda: _env("OMI_HEALTH", os.path.expanduser("~/omi_health.json")))
    health_interval_s: int = field(default_factory=lambda: int(_env("OMI_HEALTH_INTERVAL_S", "60")))

    # ── auth helpers (pure) ────────────────────────────────────────────────
    def token_candidates(self) -> tuple[str, ...]:
        """Non-empty accepted tokens (current + previous), for a constant-time compare.

        Empty when no token is configured -> nothing authenticates (fail closed).
        """
        return tuple(t for t in (self.secret_token, self.secret_token_previous) if t)

    def uid_allowed(self, uid: str | None) -> bool:
        """True if ``uid`` passes the allowlist. Empty allowlist => open (token is the gate)."""
        if not self.uid_allowlist:
            return True
        return uid in self.uid_allowlist


def load_config() -> OmiConfig:
    return OmiConfig()
