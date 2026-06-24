"""Device-connection telemetry for the ambient bridge — quantify the ambient WS "wedge" bug.

The bridge is the RELIABLE observer: the Voice PE wedges half-open and can't tell its ambient
connection died, but the bridge sees the connect/disconnect directly. This records every
connect/disconnect transition of the (single-device) connection with timestamps, computes
connection uptime + dark-gap durations, and counts "dark > N s" events (the wedge signal).

Durable record = an appended JSONL of every event (so gaps spanning a bridge restart can be
reconstructed offline from the timestamps). Live aggregates (counts, longest gap) persist across
bridge restarts via a small stats file and surface in ambient_health.json → the Genesis monitor →
dashboard. Pure stdlib; no genesis imports. Durations use a monotonic clock (injectable for tests);
event timestamps use wall-clock.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime

logger = logging.getLogger("ambient.connstats")


class ConnectionStats:
    def __init__(
        self,
        *,
        events_path: str,
        stats_path: str,
        dark_threshold_s: float,
        events_max: int = 5000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._events_path = os.path.expanduser(events_path)
        self._stats_path = os.path.expanduser(stats_path)
        self._dark_threshold_s = dark_threshold_s
        self._events_max = max(1, events_max)
        self._trim_every = max(1, events_max // 10)  # amortize the trim cost
        self._append_count = 0
        self._clock = clock
        # Persisted aggregates (reloaded from stats_path).
        self._total_connects = 0
        self._total_disconnects = 0
        self._dark_events = 0            # gaps that exceeded dark_threshold_s
        self._last_gap_s: float | None = None
        self._longest_gap_s = 0.0
        # Live state (per bridge process; monotonic resets on restart so it is NOT persisted).
        self._connect_mono: float | None = None
        self._dark_since_mono: float | None = None
        self._dark_since_iso: str | None = None
        self._dark_counted = False
        self._load()

    # --- transitions: call when the device's connection crosses 0↔1 -----------------------------

    def on_connect(self) -> None:
        now = self._clock()
        gap_s = None
        if self._dark_since_mono is not None:
            gap_s = now - self._dark_since_mono
            self._last_gap_s = gap_s
            self._longest_gap_s = max(self._longest_gap_s, gap_s)
            if gap_s >= self._dark_threshold_s and not self._dark_counted:
                self._dark_events += 1  # recovered before a tick() caught it
        self._total_connects += 1
        self._connect_mono = now
        self._dark_since_mono = None
        self._dark_since_iso = None
        self._dark_counted = False
        self._append_event("connect", gap_s=gap_s)
        self._save()

    def on_disconnect(self) -> None:
        now = self._clock()
        uptime_s = (now - self._connect_mono) if self._connect_mono is not None else None
        self._total_disconnects += 1
        self._connect_mono = None
        self._dark_since_mono = now
        self._dark_since_iso = datetime.now(UTC).isoformat()
        self._dark_counted = False
        self._append_event("disconnect", uptime_s=uptime_s)
        self._save()

    # --- periodic: call from the health loop ----------------------------------------------------

    def tick(self) -> None:
        """Count a currently-ongoing dark period as a dark event once it crosses the threshold —
        even if the device never reconnects (the wedge). Idempotent per dark period."""
        if (self._dark_since_mono is not None and not self._dark_counted
                and (self._clock() - self._dark_since_mono) >= self._dark_threshold_s):
            self._dark_events += 1
            self._dark_counted = True
            self._append_event("dark_event", dark_for_s=self._clock() - self._dark_since_mono)
            self._save()

    def snapshot(self) -> dict:
        dark_for = (self._clock() - self._dark_since_mono) if self._dark_since_mono is not None else None
        return {
            "conn_total_connects": self._total_connects,
            "conn_total_disconnects": self._total_disconnects,
            "conn_dark_events": self._dark_events,
            "conn_last_gap_s": _round(self._last_gap_s),
            "conn_longest_gap_s": _round(self._longest_gap_s),
            "conn_dark_since": self._dark_since_iso,       # None when currently connected
            "conn_dark_for_s": _round(dark_for),
        }

    # --- persistence ----------------------------------------------------------------------------

    def _append_event(self, kind: str, **fields: float | None) -> None:
        rec: dict = {"ts": datetime.now(UTC).isoformat(), "event": kind}
        rec.update({k: _round(v) for k, v in fields.items() if v is not None})
        try:
            with open(self._events_path, "a") as f:
                f.write(json.dumps(rec) + "\n")
            self._append_count += 1
            if self._append_count % self._trim_every == 0:
                self._trim_events()
        except OSError:
            logger.warning("connstats event append failed", exc_info=True)

    def _trim_events(self) -> None:
        """Keep only the last ``events_max`` lines (the JSONL append-grows forever otherwise).
        Cumulative aggregates live in stats_path, so trimming the detail log loses no counts."""
        try:
            with open(self._events_path) as f:
                lines = f.readlines()
            if len(lines) <= self._events_max:
                return
            tmp = self._events_path + ".tmp"
            with open(tmp, "w") as f:
                f.writelines(lines[-self._events_max:])
            os.replace(tmp, self._events_path)
        except OSError:
            logger.warning("connstats events trim failed", exc_info=True)

    def _save(self) -> None:
        data = {
            "total_connects": self._total_connects,
            "total_disconnects": self._total_disconnects,
            "dark_events": self._dark_events,
            "last_gap_s": self._last_gap_s,
            "longest_gap_s": self._longest_gap_s,
        }
        try:
            tmp = self._stats_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self._stats_path)
        except OSError:
            logger.warning("connstats save failed", exc_info=True)

    def _load(self) -> None:
        if not os.path.exists(self._stats_path):
            return
        try:
            with open(self._stats_path) as f:
                d = json.load(f)
        except (ValueError, OSError):
            logger.warning("connstats load failed — starting fresh", exc_info=True)
            return
        self._total_connects = int(d.get("total_connects", 0))
        self._total_disconnects = int(d.get("total_disconnects", 0))
        self._dark_events = int(d.get("dark_events", 0))
        self._last_gap_s = d.get("last_gap_s")
        self._longest_gap_s = float(d.get("longest_gap_s", 0.0))


def _round(v: float | None) -> float | None:
    return round(v, 1) if isinstance(v, (int, float)) else None
