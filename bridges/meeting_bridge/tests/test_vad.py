"""Unit tests for the meeting-bridge VAD gate (pure logic — no aiohttp / cloud SDK / numpy)."""

import struct

from meeting_bridge.vad import GateStats, SessionGate, peak_amplitude


def _pcm(*samples: int) -> bytes:
    """Little-endian PCM16 frame from int16 samples."""
    return struct.pack(f"<{len(samples)}h", *samples)


# ── peak_amplitude ──────────────────────────────────────────────────────────
def test_peak_amplitude_basic():
    assert peak_amplitude(_pcm(0, 100, -300, 50)) == 300


def test_peak_amplitude_empty_and_odd():
    assert peak_amplitude(b"") == 0
    assert peak_amplitude(b"\x01") == 0  # single byte — no full int16 sample


def test_peak_amplitude_full_scale_negative_not_read_as_silence():
    # abs(-32768) must be 32768, not a wrapped-around negative → a loud frame, never "silence".
    assert peak_amplitude(_pcm(-32768)) == 32768


def test_peak_amplitude_drops_odd_trailing_byte():
    assert peak_amplitude(_pcm(500) + b"\x07") == 500


# ── SessionGate: disabled (threshold <= 0) ──────────────────────────────────
def test_gate_disabled_forwards_everything_never_closes():
    g = SessionGate(threshold=0, hangover_s=0.4, silence_close_s=60)
    assert g.enabled is False
    # Even zero-energy frames forward and never trigger a close (legacy single-session relay).
    for t in range(5):
        assert g.observe(0, now=float(t)) == (True, False)


# ── SessionGate: enabled ────────────────────────────────────────────────────
def test_gate_speech_forwards_and_marks_speech():
    g = SessionGate(threshold=500, hangover_s=0.4, silence_close_s=60)
    assert g.enabled is True
    assert g.observe(1200, now=1.0) == (True, False)


def test_gate_silence_before_any_speech_is_noop():
    g = SessionGate(threshold=500, hangover_s=0.4, silence_close_s=60)
    # Silence at the very start of a connection: nothing to forward, nothing to close.
    assert g.observe(20, now=0.0) == (False, False)


def test_gate_hangover_keeps_forwarding_briefly_after_speech():
    g = SessionGate(threshold=500, hangover_s=0.5, silence_close_s=60)
    g.observe(2000, now=1.0)  # speech at t=1.0
    # 0.3s later, silent frame still inside the 0.5s hangover → still forwarded.
    assert g.observe(10, now=1.3) == (True, False)


def test_gate_silence_past_hangover_drops_but_holds_session():
    g = SessionGate(threshold=500, hangover_s=0.5, silence_close_s=60)
    g.observe(2000, now=1.0)
    # 2s of silence: past hangover (drop) but well under silence_close (session stays open).
    assert g.observe(10, now=3.0) == (False, False)


def test_gate_sustained_silence_signals_close():
    g = SessionGate(threshold=500, hangover_s=0.5, silence_close_s=60)
    g.observe(2000, now=1.0)
    assert g.observe(10, now=1.0 + 60) == (False, True)  # silence >= silence_close_s → close


def test_gate_reset_returns_to_no_speech_state():
    g = SessionGate(threshold=500, hangover_s=0.5, silence_close_s=60)
    g.observe(2000, now=1.0)
    g.observe(10, now=100.0)  # would signal close
    g.reset()
    # After reset, a continuing silence is a no-op again (no repeated close signal).
    assert g.observe(10, now=101.0) == (False, False)
    # And fresh speech opens anew.
    assert g.observe(2000, now=102.0) == (True, False)


# ── GateStats ───────────────────────────────────────────────────────────────
def test_gatestats_disabled_returns_none():
    s = GateStats(log_interval_s=0)
    assert s.observe(1000, forwarded=True, now=0.0) is None
    assert s.observe(1000, forwarded=True, now=999.0) is None


def test_gatestats_emits_summary_after_interval():
    s = GateStats(log_interval_s=10)
    assert s.observe(100, forwarded=False, now=0.0) is None  # window opens
    assert s.observe(900, forwarded=True, now=5.0) is None
    out = s.observe(500, forwarded=True, now=10.0)  # interval elapsed → summary
    assert out is not None
    assert out.forwarded == 2 and out.gated == 1
    assert out.max_peak == 900
    assert out.avg_peak == (100 + 900 + 500) // 3


def test_gatestats_resets_between_windows():
    s = GateStats(log_interval_s=10)
    s.observe(100, forwarded=True, now=0.0)
    s.observe(100, forwarded=True, now=10.0)  # emits the first window + resets
    out = s.observe(50, forwarded=False, now=20.0)  # new window's summary (only this frame)
    assert out is not None
    assert out.forwarded == 0 and out.gated == 1 and out.max_peak == 50
