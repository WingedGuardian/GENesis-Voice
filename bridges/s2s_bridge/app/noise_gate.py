"""Noise gate — suppress sub-threshold mic audio before OpenAI's VAD sees it.

The Voice PE streams mic audio continuously. Quiet background noise (fans,
HVAC, distant speech, keyboard) is loud enough to trip OpenAI's semantic VAD
into a false "user started speaking" turn while the bot is mid-sentence,
producing spurious interruptions. This processor sits on the input side,
right after the transport, and replaces sub-threshold ``InputAudioRawFrame``
audio with equal-length silence so the VAD never sees it. Real speech passes
through byte-identical, so genuine barge-in is preserved.

Two thresholds: a lower ``open_threshold`` while idle, and a higher
``bot_speaking_threshold`` while the bot is talking (when the bar for a real
interruption should be higher, since the device speaker bleed and room echo
raise the ambient floor). A short hangover keeps the gate open across mid-word
dips so speech is not clipped.
"""
import logging
import time

import numpy as np
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InputAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger(__name__)


class NoiseGate(FrameProcessor):
    """Gate sub-threshold mic audio out before OpenAI's VAD.

    Pure transformer/observer: every frame is pushed (possibly with its audio
    zeroed), none are dropped.
    """

    def __init__(
        self,
        open_threshold: int,
        bot_speaking_threshold: int,
        hangover_ms: float = 250,
        log_interval_s: float = 2.0,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._open_threshold = open_threshold
        self._bot_speaking_threshold = bot_speaking_threshold
        self._hangover_s = hangover_ms / 1000.0
        self._bot_speaking = False
        # Monotonic deadline until which the gate stays "open" after the last
        # above-threshold frame. 0.0 means closed (no recent loud frame).
        self._open_until = 0.0
        # --- diagnostic instrumentation (sampled; for on-device calibration) ---
        self._log_interval_s = log_interval_s
        self._stats_window_start: float | None = None
        self._frames_passed = 0
        self._frames_gated = 0
        self._max_peak_passed = 0
        self._max_peak_gated = 0
        # 0.0 sentinel: the first threshold-crossing pass during bot speech
        # always logs regardless of the interval (we want the first barge-in
        # attempt); reset on reconnect via reset_instrumentation().
        self._last_bot_pass_log = 0.0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
        elif isinstance(frame, InputAudioRawFrame):
            self._gate(frame)

        await self.push_frame(frame, direction)

    def _gate(self, frame: InputAudioRawFrame) -> None:
        """Zero out ``frame.audio`` in place if it is below threshold and the
        hangover window has lapsed. Leaves loud frames untouched."""
        peak = self._peak_amplitude(frame.audio)
        threshold = (
            self._bot_speaking_threshold if self._bot_speaking else self._open_threshold
        )
        now = time.monotonic()

        if peak >= threshold:
            # Above threshold: keep the gate open for the hangover window so a
            # following mid-word dip is not clipped.
            self._open_until = now + self._hangover_s
            passed = True
        elif now < self._open_until:
            # Below threshold but still inside the hangover window — pass.
            passed = True
        else:
            # Gated: replace with equal-length silence, preserving format.
            frame.audio = b"\x00" * len(frame.audio)
            passed = False

        self._instrument(peak, threshold, passed, now)

    def _instrument(self, peak: int, threshold: int, passed: bool, now: float) -> None:
        """Record one gate decision and emit sampled diagnostics.

        Two outputs, both rate-limited so a live conversation never floods the
        log: (1) an immediate line when audio passes the gate WHILE the bot is
        speaking — the prime suspect for a false interrupt — at most once per
        ``log_interval_s``; (2) a periodic summary of pass/gate counts and the
        max peaks seen, so the room's noise floor and what's slipping through
        can be read straight from journald during calibration.
        """
        if self._log_interval_s <= 0:
            return  # instrumentation disabled (post-calibration quiet switch)
        if self._stats_window_start is None:
            self._stats_window_start = now

        if passed:
            self._frames_passed += 1
            self._max_peak_passed = max(self._max_peak_passed, peak)
            # Flag only frames that actually CROSSED the threshold (i.e. opened
            # the gate) while the bot speaks — those are the false-interrupt
            # suspects. Frames merely riding the hangover are below threshold and
            # would log a misleadingly tiny peak.
            if (
                self._bot_speaking
                and peak >= threshold
                and now - self._last_bot_pass_log >= self._log_interval_s
            ):
                logger.info(
                    "noise-gate: audio PASSED during bot speech "
                    "(possible barge-in / false interrupt) peak=%d thr=%d",
                    peak,
                    threshold,
                )
                self._last_bot_pass_log = now
        else:
            self._frames_gated += 1
            self._max_peak_gated = max(self._max_peak_gated, peak)

        elapsed = now - self._stats_window_start
        if elapsed >= self._log_interval_s:
            total = self._frames_passed + self._frames_gated
            if total:
                logger.info(
                    "noise-gate stats[%.0fs]: bot_speaking=%s thr=%d "
                    "passed=%d/%d gated=%d max_peak_passed=%d max_peak_gated=%d",
                    elapsed,
                    self._bot_speaking,
                    threshold,
                    self._frames_passed,
                    total,
                    self._frames_gated,
                    self._max_peak_passed,
                    self._max_peak_gated,
                )
            self._reset_stats(now)

    def _reset_stats(self, now: float) -> None:
        """Start a fresh stats window at ``now``."""
        self._stats_window_start = now
        self._frames_passed = 0
        self._frames_gated = 0
        self._max_peak_passed = 0
        self._max_peak_gated = 0

    def reset_instrumentation(self) -> None:
        """Reset all diagnostic state for a fresh session.

        Called on client reconnect: the NoiseGate instance is reused across
        connections, so without this the next session's first summary window
        would span the disconnect gap and its first barge-in could be throttled
        by the previous session's log. Gating state (``_bot_speaking``,
        ``_open_until``) is intentionally NOT touched here.
        """
        self._stats_window_start = None
        self._frames_passed = 0
        self._frames_gated = 0
        self._max_peak_passed = 0
        self._max_peak_gated = 0
        self._last_bot_pass_log = 0.0

    @staticmethod
    def _peak_amplitude(audio: bytes) -> int:
        """Peak absolute int16 amplitude. Empty/odd-length buffers → 0."""
        if not audio or len(audio) < 2:
            return 0
        # An odd trailing byte cannot form an int16 sample; np.frombuffer
        # requires a buffer length that is a multiple of the dtype size.
        usable = audio if len(audio) % 2 == 0 else audio[:-1]
        # Widen to int32 BEFORE abs: int16 abs(-32768) overflows back to
        # -32768, which would make a full-scale-negative frame read as a
        # negative "peak" and get wrongly gated as silence.
        samples = np.frombuffer(usable, dtype=np.int16).astype(np.int32)
        if samples.size == 0:
            return 0
        return int(np.abs(samples).max())
