"""Ambient bridge configuration — env-driven, standalone (no genesis imports).

This service runs in its OWN venv on the bridge VM (`assistant1`), not in the
Genesis container, so it must depend only on: sherpa-onnx, websockets, soxr,
numpy, soundfile, stdlib. Do NOT import `genesis.*` here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class AmbientConfig:
    # --- WebSocket ingest ---
    host: str = field(default_factory=lambda: _env("AMBIENT_WS_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(_env("AMBIENT_WS_PORT", "8765")))
    # Sample rate the DEVICE streams (current firmware upsamples mic→24k for OpenAI).
    # If the ambient firmware is later changed to send raw 16k, set this to 16000
    # and the resample becomes a no-op.
    input_sample_rate: int = field(default_factory=lambda: int(_env("AMBIENT_INPUT_SR", "24000")))
    model_sample_rate: int = 16000  # sherpa VAD + Zipformer hard requirement

    # --- models (paths on the VM) ---
    models_dir: str = field(default_factory=lambda: _env("AMBIENT_MODELS_DIR", os.path.expanduser("~/models")))
    silero_vad: str = field(default_factory=lambda: _env("AMBIENT_SILERO_VAD", os.path.expanduser("~/models/silero_vad.onnx")))
    zipformer_dir: str = field(default_factory=lambda: _env("AMBIENT_ZIPFORMER_DIR", os.path.expanduser("~/models/sherpa-zip")))
    num_threads: int = field(default_factory=lambda: int(_env("AMBIENT_NUM_THREADS", "4")))

    # --- VAD tuning ---
    vad_min_silence_s: float = field(default_factory=lambda: float(_env("AMBIENT_VAD_MIN_SILENCE", "0.4")))
    vad_buffer_seconds: int = field(default_factory=lambda: int(_env("AMBIENT_VAD_BUFFER_S", "30")))

    # --- storage ---
    db_path: str = field(default_factory=lambda: _env("AMBIENT_DB", os.path.expanduser("~/ambient.db")))
    ttl_hours: float = field(default_factory=lambda: float(_env("AMBIENT_TTL_HOURS", "48")))
    row_ceiling: int = field(default_factory=lambda: int(_env("AMBIENT_ROW_CEILING", "200000")))
    purge_interval_s: int = field(default_factory=lambda: int(_env("AMBIENT_PURGE_INTERVAL_S", "3600")))

    # --- health / observability ---
    health_path: str = field(default_factory=lambda: _env("AMBIENT_HEALTH", os.path.expanduser("~/ambient_health.json")))
    health_interval_s: int = field(default_factory=lambda: int(_env("AMBIENT_HEALTH_INTERVAL_S", "60")))


def load_config() -> AmbientConfig:
    return AmbientConfig()
