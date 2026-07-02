"""Unit tests for the ORT session provider helper (the BFC-arena opt-out).

The onnxruntime CPU memory arena grows monotonically under variable-length audio inputs
and never returns memory to the OS — the residual activity-driven RSS ratchet. sherpa
parses ``provider="cpu:<conf-file>"`` and applies ``EnableCpuMemArena=0`` to the session
options (session.cc, verified at the pinned v1.13.2). ``ort_provider`` builds that string
and materialises the conf file; it must FAIL OPEN to plain "cpu" — capture must never die
for a memory optimisation.
"""
from ambient_bridge.config import AmbientConfig
from ambient_bridge.ort_session import ort_provider


def _clean_env(monkeypatch):
    for k in ("AMBIENT_ORT_ARENA_OFF", "AMBIENT_ORT_CONF_PATH"):
        monkeypatch.delenv(k, raising=False)


def test_provider_is_plain_cpu_by_default(monkeypatch):
    _clean_env(monkeypatch)
    assert ort_provider(AmbientConfig()) == "cpu"


def test_provider_points_at_conf_when_arena_off(monkeypatch, tmp_path):
    _clean_env(monkeypatch)
    conf = tmp_path / "ort.conf"
    monkeypatch.setenv("AMBIENT_ORT_ARENA_OFF", "1")
    monkeypatch.setenv("AMBIENT_ORT_CONF_PATH", str(conf))
    p = ort_provider(AmbientConfig())
    assert p == f"cpu:{conf}"
    text = conf.read_text()
    assert "EnableCpuMemArena=0" in text
    assert "EnableMemPattern=0" in text


def test_provider_rewrites_a_stale_conf(monkeypatch, tmp_path):
    # A leftover/hand-edited conf with the wrong body must be replaced, not trusted —
    # otherwise the arena silently stays on while the health story says it's off.
    _clean_env(monkeypatch)
    conf = tmp_path / "ort.conf"
    conf.write_text("EnableCpuMemArena=1\n")
    monkeypatch.setenv("AMBIENT_ORT_ARENA_OFF", "1")
    monkeypatch.setenv("AMBIENT_ORT_CONF_PATH", str(conf))
    ort_provider(AmbientConfig())
    assert "EnableCpuMemArena=0" in conf.read_text()


def test_provider_fails_open_to_cpu_when_conf_unwritable(monkeypatch, tmp_path):
    # Conf path inside a directory that doesn't exist → the write fails → plain "cpu"
    # (arena stays on) rather than raising into engine init and killing capture.
    _clean_env(monkeypatch)
    monkeypatch.setenv("AMBIENT_ORT_ARENA_OFF", "1")
    monkeypatch.setenv("AMBIENT_ORT_CONF_PATH", str(tmp_path / "missing-dir" / "ort.conf"))
    assert ort_provider(AmbientConfig()) == "cpu"
