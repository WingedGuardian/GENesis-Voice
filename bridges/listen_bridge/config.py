"""Listen-bridge configuration — env-driven, standalone (no genesis imports).

Runs in its OWN venv on the edge VM. Deps: speechmatics-rt, websockets, stdlib.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _flag(name: str, default: str) -> bool:
    return _env(name, default) not in ("0", "false", "False", "no", "")


@dataclass(frozen=True)
class ListenConfig:
    # --- WebSocket ingest (device streams its ambient path, repointed to this port) ---
    host: str = field(default_factory=lambda: _env("LISTEN_WS_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(_env("LISTEN_WS_PORT", "8766")))
    # The Voice PE ambient path streams 16 kHz mono 16-bit PCM — exactly pcm_s16le@16000,
    # what Speechmatics consumes — so frames forward with NO resampling.
    sample_rate: int = 16000

    # TCP keep-alive on accepted sockets: reap a silently-dead (half-open) Voice PE in
    # minutes instead of the ~2h OS default. WS server PINGs are OFF (the device rejects
    # them — same constraint as the ambient bridge), so this is the liveness backstop.
    keepalive_idle_s: int = field(default_factory=lambda: int(_env("LISTEN_KEEPALIVE_IDLE_S", "120")))
    keepalive_intvl_s: int = field(default_factory=lambda: int(_env("LISTEN_KEEPALIVE_INTVL_S", "10")))
    keepalive_cnt: int = field(default_factory=lambda: int(_env("LISTEN_KEEPALIVE_CNT", "3")))

    # --- Speechmatics realtime ---
    # Key file (chmod 600), NOT an env var on disk. Outside the repo. Biometric-adjacent.
    api_key_path: str = field(default_factory=lambda: _env(
        "LISTEN_SM_KEY_PATH", os.path.expanduser("~/.listen-bridge/speechmatics.key")))
    connection_url: str = field(default_factory=lambda: _env("LISTEN_SM_URL", ""))  # empty → SDK default
    language: str = field(default_factory=lambda: _env("LISTEN_SM_LANGUAGE", "en"))
    model: str = field(default_factory=lambda: _env("LISTEN_SM_MODEL", "enhanced"))
    # max_delay (s) trades latency for final-transcript context; 0.7–2.0 is the useful range.
    max_delay: float = field(default_factory=lambda: float(_env("LISTEN_SM_MAX_DELAY", "1.0")))
    max_speakers: int = field(default_factory=lambda: int(_env("LISTEN_SM_MAX_SPEAKERS", "2")))
    enable_partials: bool = field(default_factory=lambda: _flag("LISTEN_SM_PARTIALS", "1"))

    # --- output ---
    # Live-updating transcript files, one per session — near the ambient store but a
    # DISTINCT dir. Outside the repo (personal speech; never commit).
    output_dir: str = field(default_factory=lambda: _env(
        "LISTEN_OUTPUT_DIR", os.path.expanduser("~/listen-sessions")))
    health_path: str = field(default_factory=lambda: _env(
        "LISTEN_HEALTH", os.path.expanduser("~/listen_health.json")))


def load_config() -> ListenConfig:
    return ListenConfig()
