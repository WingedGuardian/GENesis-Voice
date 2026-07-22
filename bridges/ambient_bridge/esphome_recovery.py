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
from datetime import UTC, datetime

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
) -> tuple[bool, str | None]:
    """Reboot an ESPHome device by pressing its ``button_name`` button via the native API.

    Best-effort: returns ``(ok, error)`` — ``ok`` is True iff the button press was sent;
    ``error`` is a SANITIZED short reason on failure (else None). Never raises.
    ``client_factory`` is an injection seam for tests (default builds a real
    ``aioesphomeapi.APIClient``).

    Privacy: the ``error`` return is surfaced onward (health JSON -> dashboard/Telegram), so it
    must never carry the device IP or the noise PSK — full detail (incl. IP) is LOGGED LOCALLY
    only; the returned reason is a bounded classification (a fixed phrase or the exception class
    name)."""
    import asyncio

    if client_factory is None:
        try:
            from aioesphomeapi import APIClient
        except Exception as exc:  # noqa: BLE001 — optional dep; recovery degrades to a no-op
            logger.error("recovery: aioesphomeapi unavailable (%r) — cannot reboot device", exc)
            return False, "aioesphomeapi unavailable"

        def client_factory() -> object:  # noqa: E306
            return APIClient(ip, port, "", noise_psk=psk)

    cli = client_factory()

    async def _press() -> tuple[bool, str | None]:
        await cli.connect(login=True)
        entities, _services = await cli.list_entities_services()
        # Select by NAME among BUTTON entities (portable — no hard-coded entity key). Duck-typed on
        # the class name so this stays importable/testable without aioesphomeapi present.
        button = next(
            (
                e
                for e in entities
                if getattr(e, "name", None) == button_name
                and type(e).__name__ == "ButtonInfo"
                and getattr(e, "key", None) is not None
            ),
            None,
        )
        if button is None:
            logger.error("recovery: no button named %r on %s — not rebooting", button_name, ip)
            return False, "restart button not found"
        cli.button_command(button.key)  # synchronous in aioesphomeapi
        await asyncio.sleep(1.0)  # let the press flush before we disconnect
        logger.warning(
            "recovery: pressed %r (key=%s) on %s — reboot requested (confirmed only once the device reconnects)",
            button_name,
            button.key,
            ip,
        )
        return True, None

    try:
        # ONE timeout around the WHOLE exchange: a wedged device can stall connect OR the entity
        # listing. Without this the caller's _reboot_inflight would stay set forever → recovery dead.
        return await asyncio.wait_for(_press(), timeout=timeout_s)
    except TimeoutError:
        logger.error("recovery: reboot of %s timed out after %.0fs", ip, timeout_s)
        return False, "reboot timed out"
    except Exception as exc:  # noqa: BLE001 — best-effort; a failed reboot must not crash the bridge
        # Log the full repr (incl. IP) LOCALLY; return only the exception class name — the error
        # message may embed the IP/PSK and must not leak onto the health JSON / dashboard / Telegram.
        logger.error("recovery: reboot of %s failed: %r", ip, exc)
        return False, type(exc).__name__
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(cli.disconnect(), timeout=5.0)


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
        # Never allow an UNCAPPED reboot loop — the cap is the last defense before a human is alerted.
        # A misconfigured <1 is clamped to 1 (recovery_enabled is the on/off switch, not this knob).
        self._max_per_window = max(1, int(max_per_window))
        self._window_s = window_s
        self._clock = clock
        self._last_seen_ts: float | None = None
        self._reboot_ts: list[float] = []
        # Consecutive reboot ATTEMPTS since the device was last seen. A successful reboot
        # reconnects the device -> mark_seen() -> this resets to 0, so a non-zero value means
        # "recovery engaged and none of those attempts restored the device". Persisted so the
        # signal survives a bridge restart mid-wedge (the escalation must be restart-safe).
        self._reboots_since_seen: int = 0
        self._last_reboot_error: str | None = None
        self._load()

    # --- device-presence tracking ---------------------------------------------------------------

    def mark_seen(self) -> None:
        """Record that the device is connected right now (call while active>0). Reconnection
        means recovery worked (or the device came back on its own), so clear the failure
        bookkeeping — ``recovery_failing`` is only ever "still dark AFTER attempts"."""
        self._last_seen_ts = self._clock()
        self._reboots_since_seen = 0
        self._last_reboot_error = None
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
        # Keep only reboots STRICTLY within the window (exclusive at the boundary).
        self._reboot_ts = [t for t in self._reboot_ts if now - t < self._window_s]

    def can_reboot(self) -> bool:
        """False if within the cooldown of the last reboot, or at the rolling-window cap."""
        now = self._clock()
        self._prune(now)
        if self._reboot_ts and (now - self._reboot_ts[-1]) < self._cooldown_s:
            return False
        if len(self._reboot_ts) >= self._max_per_window:  # cap is always >= 1 (clamped in __init__)
            return False
        return True

    def at_cap(self) -> bool:
        now = self._clock()
        self._prune(now)
        return len(self._reboot_ts) >= self._max_per_window

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

    def record_reboot(self, error: str | None = None) -> None:
        """Record a reboot ATTEMPT (counts toward cooldown + cap even if the press fails), bump the
        since-seen counter, and store the (sanitized) failure reason if the press did not succeed.
        ``error`` MUST be pre-sanitized by the caller (no IP/PSK) — it flows onto the health JSON."""
        self._reboot_ts.append(self._clock())
        self._reboots_since_seen += 1
        self._last_reboot_error = error
        self._save()

    def recovery_status(
        self,
        *,
        active: int,
        escalation_dark_s: float,
        min_reboots: int,
    ) -> dict:
        """Pure verdict for the health payload: is auto-recovery armed, engaged, and STILL unable
        to restore the device? ``recovery_failing`` is True iff the device is currently dark
        (``active == 0``), has been dark at least ``escalation_dark_s`` (well past the reboot window,
        so recovery has definitively stopped trying), and at least ``min_reboots`` attempts since it
        was last seen failed to bring it back. Restart-safe: keyed off the PERSISTED last-seen /
        counter, never the in-process ConnectionStats dark timer. ``device_dark_since`` is ISO (the
        core interpolates it into an alert), never a raw epoch float."""
        dark = self.dark_for()
        failing = (
            active == 0 and dark is not None and dark >= escalation_dark_s and self._reboots_since_seen >= min_reboots
        )
        since = datetime.fromtimestamp(self._last_seen_ts, UTC).isoformat() if self._last_seen_ts is not None else None
        return {
            "recovery_failing": failing,
            "failed_reboot_count": self._reboots_since_seen,
            "device_dark_since": since,
            "last_reboot_error": self._last_reboot_error,
        }

    # --- persistence ----------------------------------------------------------------------------

    def _save(self) -> None:
        data = {
            "last_seen_ts": self._last_seen_ts,
            "reboot_ts": self._reboot_ts,
            "reboots_since_seen": self._reboots_since_seen,
            "last_reboot_error": self._last_reboot_error,
        }
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
        raw_seen = d.get("last_seen_ts")
        self._last_seen_ts = float(raw_seen) if isinstance(raw_seen, (int, float)) else None
        rb = d.get("reboot_ts") or []
        self._reboot_ts = [float(t) for t in rb if isinstance(t, (int, float))]
        # Back-compat: pre-existing state files (written before this field) lack the key -> 0.
        raw_since = d.get("reboots_since_seen")
        self._reboots_since_seen = int(raw_since) if isinstance(raw_since, int) else 0
        raw_err = d.get("last_reboot_error")
        self._last_reboot_error = raw_err if isinstance(raw_err, str) else None
