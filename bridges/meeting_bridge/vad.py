"""Voice-activity gating for the meeting bridge — drives per-connection session lifecycle.

The phone streams PCM continuously (no client-side VAD yet), so the bridge decides speech vs
silence from frame energy (peak absolute int16 amplitude) and uses that to open a cloud session on
speech and finalize it after a sustained silence. The effect: Speechmatics only bills during actual
talking, and each meeting (a speech run bounded by long silences) lands in its own transcript. A
short hangover keeps the gate open across mid-utterance dips so word-tails are not clipped.

``threshold <= 0`` DISABLES gating: every frame counts as speech, so a session opens on the first
frame and stays open for the whole connection — the legacy one-session-per-connection behavior.
This is the safe default until the energy threshold is calibrated against a real capture; the
:class:`GateStats` summary still records the peak distribution while disabled, so a calibration run
can observe the room's noise floor before the gate is ever armed.

Numpy-free (pure ``struct``) so the server imports and unit-tests without numpy.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass


def peak_amplitude(frame: bytes) -> int:
    """Peak absolute int16 amplitude of a little-endian PCM16 frame; empty/odd-length → 0.

    An odd trailing byte can't form an int16 sample, so it's dropped. Python ints don't overflow,
    so ``abs(-32768)`` is 32768 (a full-scale-negative frame reads as a loud peak, not silence).
    """
    n = len(frame) // 2
    if n == 0:
        return 0
    return max(abs(v) for v in struct.unpack(f"<{n}h", frame[: n * 2]))


class SessionGate:
    """Per-connection speech/silence state machine that drives cloud-session lifecycle.

    Feed each frame's pre-computed ``peak`` to :meth:`observe`, which returns ``(forward, close)``:

    - ``forward``: relay this frame to the cloud session (opening one if none is active).
    - ``close``: finalize the currently-open session now — a silence longer than
      ``silence_close_s`` has elapsed since the last speech.

    Monotonic time is injected (``now``) so the state machine is deterministic under test. The
    server owns the actual session object; this class owns only the speech/silence decision. After
    the server acts on ``close`` (finalizes + drops its session), it calls :meth:`reset` so a
    continuing silence doesn't keep signaling close and the next speech opens a fresh session.
    """

    def __init__(self, *, threshold: int, hangover_s: float, silence_close_s: float) -> None:
        self._threshold = threshold
        self._hangover_s = hangover_s
        self._silence_close_s = silence_close_s
        self._last_speech: float | None = None  # monotonic ts of the last above-threshold frame

    @property
    def enabled(self) -> bool:
        """False when gating is disabled (threshold <= 0) → legacy single-session relay."""
        return self._threshold > 0

    def observe(self, peak: int, now: float) -> tuple[bool, bool]:
        """Return ``(forward, close)`` for a frame with energy ``peak`` at monotonic time ``now``."""
        is_speech = self._threshold <= 0 or peak >= self._threshold
        if is_speech:
            self._last_speech = now
            return True, False
        # Below threshold (silence).
        if self._last_speech is None:
            return False, False  # no speech yet on this connection — nothing to forward or close
        silence = now - self._last_speech
        if silence <= self._hangover_s:
            return True, False  # hangover tail — keep forwarding so word-ends aren't clipped
        return False, silence >= self._silence_close_s

    def reset(self) -> None:
        """Clear speech state — call after the server finalizes a session on ``close``."""
        self._last_speech = None


@dataclass(frozen=True)
class GateSummary:
    """A calibration window's roll-up (emitted by :class:`GateStats` when the interval elapses)."""

    window_s: float
    forwarded: int
    gated: int
    max_peak: int
    avg_peak: int


class GateStats:
    """Rolling peak / forwarded / gated counters for threshold calibration.

    Pure and logger-free: :meth:`observe` returns a :class:`GateSummary` when a window of
    ``log_interval_s`` has elapsed (else ``None``), and the caller logs it. ``log_interval_s <= 0``
    disables instrumentation entirely (always returns ``None``).
    """

    def __init__(self, log_interval_s: float) -> None:
        self._interval = log_interval_s
        self._window_start: float | None = None
        self._forwarded = 0
        self._gated = 0
        self._max_peak = 0
        self._sum_peak = 0
        self._n = 0

    def observe(self, peak: int, forwarded: bool, now: float) -> GateSummary | None:
        if self._interval <= 0:
            return None
        if self._window_start is None:
            self._window_start = now
        self._n += 1
        self._max_peak = max(self._max_peak, peak)
        self._sum_peak += peak
        if forwarded:
            self._forwarded += 1
        else:
            self._gated += 1
        if now - self._window_start >= self._interval:
            summary = GateSummary(
                window_s=now - self._window_start,
                forwarded=self._forwarded,
                gated=self._gated,
                max_peak=self._max_peak,
                avg_peak=(self._sum_peak // self._n) if self._n else 0,
            )
            self._reset(now)
            return summary
        return None

    def _reset(self, now: float) -> None:
        self._window_start = now
        self._forwarded = 0
        self._gated = 0
        self._max_peak = 0
        self._sum_peak = 0
        self._n = 0
