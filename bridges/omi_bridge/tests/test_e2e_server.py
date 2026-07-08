"""Integration test: a REAL omi_bridge server subprocess over HTTP.

Complements the in-loop TestClient unit tests by exercising the actual `python -m
omi_bridge.server` entrypoint — the auth path, dedup, a live insert into a real ambient.db,
graceful SIGTERM, and (the reason this exists) the regression guard that the **secret token,
which rides the URL path, never lands in the server log**. aiohttp's default access log writes
the full request path, so `serve()` must build its runner with `access_log=None`; if that ever
regresses, this test fails.

Synthetic values only. Runs from `bridges/` like the other tests; uses the current interpreter
(which has aiohttp), so it works in the omi venv on the edge too.
"""
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

_BR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .../bridges
TOKEN = "e2e-secret-token-must-not-be-logged"
UID = "e2e-omi-uid"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _post(port, token, uid, body):
    url = f"http://127.0.0.1:{port}/omi/{token}/ingest?uid={uid}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, None


def _seg(**over):
    s = {"id": "e1", "text": "build the quarterly deck", "speaker": "SPEAKER_0",
         "speaker_id": 0, "is_user": True, "start": 10.0, "end": 12.0,
         "speech_profile_processed": True, "stt_provider": None}
    s.update(over)
    return s


def test_real_server_end_to_end(tmp_path):
    db = str(tmp_path / "ambient.db")
    logp = str(tmp_path / "svc.log")
    port = _free_port()
    env = {**os.environ, "PYTHONPATH": _BR, "PYTHONUNBUFFERED": "1",
           "OMI_INGEST_SECRET_TOKEN": TOKEN, "OMI_UID_ALLOWLIST": UID, "OMI_DB": db,
           "OMI_STATE_DB": str(tmp_path / "state.db"), "OMI_HEALTH": str(tmp_path / "h.json"),
           "OMI_HTTP_HOST": "127.0.0.1", "OMI_HTTP_PORT": str(port)}
    with open(logp, "w") as logf:
        proc = subprocess.Popen([sys.executable, "-m", "omi_bridge.server"],
                                cwd=_BR, env=env, stdout=logf, stderr=subprocess.STDOUT)
        try:
            for _ in range(80):
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.25).close()
                    break
                except OSError:
                    time.sleep(0.25)
            else:
                raise AssertionError("server never came up")

            assert _post(port, "wrong", UID, {"segments": [_seg()], "session_id": UID})[0] == 403
            st, body = _post(port, TOKEN, UID, {"segments": [_seg()], "session_id": UID})
            assert st == 200 and body["accepted"] == 1 and "message" not in body
            _, body2 = _post(port, TOKEN, UID, {"segments": [_seg()], "session_id": UID})
            assert body2["accepted"] == 0  # dedup by segment id

            rows = sqlite3.connect(db).execute(
                "SELECT source, provenance, is_user FROM ambient_transcripts").fetchall()
            assert rows == [(f"omi-{UID}", "ambient_overheard", 1)]

            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=10) == 0  # graceful shutdown
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    # THE regression guard: the path token must never appear in the server log.
    assert TOKEN not in open(logp).read()
