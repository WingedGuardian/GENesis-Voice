"""Audio pipeline: PCM → resample 24→16k → sherpa VAD → Zipformer STT → store.

Design notes (from architecture review):
- ALL blocking/CPU work (soxr resample, sherpa VAD, sherpa STT) runs in ONE
  ``asyncio.to_thread`` call per incoming WS chunk, so the async WS server's
  event loop is never blocked and one thread-hop covers the whole chunk.
- The OfflineRecognizer is shared (stateless: a fresh stream per utterance);
  the VoiceActivityDetector holds buffer state, so it is PER-CONNECTION.
- Diarization is NOT wired here yet (next increment); the store schema already
  carries a (window-prefixed) speaker_label for it.
"""
from __future__ import annotations

import asyncio
import glob
import logging
import threading

import numpy as np
import sherpa_onnx
import soxr

from .config import AmbientConfig
from .store import AmbientStore

logger = logging.getLogger("ambient.pipeline")


def _pick(model_dir: str, kind: str) -> str:
    cands = sorted(glob.glob(f"{model_dir}/{kind}*.onnx"))
    int8 = [c for c in cands if "int8" in c]
    if not (int8 or cands):
        raise FileNotFoundError(f"no {kind}*.onnx in {model_dir}")
    return (int8 or cands)[0]


class AmbientEngine:
    """Holds the shared (expensive-to-load) recognizer; mints per-connection pipelines."""

    def __init__(self, cfg: AmbientConfig, store: AmbientStore) -> None:
        self._cfg = cfg
        self._store = store
        d = cfg.zipformer_dir
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=_pick(d, "encoder"), decoder=_pick(d, "decoder"),
            joiner=_pick(d, "joiner"), tokens=f"{d}/tokens.txt",
            num_threads=cfg.num_threads, decoding_method="greedy_search",
        )
        self._rec_lock = threading.Lock()
        logger.info("Zipformer recognizer loaded from %s", d)

    def transcribe(self, samples) -> str:
        """Thread-safe STT. The recognizer is shared across connections, so
        serialize create_stream/decode_stream (one device → near-zero contention;
        avoids concurrent calls into the shared ONNX session)."""
        with self._rec_lock:
            stream = self._recognizer.create_stream()
            stream.accept_waveform(self._cfg.model_sample_rate, samples)
            self._recognizer.decode_stream(stream)
            return stream.result.text.strip()

    def new_pipeline(self, source: str) -> AmbientPipeline:
        return AmbientPipeline(self._cfg, self._store, self, source)


class AmbientPipeline:
    def __init__(self, cfg, store, engine, source: str) -> None:
        self._cfg = cfg
        self._store = store
        self._engine = engine
        self._source = source
        vad_cfg = sherpa_onnx.VadModelConfig()
        vad_cfg.silero_vad.model = cfg.silero_vad
        vad_cfg.silero_vad.min_silence_duration = cfg.vad_min_silence_s
        vad_cfg.sample_rate = cfg.model_sample_rate
        self._window = vad_cfg.silero_vad.window_size
        self._vad = sherpa_onnx.VoiceActivityDetector(
            vad_cfg, buffer_size_in_seconds=cfg.vad_buffer_seconds,
        )
        self._buf = np.empty(0, dtype=np.float32)
        self.utterances = 0

    async def feed(self, pcm_bytes: bytes) -> int:
        """Accept one raw-PCM binary frame. Returns #utterances stored from it."""
        return await asyncio.to_thread(self._process_sync, pcm_bytes, False)

    async def flush(self) -> int:
        """Drain any buffered partial utterance on disconnect."""
        return await asyncio.to_thread(self._process_sync, b"", True)

    def _process_sync(self, pcm_bytes: bytes, flush: bool) -> int:
        if pcm_bytes:
            pcm = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            if self._cfg.input_sample_rate != self._cfg.model_sample_rate:
                pcm = soxr.resample(
                    pcm, self._cfg.input_sample_rate, self._cfg.model_sample_rate,
                ).astype(np.float32)
            self._buf = np.concatenate([self._buf, pcm])

        while len(self._buf) >= self._window:
            self._vad.accept_waveform(self._buf[: self._window])
            self._buf = self._buf[self._window :]
        if flush:
            self._vad.flush()

        stored = 0
        while not self._vad.empty():
            samples = self._vad.front.samples
            self._vad.pop()
            dur = len(samples) / self._cfg.model_sample_rate
            text = self._engine.transcribe(samples)
            if not text:
                continue
            self._store.insert(
                text=text, duration_s=round(dur, 2), source=self._source,
                meta={"asr": "sherpa-zipformer"},
            )
            self.utterances += 1
            stored += 1
            logger.info("[%s] (%.1fs) %s", self._source, dur, text)
        return stored
