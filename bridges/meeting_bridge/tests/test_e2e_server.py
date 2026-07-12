"""Integration test: a REAL meeting_bridge server subprocess over TCP.

Complements the in-loop TestClient units by exercising the actual ``python -m
meeting_bridge.server`` entrypoint end to end — the pluggable session-factory seam
(``MEETING_SESSION_FACTORY``), a real WebSocket handshake with the path token, binary-PCM relay,
a marker control frame, graceful SIGTERM, and (the reason this exists) the regression guard that
the **secret token, which rides the URL path, never lands in the server log** (``access_log=None``).

Uses a throwaway echo backend injected via the factory env var, so it needs NO speechmatics-rt /
cloud key and runs in any venv with aiohttp. Synthetic values only.
"""

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time

_BR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .../bridges
TOKEN = "e2e-meeting-token-must-not-be-logged"

_FAKE_BACKEND = """
import os
_OUT = os.environ["MTG_FAKE_OUT"]
def _w(line):
    with open(_OUT, "a") as f:
        f.write(line + "\\n")
class EchoSession:
    def __init__(self, cfg, source):
        self.path = os.path.join(cfg.output_dir, "echo.md")
    async def start(self):
        _w("START")
    async def send_audio(self, frame):
        _w("FRAME %d" % len(frame))
    def add_marker(self):
        _w("MARKER")
    async def finalize(self):
        _w("FINALIZE")
def factory(cfg, source):
    return EchoSession(cfg, source)
"""


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def _drive_ws(port, token, frames):
    import aiohttp

    url = f"http://127.0.0.1:{port}/meeting/{token}"
    async with aiohttp.ClientSession() as sess:
        async with sess.ws_connect(url) as ws:
            for fr in frames:
                await ws.send_bytes(fr)
            await ws.send_str(json.dumps({"type": "marker"}))
            await ws.close()


def test_real_server_end_to_end(tmp_path):
    fake = tmp_path / "mtg_fake.py"
    fake.write_text(_FAKE_BACKEND)
    out = str(tmp_path / "echo.log")
    logp = str(tmp_path / "svc.log")
    port = _free_port()
    env = {
        **os.environ,
        "PYTHONPATH": f"{_BR}:{tmp_path}",
        "PYTHONUNBUFFERED": "1",
        "MEETING_INGEST_TOKEN": TOKEN,
        "MEETING_HTTP_HOST": "127.0.0.1",
        "MEETING_HTTP_PORT": str(port),
        "MEETING_HEALTH": str(tmp_path / "h.json"),
        "MEETING_OUTPUT_DIR": str(tmp_path / "sessions"),
        "MEETING_SESSION_FACTORY": "mtg_fake:factory",
        "MTG_FAKE_OUT": out,
    }
    with open(logp, "w") as logf:
        proc = subprocess.Popen(
            [sys.executable, "-m", "meeting_bridge.server"], cwd=_BR, env=env, stdout=logf, stderr=subprocess.STDOUT
        )
        try:
            for _ in range(80):
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.25).close()
                    break
                except OSError:
                    time.sleep(0.25)
            else:
                raise AssertionError("server never came up")

            # bad token -> 403 handshake failure, no session created
            import aiohttp

            async def _bad():
                async with aiohttp.ClientSession() as s:
                    try:
                        async with s.ws_connect(f"http://127.0.0.1:{port}/meeting/wrong"):
                            return None
                    except aiohttp.WSServerHandshakeError as e:
                        return e.status

            assert asyncio.run(_bad()) == 403

            asyncio.run(_drive_ws(port, TOKEN, [b"\x01\x02\x03\x04", b"\x05\x06\x07\x08\x09\x0a"]))

            # give the server a beat to flush finalize
            for _ in range(40):
                if os.path.exists(out) and "FINALIZE" in open(out).read():
                    break
                time.sleep(0.05)
            body = open(out).read()
            assert "START" in body
            assert "FRAME 4" in body and "FRAME 6" in body  # both PCM frames relayed intact
            assert "MARKER" in body
            assert "FINALIZE" in body

            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=10) == 0  # graceful shutdown
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    # THE regression guard: the path token must never appear in the server log.
    assert TOKEN not in open(logp).read()
