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
            return

        if now < self._open_until:
            # Below threshold but still inside the hangover window — pass.
            return

        # Gated: replace with equal-length silence, preserving format.
        frame.audio = b"\x00" * len(frame.audio)

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
