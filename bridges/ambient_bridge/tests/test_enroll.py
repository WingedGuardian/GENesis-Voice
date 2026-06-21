"""Unit tests for online-enrollment PURE logic (no sherpa/audio): the collect-tap session
accumulation + request-file parsing/staleness. The embed/finalize path is sherpa-dependent
and validated at E2E, like the rest of the embedding path."""
import json
import types

import numpy as np

from ambient_bridge.server import AmbientServer, _EnrollSession


def _srv(cfg):
    s = AmbientServer.__new__(AmbientServer)   # skip __init__ (no store/engine/sherpa)
    s._cfg = cfg
    s._enroll = None
    s._enroll_last_id = None
    return s


def _cfg(**kw):
    base = {"enroll_min_dur_s": 1.0, "enroll_target_s": 30.0,
            "enroll_request_path": "/nonexistent", "enroll_result_path": "/nonexistent"}
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_collect_buffers_and_gates_min_dur():
    s = _srv(_cfg())
    s._enroll = _EnrollSession(id="a", name="alice", target_s=30.0)
    s._collect_enroll(np.zeros(16000, np.float32), 1.0)   # >= min_dur → kept
    s._collect_enroll(np.zeros(8000, np.float32), 0.5)    # < min_dur → dropped
    assert len(s._enroll.samples) == 1
    assert abs(s._enroll.total_dur - 1.0) < 1e-6


def test_collect_stops_at_target():
    s = _srv(_cfg(enroll_target_s=2.0))
    s._enroll = _EnrollSession(id="a", name="alice", target_s=2.0)
    for _ in range(5):
        s._collect_enroll(np.zeros(16000, np.float32), 1.0)
    # once total_dur >= target the collect tap stops appending (watcher will finalize)
    assert s._enroll.total_dur == 2.0
    assert len(s._enroll.samples) == 2


def test_collect_noop_without_session():
    s = _srv(_cfg())
    s._collect_enroll(np.zeros(16000, np.float32), 1.0)   # no active session → no-op, no error
    assert s._enroll is None


def test_read_request_ok_defaults_target(tmp_path):
    req = tmp_path / "req.json"
    req.write_text(json.dumps({"id": "x1", "name": "bob"}))
    s = _srv(_cfg(enroll_request_path=str(req)))
    assert s._read_enroll_request() == {"id": "x1", "name": "bob", "target_s": 30.0}


def test_read_request_stale_is_cleared(tmp_path):
    req = tmp_path / "req.json"
    req.write_text(json.dumps({"id": "x1", "name": "bob", "ts": "2000-01-01T00:00:00+00:00"}))
    s = _srv(_cfg(enroll_request_path=str(req)))
    assert s._read_enroll_request() is None
    assert not req.exists()       # stale leftover is deleted


def test_read_request_absent(tmp_path):
    s = _srv(_cfg(enroll_request_path=str(tmp_path / "nope.json")))
    assert s._read_enroll_request() is None


def test_write_result_roundtrip(tmp_path):
    res = tmp_path / "res.json"
    s = _srv(_cfg(enroll_result_path=str(res)))
    s._write_enroll_result({"id": "x1", "status": "done", "clips": 7})
    assert json.loads(res.read_text())["clips"] == 7
