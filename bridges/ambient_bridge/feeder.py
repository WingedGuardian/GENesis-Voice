"""Test feeder — replays a WAV over the ambient WS as the device would.

Sends raw 16-bit mono PCM at the configured input rate (24 kHz) in 100 ms binary
frames, then a JSON `disconnect`. Use to validate the service end-to-end before
the real firmware exists. It's also the canonical "did my latest code reach the
VM?" smoke test.

  python -m ambient_bridge.feeder --wav sample.wav [--url ws://127.0.0.1:8765] [--fast]
"""
from __future__ import annotations

import argparse
import asyncio
import json

import numpy as np
import soundfile as sf
import soxr
import websockets


async def feed(wav: str, url: str, in_sr: int, fast: bool) -> None:
    audio, sr = sf.read(wav, dtype="float32", always_2d=True)
    audio = audio[:, 0]
    if sr != in_sr:
        audio = soxr.resample(audio, sr, in_sr).astype(np.float32)
    pcm16 = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
    chunk = int(in_sr * 0.1)  # 100 ms
    async with websockets.connect(url, max_size=None) as ws:
        n = 0
        for i in range(0, len(pcm16), chunk):
            await ws.send(pcm16[i : i + chunk].tobytes())
            n += 1
            if not fast:
                await asyncio.sleep(0.1)  # real-time pacing, like the device
        await asyncio.sleep(1.0)  # let the server drain the last utterance
        await ws.send(json.dumps({"type": "disconnect"}))
        print(f"sent {n} chunks ({len(pcm16)/in_sr:.1f}s of audio) to {url}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", required=True)
    ap.add_argument("--url", default="ws://127.0.0.1:8765")
    ap.add_argument("--in-sr", type=int, default=24000)
    ap.add_argument("--fast", action="store_true", help="no real-time pacing")
    a = ap.parse_args()
    asyncio.run(feed(a.wav, a.url, a.in_sr, a.fast))


if __name__ == "__main__":
    main()
