"""Audio pipeline: PCM → resample 24→16k → sherpa VAD → Zipformer STT → store,
with DEFERRED speaker diarization on closed windows.

Design notes (from two architecture reviews + an E2E test that caught a straddle bug):
- ALL blocking/CPU work (soxr resample, sherpa VAD, sherpa STT) runs in ONE
  ``asyncio.to_thread`` call per incoming WS chunk, so the event loop is never blocked.
- The OfflineRecognizer is shared (stateless: fresh stream per utterance); the
  VoiceActivityDetector holds buffer state, so it is PER-CONNECTION.
- Resampling uses a STATEFUL ``soxr.ResampleStream`` (per connection) so the continuous
  diarization window has no per-frame phase discontinuities (one-shot ``soxr.resample``
  resets the filter each call and corrupts the speaker embedding).
- Diarization is DEFERRED: ``_process_sync`` (in a worker thread) only *builds* a closed
  window and RETURNS it; the async ``feed``/``flush`` (on the event loop) hand it to the
  server's bounded queue. ``asyncio.Queue`` is NOT thread-safe, so it is never touched
  from the worker thread.
- Utterances are bucketed into windows by their ACTUAL AUDIO SAMPLE POSITION
  (``vad.front.start`` is an absolute sample index in the resampled stream), and each
  window's raw audio is sliced to exactly span its utterances. Because the VAD emits an
  utterance only after its trailing silence, the *event* lags the *audio*; bucketing by
  sample position (not wall-clock) puts every utterance in the window that holds its
  audio — no straddle, offsets always ≥ 0.
"""
from __future__ import annotations

import asyncio
import glob
import logging
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

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


def _autodetect_embedding(model_dir: str) -> str:
    """Find a speaker-embedding ONNX model in models_dir. The zh-cn eres2net is VALIDATED
    on English (speaker embeddings are language-agnostic; same/diff margin ~0.58)."""
    for pat in ("*eres2net*16k*.onnx", "*campplus*16k*.onnx", "*eres2net*.onnx", "*campplus*.onnx"):
        c = sorted(glob.glob(f"{model_dir}/{pat}"))
        if c:
            return c[0]
    raise FileNotFoundError(f"no speaker-embedding model (e.g. *eres2net*16k*.onnx) in {model_dir}")


# --- shadow STT-quality instrumentation (Phase 0) -------------------------------------------
# Per-utterance quality signals logged into the row ``meta`` to characterize (and later gate)
# ASR hallucination — see ~/.genesis/output/specs/ambient-stt-quality-design.md. SHADOW-ONLY:
# never affects capture. The decode signals (per-token log-probs) and the VAD-segment audio
# energy are byproducts of work already done and are IRRECOVERABLE later (the audio never leaves
# the edge); text-derived features are computed offline from the stored text. Both extractors
# are guarded — instrumentation must never break the 24/7 capture path.
_SHADOW_VER = "p0.1"  # bump when this meta schema changes (lets offline analysis filter by gen)


def _asr_feats(result) -> dict:
    """Quality fingerprint of a sherpa decode. ``ys_log_probs`` (per-token log-probs, closer to
    0 = more confident) is stored RAW so the offline analysis can pick the discriminating
    statistic (mean / min / frac-below-θ) instead of pre-committing to one. lang/emotion/event
    are kept only when populated (this Zipformer usually leaves them empty)."""
    try:
        feats: dict = {
            "ys_log_probs": [round(float(x), 3) for x in result.ys_log_probs],
            "n_tokens": len(result.tokens),
        }
        for k in ("lang", "emotion", "event"):
            v = (getattr(result, k, "") or "").strip()
            if v:
                feats[k] = v
        return feats
    except Exception:  # noqa: BLE001 — instrumentation must never break capture
        logger.warning("asr feats extraction failed (capture unaffected)", exc_info=True)
        return {}


def _audio_feats(samples) -> dict:
    """Cheap acoustic fingerprint of the VAD segment — IRRECOVERABLE once the buffer is dropped.
    Garble from hiss/music tends to cluster at low energy; the offline analysis derives a
    pseudo-SNR from ``rms`` against a rolling floor. Token-rate is derivable offline from
    ``n_tokens`` and the row's ``duration_s``, so it is not stored here."""
    try:
        s = np.asarray(samples, dtype=np.float32)
        if s.size == 0:
            return {}
        feats = {
            "rms": round(float(np.sqrt(np.mean(s ** 2))), 5),
            "peak": round(float(np.max(np.abs(s))), 5),
        }
        if s.size > 1:
            feats["zcr"] = round(float(np.mean(np.abs(np.diff(np.sign(s))) > 0)), 4)
        return feats
    except Exception:  # noqa: BLE001 — instrumentation must never break capture
        logger.warning("audio feats failed (capture unaffected)", exc_info=True)
        return {}


@dataclass
class DiarWindow:
    """A closed window of continuous audio + the utterances that fall in it.
    Diarized off the ingest path by the server's worker."""

    raw: np.ndarray                                   # continuous 16k float32 audio
    spans: list[tuple[int, float, float]]             # (row_id, start_s, end_s), window-relative
    source: str                                       # connection id (labels not comparable across sources)
    window_idx: int = 0                               # assigned at submit (server-global, monotonic)


class AmbientEngine:
    """Holds the shared (expensive-to-load) recognizer; mints per-connection pipelines.
    Diarization wiring (submit callback + window size) is injected by the server so the
    pipeline can hand closed windows to the deferred diar queue."""

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
        self._diar_submit: Callable[[DiarWindow], Awaitable[None]] | None = None
        self._diar_window_samples = 0
        self._enroll_collect: Callable[[np.ndarray, float], None] | None = None
        logger.info("Zipformer recognizer loaded from %s", d)

    def enable_diarization(self, submit: Callable[[DiarWindow], Awaitable[None]], window_samples: int) -> None:
        self._diar_submit = submit
        self._diar_window_samples = window_samples

    def enable_enroll(self, collect: Callable[[np.ndarray, float], None]) -> None:
        """Wire a per-utterance collect callback for online (no-teardown) enrollment. The
        callback is a no-op unless an enroll session is active; it must be cheap + must not
        raise (the pipeline guards it)."""
        self._enroll_collect = collect

    def transcribe(self, samples) -> tuple[str, dict]:
        """Thread-safe STT. The recognizer is shared across connections, so serialize
        create_stream/decode_stream (avoids concurrent calls into the shared ONNX session).

        Returns ``(text, asr_feats)``: ``asr_feats`` is a SHADOW-ONLY quality fingerprint of the
        decode (see ``_asr_feats``); it is logged to row meta and never affects capture."""
        with self._rec_lock:
            stream = self._recognizer.create_stream()
            stream.accept_waveform(self._cfg.model_sample_rate, samples)
            self._recognizer.decode_stream(stream)
            result = stream.result
            return result.text.strip(), _asr_feats(result)

    def new_pipeline(self, source: str) -> AmbientPipeline:
        return AmbientPipeline(
            self._cfg, self._store, self, source,
            submit_window=self._diar_submit,
            diar_window_samples=self._diar_window_samples,
            collect_enroll=self._enroll_collect,
        )


class DiarizationEngine:
    """Shared offline speaker diarization (pyannote-seg + speaker embedding + FastClustering).
    Run DEFERRED on closed windows, serialized via a lock (CPU-heavy shared ONNX session).
    Model choice validated on English (zh-cn eres2net discriminates English; margin ~0.58)."""

    def __init__(self, cfg: AmbientConfig) -> None:
        emb = cfg.emb_model or _autodetect_embedding(cfg.models_dir)
        sd_cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=cfg.seg_model),
                num_threads=cfg.diar_num_threads,
            ),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=emb, num_threads=cfg.diar_num_threads),
            clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=cfg.diar_threshold),
            min_duration_on=0.3, min_duration_off=0.5,
        )
        if not sd_cfg.validate():
            raise RuntimeError("invalid diarization config (check seg/embedding model paths)")
        self._sd = sherpa_onnx.OfflineSpeakerDiarization(sd_cfg)
        self._lock = threading.Lock()
        self.sample_rate = self._sd.sample_rate
        logger.info("Diarization engine loaded (seg=%s emb=%s thr=%.2f threads=%d)",
                    cfg.seg_model, emb, cfg.diar_threshold, cfg.diar_num_threads)

    def process(self, samples: np.ndarray) -> list[tuple[float, float, int]]:
        """Run diarization on a continuous window → (start_s, end_s, speaker) per segment.
        Thread-safe (shared session serialized)."""
        with self._lock:
            res = self._sd.process(samples).sort_by_start_time()
            return [(s.start, s.end, s.speaker) for s in res]


class AmbientPipeline:
    def __init__(self, cfg, store, engine, source: str,
                 submit_window: Callable[[DiarWindow], Awaitable[None]] | None = None,
                 diar_window_samples: int = 0,
                 collect_enroll: Callable[[np.ndarray, float], None] | None = None) -> None:
        self._cfg = cfg
        self._store = store
        self._engine = engine
        self._source = source
        self._submit_window = submit_window
        self._diar_on = submit_window is not None
        self._diar_window_samples = diar_window_samples
        self._collect_enroll = collect_enroll
        # Stateful resampler → phase-continuous audio for the diar window.
        if cfg.input_sample_rate != cfg.model_sample_rate:
            self._resampler = soxr.ResampleStream(
                cfg.input_sample_rate, cfg.model_sample_rate, 1, dtype="float32",
            )
        else:
            self._resampler = None
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
        # Diar window (sample-exact). Rolling raw audio kept as chunks (concatenated only
        # at close); absolute sample bookkeeping lets utterances bucket by audio position.
        self._raw_chunks: list[np.ndarray] = []
        self._raw_len = 0          # samples held in _raw_chunks
        self._base = 0             # absolute resampled-sample index of _raw_chunks[0][0]
        self._win_utts: list[tuple[int, int, int]] = []  # (row_id, abs_start, abs_end)
        self._win_first = 0        # abs start sample of the window's first utterance
        self._fed_total = 0        # cumulative resampled samples (to sanity-check vad.front.start)
        self._diar_checked = False  # one-time guard that seg.start is an absolute index

    async def feed(self, pcm_bytes: bytes) -> int:
        """Accept one raw-PCM binary frame. Returns #utterances stored from it.
        Submits a closed diar window (if any) from the event loop — never from the thread."""
        stored, window = await asyncio.to_thread(self._process_sync, pcm_bytes, False)
        if window is not None and self._submit_window is not None:
            await self._submit_window(window)
        return stored

    async def flush(self) -> int:
        """Drain any buffered partial utterance on disconnect + close the final window."""
        stored, window = await asyncio.to_thread(self._process_sync, b"", True)
        if window is not None and self._submit_window is not None:
            await self._submit_window(window)
        return stored

    def _resample(self, pcm_bytes: bytes) -> np.ndarray:
        raw = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        if self._resampler is not None:
            return self._resampler.resample_chunk(raw)
        return raw

    def _close_window(self) -> DiarWindow | None:
        """Slice the raw audio to exactly span the window's utterances; trim consumed audio."""
        last_end = self._win_utts[-1][2]
        full = np.concatenate(self._raw_chunks) if self._raw_chunks else np.empty(0, dtype=np.float32)
        lo = max(0, self._win_first - self._base)
        hi = max(lo, last_end - self._base)
        raw = full[lo:hi].copy()
        spans = [
            (rid, (s - self._win_first) / self._cfg.model_sample_rate,
             (e - self._win_first) / self._cfg.model_sample_rate)
            for (rid, s, e) in self._win_utts
        ]
        # Keep only audio at/after last_end for the next window; advance the base.
        remainder = full[hi:]
        self._raw_chunks = [remainder] if remainder.size else []
        self._raw_len = int(remainder.size)
        self._base = last_end
        self._win_utts = []
        self._win_first = 0
        return DiarWindow(raw=raw, spans=spans, source=self._source) if raw.size else None

    def _cap_raw(self) -> None:
        """Bound memory if no window closes for a long time (e.g. a single speaker with
        <2 utterances): drop the oldest audio down to one window's worth."""
        cap = 2 * self._diar_window_samples
        if cap <= 0 or self._raw_len <= cap:
            return
        full = np.concatenate(self._raw_chunks)
        drop = self._raw_len - self._diar_window_samples
        rem = full[drop:]
        self._raw_chunks = [rem] if rem.size else []
        self._raw_len = int(rem.size)
        self._base += drop
        if self._win_utts and self._win_first < self._base:
            # The open window's earliest audio was dropped — abandon the partial window.
            self._win_utts = []
            self._win_first = 0

    def _process_sync(self, pcm_bytes: bytes, flush: bool) -> tuple[int, DiarWindow | None]:
        if pcm_bytes:
            pcm = self._resample(pcm_bytes)
            self._buf = np.concatenate([self._buf, pcm])
            if self._diar_on:
                self._raw_chunks.append(pcm)
                self._raw_len += len(pcm)
                self._fed_total += len(pcm)

        while len(self._buf) >= self._window:
            self._vad.accept_waveform(self._buf[: self._window])
            self._buf = self._buf[self._window :]
        if flush:
            self._vad.flush()

        sr = self._cfg.model_sample_rate
        stored = 0
        closed: DiarWindow | None = None
        while not self._vad.empty():
            seg = self._vad.front
            abs_start = int(seg.start)
            # Own the samples with a copy BEFORE pop(): the VAD may recycle its internal buffer
            # on pop, so this makes both STT and the enroll tap safe by construction (not by
            # assuming the binding returns an independent array).
            samples = np.asarray(seg.samples, dtype=np.float32).copy()
            self._vad.pop()
            dur = len(samples) / sr
            # Online-enrollment tap: a no-op unless an enroll session is active. Guarded so a
            # bug here can NEVER break capture. Runs before STT so it captures even empty-text
            # utterances (enrollment wants the voice, not the transcript). `samples` is already
            # an owned copy, so the collector can keep the reference directly.
            if self._collect_enroll is not None:
                try:
                    self._collect_enroll(samples, dur)
                except Exception:  # noqa: BLE001
                    logger.warning("enroll collect failed (capture unaffected)", exc_info=True)
            text, asr_feats = self._engine.transcribe(samples)
            if not text:
                continue
            meta = {"asr": "sherpa-zipformer", "shadow_ver": _SHADOW_VER}
            if asr_feats:
                meta["asr_feats"] = asr_feats
            audio = _audio_feats(samples)
            if audio:
                meta["audio"] = audio
            row_id = self._store.insert(
                text=text, duration_s=round(dur, 2), source=self._source, meta=meta,
            )
            self.utterances += 1
            stored += 1
            logger.info("[%s] (%.1fs) %s", self._source, dur, text)
            if self._diar_on:
                abs_end = abs_start + len(samples)
                # One-time guard: seg.start must be an absolute sample index in the
                # resampled stream (the whole windowing rests on it). If not, the slice
                # math would silently use wrong audio — disable diar instead.
                if not self._diar_checked:
                    self._diar_checked = True
                    if abs_start > self._fed_total:
                        logger.error(
                            "vad.front.start=%d > samples fed=%d — seg.start is not an "
                            "absolute index; disabling diarization (capture continues)",
                            abs_start, self._fed_total,
                        )
                        self._diar_on = False
                        self._raw_chunks = []
                        self._raw_len = 0
                        self._win_utts = []
                        continue
                if not self._win_utts:
                    self._win_first = abs_start
                self._win_utts.append((row_id, abs_start, abs_end))
                # Close once the window's utterance span reaches the target. Closing right
                # after an utterance (a silence boundary) means no later utterance's audio
                # belongs to this window.
                if closed is None and len(self._win_utts) >= 2 and (abs_end - self._win_first) >= self._diar_window_samples:
                    closed = self._close_window()

        if flush and self._diar_on and closed is None and len(self._win_utts) >= 1:
            # On disconnect, label even a lone trailing utterance (→ w?:0/1) rather than
            # leaving it NULL; the mid-stream close above keeps the >=2 requirement.
            closed = self._close_window()
        if self._diar_on:
            self._cap_raw()
        return stored, closed
