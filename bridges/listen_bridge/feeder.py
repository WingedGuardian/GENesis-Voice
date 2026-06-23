"""Test feeder — replay a 16k mono 16-bit WAV over the listen WS as the device would.

Sends raw PCM in 100 ms binary frames, then a JSON ``disconnect`` — exactly what the
Voice PE ambient path sends. Use to validate the bridge end-to-end with NO device.
Convert any source first:  ffmpeg -i in.m4a -ac 1 -ar 16000 sample.wav

  python -m listen_bridge.feeder --wav sample.wav [--url ws://127.0.0.1:8766] [--fast]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import wave

import websockets

_BYTES_PER_SEC = 16000 * 2  # 16k mono 16-bit


async def feed(wav: str, url: str, fast: bool) -> None:
    with wave.open(wav, "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
            raise SystemExit(
                "feeder expects 16k mono 16-bit PCM wav "
                "(convert: ffmpeg -i in -ac 1 -ar 16000 out.wav)"
            )
        frames = w.readframes(w.getnframes())
    chunk = _BYTES_PER_SEC // 10  # 100 ms
    async with websockets.connect(url, max_size=None) as ws:
        n = 0
        for i in range(0, len(frames), chunk):
            await ws.send(frames[i : i + chunk])
            n += 1
            if not fast:
                await asyncio.sleep(0.1)  # real-time pacing, like the device
        await asyncio.sleep(1.5)  # let the bridge drain trailing finals
        await ws.send(json.dumps({"type": "disconnect"}))
        print(f"sent {n} chunks ({len(frames) / _BYTES_PER_SEC:.1f}s of audio) to {url}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", required=True)
    ap.add_argument("--url", default="ws://127.0.0.1:8766")
    ap.add_argument("--fast", action="store_true", help="no real-time pacing")
    a = ap.parse_args()
    asyncio.run(feed(a.wav, a.url, a.fast))


if __name__ == "__main__":
    main()
