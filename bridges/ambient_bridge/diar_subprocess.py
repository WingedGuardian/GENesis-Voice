"""Run the heavy, GIL-holding diarization + speaker-embedding in a SEPARATE PROCESS.

Root cause of the ambient WS pong-timeout churn (CONFIRMED 2026-06-25 by instrumentation):
sherpa's ``OfflineSpeakerDiarization.process()`` holds the Python GIL for its ENTIRE run (~50 s on
a busy multi-speaker window), which freezes the bridge's asyncio event loop — so the websockets
auto-PONG isn't sent and the device drops on its 10 s pong-timeout (code=1006), then reconnects.
``asyncio.to_thread`` cannot help: threads share one GIL. Running the work in a ``ProcessPoolExecutor``
child (``spawn`` → its own interpreter + GIL) keeps the event loop responsive.

Split of responsibilities (so active-mode + online-enroll keep using the PARENT's registry unchanged,
and there is no cross-process voiceprint state to keep in sync):

  CHILD (this module): PURE compute — diarize the window, embed each utterance span. Returns picklable
    results (segments + per-utterance embeddings). Holds its OWN engine + an embed-only registry; never
    reads/writes voiceprints or the DB.
  PARENT (server.py): keeps the voiceprint registry; does the cheap ``classify_window`` (cosine, no
    ONNX) + ALL SQLite writes (single writer).

Models load in the child (not the parent) — the parent no longer imports the diarization ONNX at all.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("ambient.diar_subprocess")

# Per-child globals, initialised ONCE by the ProcessPoolExecutor initializer. The parent process
# never touches these — it does not construct a DiarizationEngine at all anymore.
_ENGINE = None       # DiarizationEngine (pyannote seg + embedding + clustering)
_EMBEDDER = None     # SpeakerIDRegistry used ONLY to embed (voiceprints live in the parent)


def init_worker() -> None:
    """ProcessPoolExecutor ``initializer``: load the diarization engine + an embedding extractor in
    THIS child. Reads config from the environment (inherited across ``spawn``) via the bridge's own
    ``load_config()`` — so the child uses identical model paths/tuning to the parent. Runs once per
    child. Imports are LAZY so the parent process never imports/loads the sherpa diarization ONNX."""
    global _ENGINE, _EMBEDDER
    from .config import load_config
    from .pipeline import DiarizationEngine, _autodetect_embedding
    from .speaker_id import SpeakerIDRegistry

    from .ort_session import ort_provider

    cfg = load_config()
    _ENGINE = DiarizationEngine(cfg)
    # Embed-only registry: embed() needs just the ONNX model. Classification + voiceprints stay in
    # the parent, so this child copy never reads or mutates the persisted voiceprints (no sync).
    # Mirror the parent's guard — only load the embedder when speaker-ID is enabled, so a deployment
    # with diar on but speaker-ID off (or no embedding model on disk) doesn't crash the initializer
    # (which would surface as BrokenProcessPool on every window).
    if cfg.speaker_id_enabled:
        model = cfg.speaker_id_model or _autodetect_embedding(cfg.models_dir)
        _EMBEDDER = SpeakerIDRegistry(
            model,
            persist_path=cfg.speaker_registry_path,
            num_threads=cfg.diar_num_threads,
            user_name=cfg.user_speaker_name,
            provider=ort_provider(cfg),  # arena opt-out reaches the child's embed session too
        )
    logger.info("diar subprocess ready (engine loaded, embedder=%s)", _EMBEDDER is not None)


def process_window(raw, spans, sr, do_speaker_id):
    """Heavy GIL work, run in the child: diarize ``raw`` + embed each utterance span.

    Args (all picklable): ``raw`` continuous 16k float32 window audio; ``spans`` list of
    ``(row_id, start_s, end_s)`` (window-relative); ``sr`` sample rate; ``do_speaker_id`` whether to
    embed (False when no voiceprint is enrolled — avoids wasted embeds).

    Returns ``(segs, embeddings)``:
      ``segs``        list[(start_s, end_s, speaker_idx)] from OfflineSpeakerDiarization.
      ``embeddings``  list[np.ndarray|None] aligned to ``spans`` (one L2-normed vector per utterance,
                      None if too short), or None when ``do_speaker_id`` is False.
    The parent computes durations/clusters from ``spans`` + ``segs`` and classifies the embeddings
    against its voiceprints."""
    segs = _ENGINE.process(raw)
    embeddings = None
    if do_speaker_id and _EMBEDDER is not None:
        embeddings = [_EMBEDDER.embed(raw[int(s * sr):int(e * sr)]) for (_rid, s, e) in spans]
    _malloc_trim()  # between-window allocator hygiene (≤ ~1/min; see the docstring below)
    return segs, embeddings


def _malloc_trim() -> None:
    """Return whole free glibc pages to the OS after each window. With the ORT arena off,
    freed inference tensors land in glibc's free lists — this releases them instead of
    letting the child's RSS coast at its high-water mark. Best-effort: a non-glibc libc
    (or any ctypes failure) is a silent no-op; this must never break a window."""
    try:
        import ctypes
        ctypes.CDLL(None).malloc_trim(0)
    except Exception:  # noqa: BLE001 — hygiene, never worth crashing diarization for
        pass
