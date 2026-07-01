"""Server-side auto-recovery for the ambient WS "wedge".

The Voice PE cannot detect that its ambient WebSocket died — it wedges half-open (observed:
0 device-side disconnect events over a 21 h capture) and never reconnects; only a device reboot
recovers it. The BRIDGE is the reliable observer (TCP keep-alive reaping + the active-connection
count), so when the device has been gone longer than a threshold — and was recently present — the
bridge reboots it by pressing the device's "Restart" button over the ESPHome native API.

Restart-safe by design: the wedge signal is "how long since the device was last SEEN", keyed off a
WALL-CLOCK ``last_seen`` timestamp that is PERSISTED to disk. So a deploy-induced wedge — where the
bridge restarts into a fresh process the wedged device never reconnects to — is caught too (the
in-process ``ConnectionStats`` dark timer would be ``None`` there; this deliberately does not use it).

Safety: default OFF; arms only when enabled AND device ip + PSK are configured. A cooldown + a
rolling-window cap make reboot-loops impossible (cap reached → stop + WARN for a human). Best-effort:
a reboot failure never propagates. ``aioesphomeapi`` is imported lazily inside ``reboot_device`` so a
missing dep can't break the bridge import (recovery just logs and no-ops).

Pure stdlib otherwise; no genesis imports.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from collections.abc import Callable

logger = logging.getLogger("ambient.recovery")

DEFAULT_BUTTON_NAME = "Restart"


async def reboot_device(
    ip: str,
    port: int,
    psk: str,
    *,
    button_name: str = DEFAULT_BUTTON_NAME,
    timeout_s: float = 15.0,
    client_factory: Callable[[], object] | None = None,
) -> bool:
    """Reboot an ESPHome device by pressing its ``button_name`` button via the native API.

    Best-effort: returns ``True`` iff the button press was sent; never raises. ``client_factory``
    is an injection seam for tests (default builds a real ``aioesphomeapi.APIClient``)."""
    import asyncio

    if client_factory is None:
        try:
            from aioesphomeapi import APIClient
        except Exception as exc:  # noqa: BLE001 — optional dep; recovery degrades to a no-op
            logger.error("recovery: aioesphomeapi unavailable (%r) — cannot reboot device", exc)
            return False

        def client_factory() -> object:  # noqa: E306
            return APIClient(ip, port, "", noise_psk=psk)

    cli = client_factory()
    try:
        await asyncio.wait_for(cli.connect(login=True), timeout=timeout_s)
        entities, _services = await cli.list_entities_services()
        # Select by NAME among BUTTON entities (portable — no hard-coded entity key). Duck-typed on
        # the class name so this stays importable/testable without aioesphomeapi present.
        button = next(
            (e for e in entities
             if getattr(e, "name", None) == button_name
             and type(e).__name__ == "ButtonInfo"
             and getattr(e, "key", None) is not None),
            None,
        )
        if button is None:
            logger.error("recovery: no button named %r on %s — not rebooting", button_name, ip)
            return False
        cli.button_command(button.key)  # synchronous in aioesphomeapi
        await asyncio.sleep(1.0)         # let the press flush before we disconnect
        logger.warning("recovery: pressed %r (key=%s) on %s — device reboot requested",
                       button_name, button.key, ip)
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort; a failed reboot must not crash the bridge
        logger.error("recovery: reboot of %s failed: %r", ip, exc)
        return False
    finally:
        with contextlib.suppress(Exception):
            await cli.disconnect()


class RecoveryState:
    """Persisted recovery bookkeeping: the wall-clock time the device was last SEEN (active>0), plus
    a history of reboot timestamps for the cooldown + rolling-window cap. Wall-clock (survives process
    death/restart), injectable for tests. All decisions are pure functions of the persisted state."""

    def __init__(
        self,
        *,
        path: str,
        cooldown_s: float,
        max_per_window: int,
        window_s: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = os.path.expanduser(path)
        self._cooldown_s = cooldown_s
        self._max_per_window = max_per_window
        self._window_s = window_s
        self._clock = clock
        self._last_seen_ts: float | None = None
        self._reboot_ts: list[float] = []
        self._load()

    # --- device-presence tracking ---------------------------------------------------------------

    def mark_seen(self) -> None:
        """Record that the device is connected right now (call while active>0)."""
        self._last_seen_ts = self._clock()
        self._save()

    @property
    def last_seen_ts(self) -> float | None:
        return self._last_seen_ts

    def dark_for(self) -> float | None:
        """Seconds since the device was last seen, or None if never seen."""
        if self._last_seen_ts is None:
            return None
        return max(0.0, self._clock() - self._last_seen_ts)

    # --- reboot gating --------------------------------------------------------------------------

    def _prune(self, now: float) -> None:
        self._reboot_ts = [t for t in self._reboot_ts if now - t <= self._window_s]

    def can_reboot(self) -> bool:
        """False if within the cooldown of the last reboot, or at the rolling-window cap."""
        now = self._clock()
        self._prune(now)
        if self._reboot_ts and (now - self._reboot_ts[-1]) < self._cooldown_s:
            return False
        if self._max_per_window and len(self._reboot_ts) >= self._max_per_window:
            return False
        return True

    def at_cap(self) -> bool:
        now = self._clock()
        self._prune(now)
        return bool(self._max_per_window) and len(self._reboot_ts) >= self._max_per_window

    def should_reboot(self, *, active: int, dark_threshold_s: float, seen_window_s: float) -> bool:
        """The full policy. Reboot iff: no device connected, it was seen before (not a fresh
        install), it has been dark at least ``dark_threshold_s`` but no longer than
        ``seen_window_s`` (beyond that we treat it as legitimately absent, not wedged), and the
        cooldown/cap allow it."""
        if active > 0:
            return False
        dark = self.dark_for()
        if dark is None:
            return False
        if not (dark_threshold_s <= dark <= seen_window_s):
            return False
        return self.can_reboot()

    def record_reboot(self) -> None:
        """Record a reboot ATTEMPT (counts toward cooldown + cap even if the press fails)."""
        self._reboot_ts.append(self._clock())
        self._save()

    # --- persistence ----------------------------------------------------------------------------

    def _save(self) -> None:
        data = {"last_seen_ts": self._last_seen_ts, "reboot_ts": self._reboot_ts}
        try:
            tmp = self._path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self._path)
        except OSError:
            logger.warning("recovery state save failed", exc_info=True)

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path) as f:
                d = json.load(f)
        except (ValueError, OSError):
            logger.warning("recovery state load failed — starting fresh", exc_info=True)
            return
        self._last_seen_ts = d.get("last_seen_ts")
        rb = d.get("reboot_ts") or []
        self._reboot_ts = [float(t) for t in rb if isinstance(t, (int, float))]
