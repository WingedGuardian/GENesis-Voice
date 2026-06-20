"""Enroll a named speaker's voiceprint into the ambient speaker registry.

General + reusable (the user first, then family/guests). Two input modes:

  # (a) ingest existing 16 kHz wavs (e.g. a Stage-0 capture set) — no re-recording:
  python -m ambient_bridge.enroll --name user --from-dir ~/spike_audio_s0/enroll

  # (b) live capture through the device (bridge must be STOPPED so :8765 is free;
  #     toggle the device's ambient mode so it connects here; then speak):
  python -m ambient_bridge.enroll --name alice

Writes the L2-normalized centroid voiceprint to the registry JSON
(``AMBIENT_SPEAKER_REGISTRY``, default ~/ambient_speaker_registry.json). The ambient
bridge loads it at startup and tags matching utterances ``is_user`` (or, generally,
the named speaker in a later stage). Run on the bridge VM (sherpa-onnx is edge-only).
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import logging
import os
import signal
import sys

import numpy as np
import sherpa_onnx
import soundfile as sf
import soxr
import websockets

from .config import load_config
from .pipeline import _autodetect_embedding
from .speaker_id import SpeakerIDRegistry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ambient.enroll")

TARGET_SR = 16000


def _load_16k_mono(path: str) -> np.ndarray:
    """Read any wav/flac as float32 mono @ 16 kHz (phase-continuous resample)."""
    audio, sr = sf.read(path, dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != TARGET_SR:
        audio = soxr.resample(audio, sr, TARGET_SR)
    return np.ascontiguousarray(audio, dtype=np.float32)


def _from_dir(directory: str) -> list[np.ndarray]:
    paths = sorted(glob.glob(os.path.join(os.path.expanduser(directory), "*.wav")))
    if not paths:
        raise SystemExit(f"no .wav files in {directory}")
    samples = [_load_16k_mono(p) for p in paths]
    log.info("loaded %d wavs from %s", len(samples), directory)
    return samples


async def _capture_live(cfg, *, target_s: float, min_dur: float, port: int) -> list[np.ndarray]:
    """Run a throwaway WS capture server (same wire contract as the bridge), VAD-segment
    the device stream, and collect utterances >= min_dur until ~target_s total."""
    collected: list[np.ndarray] = []
    total = 0.0
    done = asyncio.Event()

    vad_cfg = sherpa_onnx.VadModelConfig()
    vad_cfg.silero_vad.model = cfg.silero_vad
    vad_cfg.silero_vad.min_silence_duration = cfg.vad_min_silence_s
    vad_cfg.sample_rate = TARGET_SR

    async def handler(ws) -> None:
        nonlocal total
        rs = (soxr.ResampleStream(cfg.input_sample_rate, TARGET_SR, 1, dtype="float32")
              if cfg.input_sample_rate != TARGET_SR else None)
        vad = sherpa_onnx.VoiceActivityDetector(vad_cfg, buffer_size_in_seconds=cfg.vad_buffer_seconds)
        win = vad_cfg.silero_vad.window_size
        buf = np.empty(0, dtype=np.float32)
        log.info("device connected — speak now")
        async for msg in ws:
            if not isinstance(msg, bytes):
                continue
            raw = np.frombuffer(msg, dtype=np.int16).astype(np.float32) / 32768.0
            x = rs.resample_chunk(raw) if rs is not None else raw
            buf = np.concatenate([buf, x])
            while len(buf) >= win:
                vad.accept_waveform(buf[:win])
                buf = buf[win:]
            while not vad.empty():
                seg = np.array(vad.front.samples, dtype=np.float32)
                vad.pop()
                dur = len(seg) / TARGET_SR
                if dur >= min_dur:
                    collected.append(seg)
                    total += dur
                    log.info("  captured %.1fs utt (%.0f/%.0fs collected)", dur, total, target_s)
                    if total >= target_s:
                        done.set()
                        return  # enough collected — close this connection, stop buffering

    async with websockets.serve(handler, "0.0.0.0", port, max_size=None, ping_interval=None):
        log.info("capture server on ws://0.0.0.0:%d/ — need ~%.0fs of >=%.1fs utterances",
                 port, target_s, min_dur)
        await done.wait()
    return collected


def main() -> int:
    cfg = load_config()
    ap = argparse.ArgumentParser(description="Enroll a speaker voiceprint")
    ap.add_argument("--name", default=cfg.user_speaker_name, help="speaker name (default: the user)")
    ap.add_argument("--from-dir", help="enroll from existing 16k wavs in this dir (no live capture)")
    ap.add_argument("--model", default=cfg.speaker_id_model, help="embedding ONNX (default: autodetect)")
    ap.add_argument("--registry", default=cfg.speaker_registry_path, help="registry JSON path")
    ap.add_argument("--target-s", type=float, default=30.0, help="live: seconds of speech to collect")
    ap.add_argument("--min-dur", type=float, default=1.0, help="live: min utterance seconds to keep")
    ap.add_argument("--port", type=int, default=cfg.port, help="live: WS port (bridge must be stopped)")
    args = ap.parse_args()

    model = args.model or _autodetect_embedding(cfg.models_dir)
    registry = SpeakerIDRegistry(
        model, persist_path=args.registry,
        num_threads=cfg.diar_num_threads, user_name=cfg.user_speaker_name,
    )

    if args.from_dir:
        samples = _from_dir(args.from_dir)
    else:
        loop = asyncio.new_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, loop.stop)
            except NotImplementedError:
                pass
        try:
            samples = loop.run_until_complete(
                _capture_live(cfg, target_s=args.target_s, min_dur=args.min_dur, port=args.port),
            )
        finally:
            loop.close()

    if not samples:
        log.error("no audio collected — nothing enrolled")
        return 1
    n = registry.enroll(args.name, samples)
    log.info("✓ enrolled %r from %d usable clips → %s (speakers: %s)",
             args.name, n, args.registry, registry.names())
    return 0


if __name__ == "__main__":
    sys.exit(main())
