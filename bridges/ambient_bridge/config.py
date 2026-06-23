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
    # TCP keep-alive on accepted client sockets: detect a silently-dead (half-open) Voice PE
    # in ~idle + intvl*cnt seconds instead of the OS default (~2h). Needed because WS server
    # PINGs are OFF (the device rejects them — see server.serve()), so without this a dead
    # socket lingers ESTAB and inflates active_connections.
    keepalive_idle_s: int = field(default_factory=lambda: int(_env("AMBIENT_KEEPALIVE_IDLE_S", "120")))
    keepalive_intvl_s: int = field(default_factory=lambda: int(_env("AMBIENT_KEEPALIVE_INTVL_S", "10")))
    keepalive_cnt: int = field(default_factory=lambda: int(_env("AMBIENT_KEEPALIVE_CNT", "3")))

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

    # --- diarization (Stage-1b, additive) ---
    # Speaker diarization runs DEFERRED on closed windows; if models are missing or
    # init fails, the service runs capture-only with speaker_label NULL.
    diar_enabled: bool = field(default_factory=lambda: _env("AMBIENT_DIAR_ENABLED", "1") not in ("0", "false", "False", "no", ""))
    # pyannote segmentation model; embedding model (empty → autodetect *eres2net*16k* in models_dir).
    # The zh-cn eres2net is VALIDATED on English (speaker embeddings are language-agnostic).
    seg_model: str = field(default_factory=lambda: _env("AMBIENT_SEG_MODEL", os.path.expanduser("~/models/sherpa-onnx-pyannote-segmentation-3-0/model.onnx")))
    emb_model: str = field(default_factory=lambda: _env("AMBIENT_EMB_MODEL", ""))
    diar_threshold: float = field(default_factory=lambda: float(_env("AMBIENT_DIAR_THRESHOLD", "0.7")))
    # Window of CONTINUOUS audio to diarize together (seconds of accumulated stream).
    diar_window_s: float = field(default_factory=lambda: float(_env("AMBIENT_DIAR_WINDOW_S", "60")))
    diar_queue_max: int = field(default_factory=lambda: int(_env("AMBIENT_DIAR_QUEUE_MAX", "4")))
    # Diar shares the CPU with STT; keep below (cores - stt threads) on small boxes.
    diar_num_threads: int = field(default_factory=lambda: int(_env("AMBIENT_DIAR_NUM_THREADS", "2")))

    # --- speaker identification (Stage-A: per-utterance is_user tagging) ---
    # Tags each row is_user (1=user / 0=other / NULL=no verdict) via a speaker-embedding
    # match to an enrolled voiceprint. Runs in the diar worker (reuses cluster labels to
    # recover short utts). Disabled, or no registry file → is_user stays NULL (capture
    # unaffected). threshold 0.35 + min_embed_s 3.0 are the Stage-0 16k gate results.
    speaker_id_enabled: bool = field(default_factory=lambda: _env("AMBIENT_SPEAKER_ID_ENABLED", "1") not in ("0", "false", "False", "no", ""))
    speaker_id_model: str = field(default_factory=lambda: _env("AMBIENT_SPEAKER_ID_MODEL", ""))  # empty → autodetect *eres2net*16k*
    user_verify_threshold: float = field(default_factory=lambda: float(_env("AMBIENT_USER_VERIFY_THRESHOLD", "0.35")))
    # min utterance seconds for a DIRECT per-utterance verdict; shorter utts get a verdict
    # only via cluster-centroid aggregation (Stage-0: clean separation holds ≥3s).
    min_embed_s: float = field(default_factory=lambda: float(_env("AMBIENT_MIN_EMBED_S", "3.0")))
    speaker_registry_path: str = field(default_factory=lambda: _env("AMBIENT_SPEAKER_REGISTRY", os.path.expanduser("~/ambient_speaker_registry.json")))
    user_speaker_name: str = field(default_factory=lambda: _env("AMBIENT_USER_SPEAKER_NAME", "user"))

    # --- online enrollment (no-teardown: enroll a voiceprint while the bridge keeps running) ---
    # The bridge polls request_path; on a fresh request it collects live VAD utterances
    # (>= enroll_min_dur each) until enroll_target_s total, builds the voiceprint, writes
    # result_path. Paths default to ~/ (OUTSIDE the repo — biometric, never commit).
    enroll_request_path: str = field(default_factory=lambda: _env("AMBIENT_ENROLL_REQUEST", os.path.expanduser("~/ambient_enroll_request.json")))
    enroll_result_path: str = field(default_factory=lambda: _env("AMBIENT_ENROLL_RESULT", os.path.expanduser("~/ambient_enroll_result.json")))
    enroll_check_interval_s: float = field(default_factory=lambda: float(_env("AMBIENT_ENROLL_CHECK_S", "2.0")))
    enroll_min_dur_s: float = field(default_factory=lambda: float(_env("AMBIENT_ENROLL_MIN_DUR_S", "1.0")))
    enroll_target_s: float = field(default_factory=lambda: float(_env("AMBIENT_ENROLL_TARGET_S", "30.0")))
    enroll_max_wait_s: float = field(default_factory=lambda: float(_env("AMBIENT_ENROLL_MAX_WAIT_S", "120.0")))

    # --- ACTIVE mode (cloud Speechmatics; default is passive/local) ---
    # Mode is set per-device via the HTTP control endpoint; passive stays the DEFAULT so a
    # dropped/late POST fails safe to LOCAL. Active opens a Speechmatics realtime session that
    # writes a live diarized transcript to active_output_dir (DISTINCT from ambient.db).
    control_http_port: int = field(default_factory=lambda: int(_env("AMBIENT_CONTROL_HTTP_PORT", "8767")))
    active_sm_key_path: str = field(default_factory=lambda: _env("AMBIENT_ACTIVE_SM_KEY_PATH", os.path.expanduser("~/.ambient-active/speechmatics.key")))
    active_output_dir: str = field(default_factory=lambda: _env("AMBIENT_ACTIVE_OUTPUT_DIR", os.path.expanduser("~/listen-sessions")))
    active_language: str = field(default_factory=lambda: _env("AMBIENT_ACTIVE_LANGUAGE", "en"))
    active_model: str = field(default_factory=lambda: _env("AMBIENT_ACTIVE_MODEL", "enhanced"))
    active_max_delay: float = field(default_factory=lambda: float(_env("AMBIENT_ACTIVE_MAX_DELAY", "1.0")))
    active_max_speakers: int = field(default_factory=lambda: int(_env("AMBIENT_ACTIVE_MAX_SPEAKERS", "2")))


def load_config() -> AmbientConfig:
    return AmbientConfig()
