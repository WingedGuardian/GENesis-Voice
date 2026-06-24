"""Unit tests for the bridge connection recorder (deterministic via an injected clock)."""
import json

from ambient_bridge.connection_stats import ConnectionStats


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _stats(tmp_path, threshold=120.0, clock=None):
    return ConnectionStats(
        events_path=str(tmp_path / "ev.jsonl"),
        stats_path=str(tmp_path / "stats.json"),
        dark_threshold_s=threshold, clock=clock or _Clock())


def test_connect_disconnect_counts_and_event_log(tmp_path):
    clk = _Clock()
    s = _stats(tmp_path, clock=clk)
    s.on_connect()
    clk.advance(30)
    s.on_disconnect()
    snap = s.snapshot()
    assert snap["conn_total_connects"] == 1
    assert snap["conn_total_disconnects"] == 1
    assert snap["conn_dark_since"] is not None  # currently dark
    kinds = [json.loads(line)["event"] for line in (tmp_path / "ev.jsonl").read_text().splitlines()]
    assert kinds == ["connect", "disconnect"]


def test_short_gap_is_not_a_dark_event(tmp_path):
    clk = _Clock()
    s = _stats(tmp_path, threshold=120.0, clock=clk)
    s.on_connect()
    s.on_disconnect()
    clk.advance(30)  # 30s dark < 120
    s.on_connect()
    assert s.snapshot()["conn_dark_events"] == 0
    assert s.snapshot()["conn_last_gap_s"] == 30.0


def test_long_gap_counts_dark_event_on_reconnect(tmp_path):
    clk = _Clock()
    s = _stats(tmp_path, threshold=120.0, clock=clk)
    s.on_connect()
    s.on_disconnect()
    clk.advance(200)  # 200s dark > 120
    s.on_connect()
    assert s.snapshot()["conn_dark_events"] == 1
    assert s.snapshot()["conn_longest_gap_s"] == 200.0


def test_tick_counts_ongoing_dark_and_is_idempotent(tmp_path):
    clk = _Clock()
    s = _stats(tmp_path, threshold=120.0, clock=clk)
    s.on_connect()
    s.on_disconnect()
    clk.advance(60)
    s.tick()  # 60s — not yet
    assert s.snapshot()["conn_dark_events"] == 0
    clk.advance(70)
    s.tick()  # 130s > 120 → counted
    assert s.snapshot()["conn_dark_events"] == 1
    clk.advance(50)
    s.tick()  # idempotent
    assert s.snapshot()["conn_dark_events"] == 1
    s.on_connect()  # reconnect does NOT double-count (tick already counted it)
    assert s.snapshot()["conn_dark_events"] == 1


def test_persistence_reloads_aggregates(tmp_path):
    clk = _Clock()
    s = _stats(tmp_path, clock=clk)
    s.on_connect()
    s.on_disconnect()
    clk.advance(200)
    s.on_connect()
    s2 = ConnectionStats(  # fresh instance (simulates a bridge restart) reloads from disk
        events_path=str(tmp_path / "ev.jsonl"), stats_path=str(tmp_path / "stats.json"),
        dark_threshold_s=120.0)
    snap = s2.snapshot()
    assert snap["conn_total_connects"] == 2
    assert snap["conn_dark_events"] == 1
    assert snap["conn_longest_gap_s"] == 200.0


def test_event_log_is_capped(tmp_path):
    # events_max=10 → the JSONL never exceeds it; cumulative counts are unaffected.
    s = ConnectionStats(
        events_path=str(tmp_path / "ev.jsonl"), stats_path=str(tmp_path / "stats.json"),
        dark_threshold_s=120.0, events_max=10, clock=_Clock())
    for _ in range(40):  # 40 connect+disconnect = 80 events, capped to ≤10
        s.on_connect()
        s.on_disconnect()
    n = len((tmp_path / "ev.jsonl").read_text().splitlines())
    assert n <= 10
    assert s.snapshot()["conn_total_connects"] == 40  # aggregates not lost to trimming


def test_snapshot_shape_when_connected(tmp_path):
    s = _stats(tmp_path)
    s.on_connect()
    snap = s.snapshot()
    assert snap["conn_dark_since"] is None and snap["conn_dark_for_s"] is None
    for k in ("conn_total_connects", "conn_total_disconnects", "conn_dark_events", "conn_longest_gap_s"):
        assert k in snap
