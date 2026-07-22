"""Unit tests for the ambient device auto-recovery (deterministic via an injected clock;
reboot primitive exercised against a fake ESPHome API client — no aioesphomeapi needed)."""
import asyncio
import types

import pytest

from ambient_bridge.esphome_recovery import RecoveryState, reboot_device


class _Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0  # wall-clock-ish base

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _state(tmp_path, *, cooldown_s=300.0, max_per_window=3, window_s=3600.0, clock=None):
    return RecoveryState(
        path=str(tmp_path / "recovery.json"),
        cooldown_s=cooldown_s, max_per_window=max_per_window, window_s=window_s,
        clock=clock or _Clock(),
    )


# --- presence / dark tracking -------------------------------------------------------------------

def test_never_seen_never_reboots(tmp_path):
    s = _state(tmp_path)
    assert s.dark_for() is None
    assert s.should_reboot(active=0, dark_threshold_s=300, seen_window_s=7200) is False


def test_mark_seen_resets_dark(tmp_path):
    clk = _Clock()
    s = _state(tmp_path, clock=clk)
    s.mark_seen()
    assert s.dark_for() == 0.0
    clk.advance(120)
    assert s.dark_for() == 120.0


def test_dark_past_threshold_triggers(tmp_path):
    clk = _Clock()
    s = _state(tmp_path, clock=clk)
    s.mark_seen()
    clk.advance(299)
    assert s.should_reboot(active=0, dark_threshold_s=300, seen_window_s=7200) is False  # too soon
    clk.advance(2)  # now 301s dark
    assert s.should_reboot(active=0, dark_threshold_s=300, seen_window_s=7200) is True


def test_connected_never_reboots(tmp_path):
    clk = _Clock()
    s = _state(tmp_path, clock=clk)
    s.mark_seen()
    clk.advance(9999)
    assert s.should_reboot(active=1, dark_threshold_s=300, seen_window_s=7200) is False


def test_absent_beyond_window_is_not_a_wedge(tmp_path):
    clk = _Clock()
    s = _state(tmp_path, clock=clk)
    s.mark_seen()
    clk.advance(7201)  # dark longer than seen_window → treat as legitimately absent
    assert s.should_reboot(active=0, dark_threshold_s=300, seen_window_s=7200) is False


# --- cooldown + rolling-window cap --------------------------------------------------------------

def test_cooldown_blocks_then_clears(tmp_path):
    clk = _Clock()
    s = _state(tmp_path, cooldown_s=300, clock=clk)
    assert s.can_reboot() is True
    s.record_reboot()
    assert s.can_reboot() is False           # within cooldown
    clk.advance(300)
    assert s.can_reboot() is True            # cooldown elapsed


def test_cap_blocks_and_window_rolls(tmp_path):
    clk = _Clock()
    s = _state(tmp_path, cooldown_s=0, max_per_window=3, window_s=3600, clock=clk)
    for _ in range(3):
        assert s.can_reboot() is True
        s.record_reboot()
        clk.advance(60)
    assert s.at_cap() is True
    assert s.can_reboot() is False           # at the cap
    clk.advance(3600)                         # roll the window past all 3
    assert s.can_reboot() is True
    assert s.at_cap() is False


def test_should_reboot_respects_cooldown(tmp_path):
    clk = _Clock()
    s = _state(tmp_path, cooldown_s=300, clock=clk)
    s.mark_seen()
    clk.advance(400)                          # 400s dark
    s.record_reboot()                         # just rebooted
    assert s.should_reboot(active=0, dark_threshold_s=300, seen_window_s=7200) is False
    clk.advance(300)                          # cooldown elapsed, still dark (<window)
    assert s.should_reboot(active=0, dark_threshold_s=300, seen_window_s=7200) is True


# --- persistence == restart-safety (the load-bearing property) ----------------------------------

def test_state_survives_restart(tmp_path):
    clk = _Clock()
    s = _state(tmp_path, clock=clk)
    s.mark_seen()
    seen_at = clk()
    s.record_reboot()
    # A "restart": a brand-new object over the same file, at a later wall-clock time.
    clk.advance(120)
    s2 = _state(tmp_path, clock=clk)
    assert s2.last_seen_ts == seen_at        # last-seen restored → deploy-wedge dark clock continues
    assert s2.dark_for() == 120.0            # dark measured across the restart
    assert s2.can_reboot() is False          # reboot history restored → cooldown still enforced


# --- reboot primitive against a fake ESPHome API client -----------------------------------------

# Class names match the real aioesphomeapi entity-info classes (the reboot filter selects buttons
# via type(e).__name__ == "ButtonInfo"), so these fakes exercise that filter faithfully.
class ButtonInfo:
    def __init__(self, name, key):
        self.name = name
        self.key = key


class SensorInfo:
    def __init__(self, name, key):
        self.name = name
        self.key = key


class _FakeClient:
    def __init__(self, entities, *, connect_raises=False):
        self._entities = entities
        self._connect_raises = connect_raises
        self.pressed_key = None
        self.disconnected = False

    async def connect(self, login=False):
        if self._connect_raises:
            raise ConnectionError("boom")

    async def list_entities_services(self):
        return self._entities, []

    def button_command(self, key):
        self.pressed_key = key

    async def disconnect(self):
        self.disconnected = True


def test_reboot_presses_named_button(tmp_path):
    client = _FakeClient([SensorInfo("Uptime", 1), ButtonInfo("Restart", 4242)])
    ok, err = asyncio.run(reboot_device("1.2.3.4", 6053, "psk", client_factory=lambda: client))
    assert ok is True
    assert err is None
    assert client.pressed_key == 4242
    assert client.disconnected is True


def test_reboot_no_matching_button_returns_false(tmp_path):
    client = _FakeClient([ButtonInfo("Mute", 7)])
    ok, err = asyncio.run(reboot_device("1.2.3.4", 6053, "psk", client_factory=lambda: client))
    assert ok is False
    assert err == "restart button not found"
    assert client.pressed_key is None
    assert client.disconnected is True       # still cleans up


def test_reboot_ignores_non_button_named_restart(tmp_path):
    client = _FakeClient([SensorInfo("Restart", 9)])  # right name, wrong entity type
    ok, err = asyncio.run(reboot_device("1.2.3.4", 6053, "psk", client_factory=lambda: client))
    assert ok is False
    assert err == "restart button not found"
    assert client.pressed_key is None


def test_reboot_connect_error_returns_false_never_raises(tmp_path):
    client = _FakeClient([ButtonInfo("Restart", 1)], connect_raises=True)
    ok, err = asyncio.run(reboot_device("1.2.3.4", 6053, "psk", client_factory=lambda: client))
    assert ok is False
    # Sanitized classification — the exception CLASS name, never a message that could embed the IP/PSK.
    assert err == "ConnectionError"
    assert "1.2.3.4" not in (err or "")
    assert client.disconnected is True       # finally-block cleanup still runs


def test_cap_never_uncapped_when_misconfigured_zero(tmp_path):
    # max_per_window <= 0 must NOT mean "unlimited" — it's clamped to 1 (the cap can't be bypassed).
    clk = _Clock()
    s = _state(tmp_path, cooldown_s=0, max_per_window=0, window_s=3600, clock=clk)
    s.record_reboot()
    assert s.at_cap() is True
    assert s.can_reboot() is False


def test_prune_boundary_is_exclusive(tmp_path):
    # A reboot exactly window_s ago is pruned (exclusive boundary), so the cap clears on schedule.
    clk = _Clock()
    s = _state(tmp_path, cooldown_s=0, max_per_window=1, window_s=3600, clock=clk)
    s.record_reboot()
    assert s.can_reboot() is False           # at the cap of 1
    clk.advance(3600)                         # exactly window_s later
    assert s.at_cap() is False
    assert s.can_reboot() is True


def test_load_ignores_corrupt_last_seen(tmp_path):
    # A non-numeric persisted last_seen_ts must not crash dark_for() (health-loop safety).
    import json
    p = tmp_path / "recovery.json"
    p.write_text(json.dumps({"last_seen_ts": "not-a-number", "reboot_ts": ["bad", 5.0]}))
    s = RecoveryState(path=str(p), cooldown_s=300, max_per_window=3, window_s=3600, clock=_Clock())
    assert s.dark_for() is None               # corrupt last_seen → None, no TypeError
    # only the numeric reboot ts survived the load
    assert s.should_reboot(active=0, dark_threshold_s=300, seen_window_s=7200) is False


def test_do_device_reboot_always_resets_inflight(tmp_path, monkeypatch):
    """Server glue: the reboot task must clear _reboot_inflight even when the reboot FAILS — else
    recovery locks up after one attempt. Needs the bridge deps (server imports sherpa) → skipped
    where they're absent; runs on the edge venv."""
    pytest.importorskip("sherpa_onnx")
    from ambient_bridge import server as srv
    from ambient_bridge.config import AmbientConfig

    async def _fail_reboot(*_a, **_k):
        return False, "reboot timed out"  # failed press — no network (tuple contract)

    monkeypatch.setattr(srv, "reboot_device", _fail_reboot)
    rec = _state(tmp_path)
    rec.mark_seen()
    fake = types.SimpleNamespace(
        _recovery=rec, _recovery_psk="psk", _cfg=AmbientConfig(), _reboot_inflight=True)
    asyncio.run(srv.AmbientServer._do_device_reboot(fake))
    assert fake._reboot_inflight is False      # cleared in finally, even on failure
    assert rec.at_cap() is False               # 1 attempt recorded (default cap 3)

    # The sanitized failure reason flowed through record_reboot into the persisted state.
    status = rec.recovery_status(active=0, escalation_dark_s=0, min_reboots=1)
    assert status["last_reboot_error"] == "reboot timed out"
    assert status["failed_reboot_count"] == 1


# --- recovery_status: the recovery_failing verdict emitted into ambient_health.json -------------
# recovery_failing = armed recovery ENGAGED and STILL couldn't restore the device. All four
# conditions must hold: device dark now (active==0), never-seen guard, dark >= escalation_dark_s,
# and >= min_reboots failed attempts since last-seen.


def test_recovery_status_not_failing_when_device_back(tmp_path):
    clk = _Clock()
    s = _state(tmp_path, clock=clk)
    s.mark_seen()
    s.record_reboot(error="reboot timed out")
    clk.advance(20000)
    # Device is BACK (active>0) — recovery worked / it returned; never "failing".
    assert s.recovery_status(active=1, escalation_dark_s=14400, min_reboots=1)["recovery_failing"] is False


def test_recovery_status_not_failing_below_escalation_dark(tmp_path):
    clk = _Clock()
    s = _state(tmp_path, clock=clk)
    s.mark_seen()
    s.record_reboot(error="reboot timed out")
    clk.advance(3600)  # only 1h dark — recovery may legitimately still be within its window
    assert s.recovery_status(active=0, escalation_dark_s=14400, min_reboots=1)["recovery_failing"] is False


def test_recovery_status_not_failing_below_min_reboots(tmp_path):
    # Long dark but NO reboot attempts (e.g. the cap was pre-exhausted by a prior episode). With
    # min_reboots=1 this stays quiet — the documented coverage hole (architect #6).
    clk = _Clock()
    s = _state(tmp_path, clock=clk)
    s.mark_seen()
    clk.advance(20000)
    st = s.recovery_status(active=0, escalation_dark_s=14400, min_reboots=1)
    assert st["recovery_failing"] is False
    assert st["failed_reboot_count"] == 0


def test_recovery_status_failing_when_dark_past_escalation_and_attempts_failed(tmp_path):
    clk = _Clock()
    s = _state(tmp_path, clock=clk)
    s.mark_seen()
    s.record_reboot(error="reboot timed out")
    s.record_reboot(error="ConnectionError")
    clk.advance(20000)  # dark well past the 4h escalation
    st = s.recovery_status(active=0, escalation_dark_s=14400, min_reboots=1)
    assert st["recovery_failing"] is True
    assert st["failed_reboot_count"] == 2
    assert st["last_reboot_error"] == "ConnectionError"  # most recent attempt's reason
    # device_dark_since is emitted ISO (never a raw epoch float — architect #4).
    assert isinstance(st["device_dark_since"], str)
    assert "T" in st["device_dark_since"]


def test_recovery_status_device_dark_since_none_when_never_seen(tmp_path):
    st = _state(tmp_path).recovery_status(active=0, escalation_dark_s=14400, min_reboots=1)
    assert st["device_dark_since"] is None
    assert st["recovery_failing"] is False  # dark_for None -> never failing


def test_reboots_since_seen_resets_and_clears_error_on_mark_seen(tmp_path):
    s = _state(tmp_path)
    s.mark_seen()
    s.record_reboot(error="reboot timed out")
    s.record_reboot(error="ConnectionError")
    assert s.recovery_status(active=0, escalation_dark_s=0, min_reboots=1)["failed_reboot_count"] == 2
    s.mark_seen()  # device reconnected -> recovery worked -> bookkeeping cleared
    st = s.recovery_status(active=0, escalation_dark_s=0, min_reboots=1)
    assert st["failed_reboot_count"] == 0
    assert st["last_reboot_error"] is None


def test_reboots_since_seen_persists_across_restart(tmp_path):
    # The restart-safety property that makes recovery_failing usable across a bridge restart mid-wedge.
    clk = _Clock()
    s = _state(tmp_path, clock=clk)
    s.mark_seen()
    s.record_reboot(error="ConnectionError")
    clk.advance(120)
    s2 = _state(tmp_path, clock=clk)  # "restart": a new object over the same file
    st = s2.recovery_status(active=0, escalation_dark_s=0, min_reboots=1)
    assert st["failed_reboot_count"] == 1
    assert st["last_reboot_error"] == "ConnectionError"


def test_recovery_state_backcompat_missing_new_keys(tmp_path):
    # A pre-existing state file (written before reboots_since_seen/last_reboot_error existed) must
    # load to 0 / None, never crash.
    import json

    p = tmp_path / "recovery.json"
    p.write_text(json.dumps({"last_seen_ts": 1_000_000.0, "reboot_ts": [1_000_050.0]}))
    s = RecoveryState(path=str(p), cooldown_s=300, max_per_window=3, window_s=3600, clock=_Clock())
    st = s.recovery_status(active=0, escalation_dark_s=0, min_reboots=1)
    assert st["failed_reboot_count"] == 0
    assert st["last_reboot_error"] is None
