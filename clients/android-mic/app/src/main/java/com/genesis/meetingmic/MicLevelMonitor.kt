package com.genesis.meetingmic

import kotlin.math.abs
import kotlin.math.log10

enum class MicSignal { GREY, GREEN, RED }

data class MicReading(
    val signal: MicSignal,
    val reason: String,
    /** 0..1 meter position (log scale, -60 dBFS → 0). */
    val level: Float,
)

/**
 * Pure, fail-closed judgement of whether the microphone is actually hearing anything.
 *
 * GREEN must be EARNED by fresh evidence: a non-zero frame within [greenWindowMs]. Everything else is
 * GREY (not yet known) or RED (known bad). A silenced capture delivers exact digital zeros, which a real
 * mic never produces even in a quiet room (measured on a live install: quiet room peak ≈ 270–700,
 * a muted capture peak = 0), so "all zeros" is a clean signal rather than a loudness heuristic.
 *
 * Time comes in as a monotonic clock from the caller. [read] is called on a timer as well as per frame,
 * so a capture loop that stops delivering frames cannot freeze the signal green.
 */
class MicLevelMonitor(
    private val greenWindowMs: Long = 2_000,
    private val redAfterMs: Long = 5_000,
) {
    private var startedAtMs = -1L
    private var lastFrameMs = -1L
    private var lastNonZeroMs = -1L
    private var zeroRunStartMs = -1L
    private var silenced = false
    private var level = 0f

    @Synchronized
    fun start(nowMs: Long) {
        startedAtMs = nowMs
        lastFrameMs = -1L
        lastNonZeroMs = -1L
        zeroRunStartMs = -1L
        silenced = false
        level = 0f
    }

    @Synchronized
    fun stop() {
        startedAtMs = -1L
        level = 0f
    }

    @Synchronized
    fun onFrame(peak: Int, nowMs: Long) {
        if (startedAtMs < 0) return
        lastFrameMs = nowMs
        if (peak > 0) {
            lastNonZeroMs = nowMs
            zeroRunStartMs = -1L
        } else if (zeroRunStartMs < 0) {
            zeroRunStartMs = nowMs
        }
        // Fast attack, slow decay, so the meter is readable at a glance.
        val target = levelFor(peak)
        level = if (target >= level) target else level * 0.8f + target * 0.2f
    }

    /** Fed from the platform's recording-configuration callback (API 29+). */
    @Synchronized
    fun setSilenced(value: Boolean) {
        silenced = value
    }

    @Synchronized
    fun read(nowMs: Long): MicReading {
        if (startedAtMs < 0) return MicReading(MicSignal.GREY, "mic off", 0f)
        if (silenced) return MicReading(MicSignal.RED, "Android is muting this app's mic", 0f)
        if (lastNonZeroMs >= 0 && nowMs - lastNonZeroMs <= greenWindowMs) {
            return MicReading(MicSignal.GREEN, "hearing sound", level)
        }
        val sinceFrame = nowMs - (if (lastFrameMs >= 0) lastFrameMs else startedAtMs)
        if (sinceFrame >= redAfterMs) {
            return MicReading(MicSignal.RED, "no audio frames from the mic", 0f)
        }
        if (zeroRunStartMs >= 0 && nowMs - zeroRunStartMs >= redAfterMs) {
            return MicReading(MicSignal.RED, "mic is getting no sound (all-zero audio)", 0f)
        }
        return MicReading(MicSignal.GREY, "waiting for mic audio", level)
    }

    companion object {
        /** Peak absolute amplitude of little-endian PCM16 in `buf[0, n)`. */
        fun peakOf(buf: ByteArray, n: Int): Int {
            var peak = 0
            var i = 0
            while (i + 1 < n) {
                val sample = (buf[i].toInt() and 0xFF) or (buf[i + 1].toInt() shl 8)
                val a = abs(sample.toShort().toInt())
                if (a > peak) peak = a
                i += 2
            }
            return peak
        }

        fun levelFor(peak: Int): Float {
            if (peak <= 0) return 0f
            val dbfs = 20.0 * log10(peak / 32768.0)
            return ((dbfs + 60.0) / 60.0).toFloat().coerceIn(0f, 1f)
        }
    }
}
