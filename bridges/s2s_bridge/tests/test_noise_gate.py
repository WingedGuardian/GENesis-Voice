"""Tests for NoiseGate — gates sub-threshold mic noise before OpenAI VAD.

The Voice PE streams mic audio continuously. Quiet room noise (fans, HVAC,
keyboard) can trip OpenAI's semantic VAD into a false "user is interrupting"
turn while the bot is mid-sentence. NoiseGate replaces sub-threshold audio
with equal-length silence so the VAD never sees it, while passing real speech
through byte-identical (barge-in preserved).
"""
import asyncio

import numpy as np
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
    TextFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from app.noise_gate import NoiseGate


def _audio_frame(peak: int, n_samples: int = 240) -> InputAudioRawFrame:
    """Build an InputAudioRawFrame whose peak amplitude is exactly ``peak``."""
    samples = np.zeros(n_samples, dtype=np.int16)
    samples[0] = peak
    samples[1] = -peak
    return InputAudioRawFrame(audio=samples.tobytes(), sample_rate=24000, num_channels=1)


def _make_gate(monkeypatch, **kwargs):
    """Construct a NoiseGate with push_frame captured and a mutable clock.

    Returns (gate, pushed_list, clock_dict). ``pushed_list`` collects every
    (frame, direction) pushed downstream. ``clock_dict["t"]`` is the monotonic
    clock NoiseGate reads.
    """
    clock = {"t": 1000.0}
    monkeypatch.setattr("app.noise_gate.time.monotonic", lambda: clock["t"])

    defaults = {"open_threshold": 500, "bot_speaking_threshold": 1500, "hangover_ms": 250}
    defaults.update(kwargs)
    gate = NoiseGate(**defaults)

    pushed: list = []

    async def fake_push(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append((frame, direction))

    gate.push_frame = fake_push
    return gate, pushed, clock


def _run(coro):
    asyncio.run(coro)


def test_below_threshold_frame_is_silenced(monkeypatch):
    """A frame whose peak is below open_threshold is replaced with all-zero
    audio of the SAME length, with sample_rate/channels preserved."""
    gate, pushed, _ = _make_gate(monkeypatch)
    frame = _audio_frame(peak=100)  # < 500
    original_len = len(frame.audio)

    _run(gate.process_frame(frame, FrameDirection.DOWNSTREAM))

    assert len(pushed) == 1
    out, _direction = pushed[0]
    assert isinstance(out, InputAudioRawFrame)
    assert len(out.audio) == original_len
    assert out.audio == b"\x00" * original_len
    assert out.sample_rate == 24000
    assert out.num_channels == 1


def test_above_threshold_frame_passes_byte_identical(monkeypatch):
    """A frame whose peak is above open_threshold passes through unchanged."""
    gate, pushed, _ = _make_gate(monkeypatch)
    frame = _audio_frame(peak=5000)  # > 500
    expected = frame.audio

    _run(gate.process_frame(frame, FrameDirection.DOWNSTREAM))

    assert len(pushed) == 1
    out, _direction = pushed[0]
    assert out is frame
    assert out.audio == expected


def test_hangover_keeps_gate_open_for_brief_dip(monkeypatch):
    """After a loud frame opens the gate, a brief quiet frame WITHIN the
    hangover window still passes (hysteresis stops mid-word clipping)."""
    gate, pushed, clock = _make_gate(monkeypatch, hangover_ms=250)

    loud = _audio_frame(peak=5000)
    _run(gate.process_frame(loud, FrameDirection.DOWNSTREAM))

    # 100ms later: a quiet frame that would normally be gated.
    clock["t"] = 1000.1  # +100ms, within 250ms hangover
    quiet = _audio_frame(peak=50)
    quiet_audio = quiet.audio
    _run(gate.process_frame(quiet, FrameDirection.DOWNSTREAM))

    assert len(pushed) == 2
    out, _direction = pushed[1]
    # Passed through (not silenced) because the hangover window is still open.
    assert out.audio == quiet_audio
    assert out.audio != b"\x00" * len(quiet_audio)


def test_hangover_expires_then_gates(monkeypatch):
    """Once the hangover window lapses, quiet frames are gated again."""
    gate, pushed, clock = _make_gate(monkeypatch, hangover_ms=250)

    _run(gate.process_frame(_audio_frame(peak=5000), FrameDirection.DOWNSTREAM))

    clock["t"] = 1000.5  # +500ms, past the 250ms hangover
    quiet = _audio_frame(peak=50)
    _run(gate.process_frame(quiet, FrameDirection.DOWNSTREAM))

    out, _direction = pushed[1]
    assert out.audio == b"\x00" * len(out.audio)


def test_bot_speaking_raises_threshold(monkeypatch):
    """While the bot speaks, the higher bot_speaking_threshold applies: a
    mid-level frame that passes when idle gets gated during bot speech.

    The clock is advanced past the hangover window between phases so the
    threshold change is what's under test, not residual hysteresis.
    """
    gate, pushed, clock = _make_gate(
        monkeypatch, open_threshold=500, bot_speaking_threshold=1500, hangover_ms=250
    )

    # Idle: a peak=1000 frame is above open_threshold (500) → passes.
    mid_idle = _audio_frame(peak=1000)
    mid_idle_audio = mid_idle.audio
    _run(gate.process_frame(mid_idle, FrameDirection.DOWNSTREAM))
    assert pushed[-1][0].audio == mid_idle_audio  # passed

    # Bot starts speaking → threshold jumps to 1500.
    _run(gate.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM))

    # Advance past the hangover opened by the idle frame so hysteresis doesn't
    # mask the threshold change.
    clock["t"] = 1000.5  # +500ms > 250ms hangover
    # Same peak=1000 frame is now BELOW bot_speaking_threshold → gated.
    mid_busy = _audio_frame(peak=1000)
    _run(gate.process_frame(mid_busy, FrameDirection.DOWNSTREAM))
    assert pushed[-1][0].audio == b"\x00" * len(mid_busy.audio)

    # Bot stops → threshold returns to open_threshold; peak=1000 passes again.
    _run(gate.process_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM))
    clock["t"] = 1001.0  # advance again, no lingering hangover (frame was gated)
    mid_idle2 = _audio_frame(peak=1000)
    mid_idle2_audio = mid_idle2.audio
    _run(gate.process_frame(mid_idle2, FrameDirection.DOWNSTREAM))
    assert pushed[-1][0].audio == mid_idle2_audio


def test_speaking_frames_pass_through(monkeypatch):
    """Bot speaking control frames are passed through unchanged."""
    gate, pushed, _ = _make_gate(monkeypatch)
    started = BotStartedSpeakingFrame()
    _run(gate.process_frame(started, FrameDirection.UPSTREAM))
    assert pushed[-1][0] is started


def test_non_audio_frame_passes_untouched(monkeypatch):
    """Arbitrary non-audio frames pass through unchanged."""
    gate, pushed, _ = _make_gate(monkeypatch)
    text = TextFrame("hello")
    _run(gate.process_frame(text, FrameDirection.DOWNSTREAM))
    assert len(pushed) == 1
    assert pushed[0][0] is text


def test_empty_audio_is_safe(monkeypatch):
    """An empty/odd-length audio buffer is treated as peak 0 (gated) without
    raising — and the silenced replacement matches the original length."""
    gate, pushed, _ = _make_gate(monkeypatch)
    frame = InputAudioRawFrame(audio=b"", sample_rate=24000, num_channels=1)
    _run(gate.process_frame(frame, FrameDirection.DOWNSTREAM))
    out, _direction = pushed[0]
    assert out.audio == b""


def test_full_scale_negative_sample_not_gated(monkeypatch):
    """A full-scale -32768 (INT16_MIN) sample is maximally LOUD. With naive
    int16 abs it would overflow back to -32768 (negative 'peak') and get
    wrongly silenced; with int32 widening abs(-32768)=32768 ≥ threshold so the
    frame passes through byte-identical."""
    gate, pushed, _ = _make_gate(monkeypatch, open_threshold=500)
    samples = np.zeros(240, dtype=np.int16)
    samples[0] = -32768  # full-scale negative, no positive counterpart
    frame = InputAudioRawFrame(
        audio=samples.tobytes(), sample_rate=24000, num_channels=1
    )
    original = frame.audio
    _run(gate.process_frame(frame, FrameDirection.DOWNSTREAM))
    assert pushed[0][0].audio == original  # passed through, NOT silenced
