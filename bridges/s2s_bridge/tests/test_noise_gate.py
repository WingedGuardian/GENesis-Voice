"""Tests for NoiseGate — gates sub-threshold mic noise before OpenAI VAD.

The Voice PE streams mic audio continuously. Quiet room noise (fans, HVAC,
keyboard) can trip OpenAI's semantic VAD into a false "user is interrupting"
turn while the bot is mid-sentence. NoiseGate replaces sub-threshold audio
with equal-length silence so the VAD never sees it, while passing real speech
through byte-identical (barge-in preserved).
"""
import asyncio
import logging

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


# --- diagnostic instrumentation (for on-device calibration) ---
# The gate must surface what it is actually seeing — peaks vs threshold, and
# when audio slips through while the bot is speaking — without spamming the log
# on every ~100ms frame. These tests pin the sampled-logging contract.


def test_emits_periodic_stats_after_interval(monkeypatch, caplog):
    """After ``log_interval_s`` of frames, one summary line is emitted with the
    pass/gate counts so we can read the noise floor from journald."""
    gate, _pushed, clock = _make_gate(monkeypatch, log_interval_s=2.0)
    with caplog.at_level(logging.INFO, logger="app.noise_gate"):
        _run(gate.process_frame(_audio_frame(peak=100), FrameDirection.DOWNSTREAM))
        clock["t"] = 1002.5  # +2.5s > 2.0s interval — flush on the next frame
        _run(gate.process_frame(_audio_frame(peak=100), FrameDirection.DOWNSTREAM))

    summaries = [r.message for r in caplog.records if "noise-gate stats" in r.message]
    assert len(summaries) == 1
    assert "gated=2" in summaries[0]


def test_no_stats_before_interval(monkeypatch, caplog):
    """Frames within one interval do NOT emit a summary (no per-frame spam)."""
    gate, _pushed, clock = _make_gate(monkeypatch, log_interval_s=2.0)
    with caplog.at_level(logging.INFO, logger="app.noise_gate"):
        _run(gate.process_frame(_audio_frame(peak=100), FrameDirection.DOWNSTREAM))
        clock["t"] = 1001.0  # +1.0s < 2.0s interval
        _run(gate.process_frame(_audio_frame(peak=100), FrameDirection.DOWNSTREAM))

    assert not any("noise-gate stats" in r.message for r in caplog.records)


def test_stats_report_peaks(monkeypatch, caplog):
    """The summary reports the max passed/gated peaks — the numbers we calibrate
    thresholds against."""
    gate, _pushed, clock = _make_gate(
        monkeypatch, open_threshold=500, bot_speaking_threshold=1500, log_interval_s=2.0
    )
    with caplog.at_level(logging.INFO, logger="app.noise_gate"):
        _run(gate.process_frame(_audio_frame(peak=300), FrameDirection.DOWNSTREAM))  # gated
        _run(gate.process_frame(_audio_frame(peak=800), FrameDirection.DOWNSTREAM))  # passed
        clock["t"] = 1002.1
        _run(gate.process_frame(_audio_frame(peak=100), FrameDirection.DOWNSTREAM))  # gated → flush

    summaries = [r.message for r in caplog.records if "noise-gate stats" in r.message]
    assert len(summaries) == 1
    assert "passed=1/3" in summaries[0]
    assert "max_peak_passed=800" in summaries[0]
    assert "max_peak_gated=300" in summaries[0]


def test_passed_during_bot_speech_logged(monkeypatch, caplog):
    """When audio slips through the gate WHILE the bot is speaking — the prime
    false-interrupt suspect — an immediate line is logged with peak + threshold
    so it can be correlated with the interrupt timestamps."""
    # bot_sustain_ms=0: this test pins the LOGGING contract for a frame that passes
    # during bot speech, independent of the sustained-crossing gating policy.
    gate, _pushed, _clock = _make_gate(
        monkeypatch, open_threshold=500, bot_speaking_threshold=1500, log_interval_s=2.0,
        bot_sustain_ms=0,
    )
    with caplog.at_level(logging.INFO, logger="app.noise_gate"):
        _run(gate.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM))
        _run(gate.process_frame(_audio_frame(peak=5000), FrameDirection.DOWNSTREAM))

    hits = [r.message for r in caplog.records if "PASSED during bot speech" in r.message]
    assert len(hits) == 1
    assert "peak=5000" in hits[0]


def test_passed_during_bot_speech_throttled(monkeypatch, caplog):
    """Repeated pass-through during a single bot turn logs at most once per
    interval — a real barge-in must not flood the log."""
    # bot_sustain_ms=0: pins the log THROTTLE for passed frames, not the gating policy.
    gate, _pushed, clock = _make_gate(
        monkeypatch, open_threshold=500, bot_speaking_threshold=1500, log_interval_s=2.0,
        bot_sustain_ms=0,
    )
    with caplog.at_level(logging.INFO, logger="app.noise_gate"):
        _run(gate.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM))
        _run(gate.process_frame(_audio_frame(peak=5000), FrameDirection.DOWNSTREAM))
        clock["t"] = 1000.5  # +0.5s < 2.0s interval
        _run(gate.process_frame(_audio_frame(peak=5000), FrameDirection.DOWNSTREAM))

    hits = [r.message for r in caplog.records if "PASSED during bot speech" in r.message]
    assert len(hits) == 1


def test_logging_disabled_when_interval_non_positive(monkeypatch, caplog):
    """``log_interval_s <= 0`` turns instrumentation off entirely — the post-
    calibration quiet switch (set via env, no redeploy)."""
    gate, _pushed, clock = _make_gate(monkeypatch, log_interval_s=0)
    with caplog.at_level(logging.INFO, logger="app.noise_gate"):
        _run(gate.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM))
        for _ in range(5):
            _run(gate.process_frame(_audio_frame(peak=5000), FrameDirection.DOWNSTREAM))
        clock["t"] = 1100.0  # well past any interval
        _run(gate.process_frame(_audio_frame(peak=5000), FrameDirection.DOWNSTREAM))

    assert not any("noise-gate" in r.message for r in caplog.records)


def test_hangover_pass_during_bot_speech_not_flagged(monkeypatch, caplog):
    """A quiet frame let through ONLY because it rides the hangover window did
    not cross the threshold — it must NOT be logged as a barge-in suspect, or
    its near-zero peak corrupts the calibration signal. Only threshold-crossing
    frames (the ones that actually opened the gate) get flagged."""
    gate, _pushed, clock = _make_gate(
        monkeypatch,
        open_threshold=500,
        bot_speaking_threshold=1500,
        hangover_ms=5000,  # long hangover so the quiet frame still passes
        bot_sustain_ms=0,  # instant open: this pins the instrumentation guard, not gating
        log_interval_s=0.1,  # short throttle so it's NOT what suppresses the log
    )
    with caplog.at_level(logging.INFO, logger="app.noise_gate"):
        _run(gate.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM))
        _run(gate.process_frame(_audio_frame(peak=5000), FrameDirection.DOWNSTREAM))  # opens gate
        clock["t"] = 1000.2  # +0.2s > 0.1s throttle, but < 5.0s hangover
        _run(gate.process_frame(_audio_frame(peak=50), FrameDirection.DOWNSTREAM))  # hangover pass

    # Exactly one flag — the threshold-crossing frame. The quiet hangover frame
    # is NOT flagged: with log_interval_s=0.1 the throttle would have allowed a
    # second log 0.2s later, so the single hit proves the peak>=threshold guard.
    hits = [r.message for r in caplog.records if "PASSED during bot speech" in r.message]
    assert len(hits) == 1
    assert "peak=5000 thr=1500" in hits[0]


def test_reset_instrumentation_starts_a_clean_window(monkeypatch, caplog):
    """``reset_instrumentation()`` (called on client reconnect) clears the
    throttle + counters so a fresh session's first barge-in always logs, even if
    the previous session logged one moments earlier."""
    gate, _pushed, clock = _make_gate(
        monkeypatch, open_threshold=500, bot_speaking_threshold=1500, log_interval_s=2.0
    )
    _run(gate.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM))
    _run(gate.process_frame(_audio_frame(peak=5000), FrameDirection.DOWNSTREAM))  # logs, sets throttle

    gate.reset_instrumentation()

    with caplog.at_level(logging.INFO, logger="app.noise_gate"):
        clock["t"] = 1000.1  # only +0.1s — would be throttled WITHOUT the reset
        _run(gate.process_frame(_audio_frame(peak=5000), FrameDirection.DOWNSTREAM))

    hits = [r.message for r in caplog.records if "PASSED during bot speech" in r.message]
    assert len(hits) == 1


def test_flush_on_bot_speaking_transition(monkeypatch, caplog):
    """A bot start/stop flushes + resets the stats window so each summary covers
    exactly ONE regime. Without this, a window straddling the transition reports
    a mix (e.g. frames gated under the bot-speaking threshold showing up in a
    bot_speaking=False summary), corrupting the per-regime calibration numbers."""
    gate, _pushed, clock = _make_gate(
        monkeypatch, open_threshold=500, bot_speaking_threshold=10000, log_interval_s=10.0
    )
    with caplog.at_level(logging.INFO, logger="app.noise_gate"):
        _run(gate.process_frame(_audio_frame(peak=30000), FrameDirection.DOWNSTREAM))  # idle pass
        clock["t"] = 1000.3  # past the 250ms hangover
        _run(gate.process_frame(_audio_frame(peak=100), FrameDirection.DOWNSTREAM))  # idle gate
        clock["t"] = 1000.5
        # Bot starts — must flush the IDLE window NOW, despite the 10s interval.
        _run(gate.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM))

    summaries = [r.message for r in caplog.records if "noise-gate stats" in r.message]
    assert len(summaries) == 1
    assert "bot_speaking=False" in summaries[0]  # the flushed window is the idle one
    assert "passed=1/2" in summaries[0]
    assert "max_peak_passed=30000" in summaries[0]


# --- sustained-crossing requirement during bot speech (false barge-in fix) ---------------
# Observed live: a SINGLE ~20ms echo/transient frame peaking just over
# bot_speaking_threshold instantly opened the gate (+ hangover), OpenAI's semantic VAD saw
# the burst, and the bot was cut off mid-sentence repeatedly. Real barge-in speech sustains
# for hundreds of ms; transients don't — so while the bot speaks, the gate opens only after
# ``bot_sustain_ms`` of CONSECUTIVE above-threshold frames. Idle behavior is unchanged.

def _bot_speaking_gate(monkeypatch, **kwargs):
    defaults = {"bot_sustain_ms": 100}
    defaults.update(kwargs)
    gate, pushed, clock = _make_gate(monkeypatch, **defaults)
    _run(gate.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM))
    pushed.clear()
    return gate, pushed, clock


def test_bot_speech_single_transient_stays_gated(monkeypatch):
    """One loud 20ms frame during bot speech (the observed echo-spike failure) must NOT
    open the gate — it is silenced, and so is the quiet frame after it."""
    gate, pushed, clock = _bot_speaking_gate(monkeypatch)

    _run(gate.process_frame(_audio_frame(peak=2000), FrameDirection.DOWNSTREAM))  # > 1500
    clock["t"] = 1000.02
    _run(gate.process_frame(_audio_frame(peak=100), FrameDirection.DOWNSTREAM))

    assert all(out.audio == b"\x00" * len(out.audio) for out, _d in pushed)


def test_bot_speech_sustained_crossing_opens_gate(monkeypatch):
    """~100ms of consecutive above-threshold frames = real barge-in → the gate opens,
    later frames pass, and the hangover then covers a mid-word dip."""
    gate, pushed, clock = _bot_speaking_gate(monkeypatch, bot_sustain_ms=100, hangover_ms=250)

    for i in range(6):                      # t = 0..100ms in 20ms frames, all loud
        clock["t"] = 1000.0 + i * 0.02
        _run(gate.process_frame(_audio_frame(peak=2000), FrameDirection.DOWNSTREAM))

    # The frame at +100ms (6th) reaches the sustain requirement and passes.
    assert pushed[-1][0].audio != b"\x00" * len(pushed[-1][0].audio)
    # A quiet mid-word dip right after still rides the hangover.
    clock["t"] = 1000.15
    quiet = _audio_frame(peak=50)
    quiet_audio = quiet.audio
    _run(gate.process_frame(quiet, FrameDirection.DOWNSTREAM))
    assert pushed[-1][0].audio == quiet_audio


def test_bot_speech_streak_resets_on_subthreshold_frame(monkeypatch):
    """A sub-threshold frame breaks the streak: two 60ms bursts separated by a quiet
    frame never open the gate (neither burst alone sustains 100ms)."""
    gate, pushed, clock = _bot_speaking_gate(monkeypatch, bot_sustain_ms=100)

    for i in range(4):                      # 0..60ms loud (not enough)
        clock["t"] = 1000.0 + i * 0.02
        _run(gate.process_frame(_audio_frame(peak=2000), FrameDirection.DOWNSTREAM))
    clock["t"] = 1000.08
    _run(gate.process_frame(_audio_frame(peak=100), FrameDirection.DOWNSTREAM))   # break
    for i in range(4):                      # a new 60ms burst — new streak, still short
        clock["t"] = 1000.10 + i * 0.02
        _run(gate.process_frame(_audio_frame(peak=2000), FrameDirection.DOWNSTREAM))

    assert all(out.audio == b"\x00" * len(out.audio) for out, _d in pushed)


def test_idle_regime_unchanged_instant_open(monkeypatch):
    """The sustain requirement applies ONLY while the bot speaks — idle keeps the
    instant open (no added latency on normal speech onsets)."""
    gate, pushed, clock = _make_gate(monkeypatch, bot_sustain_ms=100)
    frame = _audio_frame(peak=5000)
    expected = frame.audio
    _run(gate.process_frame(frame, FrameDirection.DOWNSTREAM))
    assert pushed[0][0].audio == expected


def test_bot_sustain_zero_restores_instant_barge_in(monkeypatch):
    """bot_sustain_ms=0 is the rollback knob: a single loud frame during bot speech
    opens the gate immediately (the pre-fix behavior)."""
    gate, pushed, clock = _bot_speaking_gate(monkeypatch, bot_sustain_ms=0)
    frame = _audio_frame(peak=2000)
    expected = frame.audio
    _run(gate.process_frame(frame, FrameDirection.DOWNSTREAM))
    assert pushed[0][0].audio == expected


def test_bot_transition_resets_streak(monkeypatch):
    """A bot stop/start transition resets the streak — loud frames straddling the
    transition must not be summed into one sustained crossing."""
    gate, pushed, clock = _bot_speaking_gate(monkeypatch, bot_sustain_ms=100)

    for i in range(4):                      # 60ms loud while bot speaks
        clock["t"] = 1000.0 + i * 0.02
        _run(gate.process_frame(_audio_frame(peak=2000), FrameDirection.DOWNSTREAM))
    _run(gate.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM))
    _run(gate.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM))
    pushed.clear()
    clock["t"] = 1000.10                    # 40ms more loud — would cross 100ms if summed
    _run(gate.process_frame(_audio_frame(peak=2000), FrameDirection.DOWNSTREAM))
    clock["t"] = 1000.12
    _run(gate.process_frame(_audio_frame(peak=2000), FrameDirection.DOWNSTREAM))

    assert all(out.audio == b"\x00" * len(out.audio) for out, _d in pushed)
