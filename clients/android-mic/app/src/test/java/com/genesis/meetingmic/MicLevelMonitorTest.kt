package com.genesis.meetingmic

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class MicLevelMonitorTest {

    private fun feed(m: MicLevelMonitor, peak: Int, fromMs: Long, toMs: Long) {
        var t = fromMs
        while (t <= toMs) {
            m.onFrame(peak, t)
            t += 100
        }
    }

    @Test
    fun startsGreyBeforeAnyAudio() {
        val m = MicLevelMonitor()
        m.start(0)
        assertEquals(MicSignal.GREY, m.read(100).signal)
    }

    @Test
    fun quietRoomNoiseIsGreen() {
        val m = MicLevelMonitor()
        m.start(0)
        feed(m, 300, 0, 10_000)
        val r = m.read(10_000)
        assertEquals(MicSignal.GREEN, r.signal)
        assertTrue(r.level > 0f)
    }

    @Test
    fun allZeroAudioGoesGreyThenRed() {
        val m = MicLevelMonitor()
        m.start(0)
        feed(m, 300, 0, 1_000)
        feed(m, 0, 1_100, 4_000)
        assertEquals(MicSignal.GREY, m.read(4_000).signal)
        feed(m, 0, 4_100, 6_200)
        val r = m.read(6_200)
        assertEquals(MicSignal.RED, r.signal)
        assertTrue(r.reason.contains("all-zero"))
    }

    @Test
    fun zerosFromTheStartAreRed() {
        val m = MicLevelMonitor()
        m.start(0)
        feed(m, 0, 0, 5_000)
        assertEquals(MicSignal.RED, m.read(5_000).signal)
    }

    @Test
    fun noFramesAtAllIsRedOnTheClockAlone() {
        val m = MicLevelMonitor()
        m.start(0)
        assertEquals(MicSignal.GREY, m.read(4_900).signal)
        val r = m.read(5_000)
        assertEquals(MicSignal.RED, r.signal)
        assertTrue(r.reason.contains("no audio frames"))
    }

    @Test
    fun framesThatStopAfterSpeechCannotStayGreen() {
        val m = MicLevelMonitor()
        m.start(0)
        feed(m, 5_000, 0, 3_000)
        assertEquals(MicSignal.GREEN, m.read(3_000).signal)
        assertEquals(MicSignal.GREY, m.read(5_500).signal)
        assertEquals(MicSignal.RED, m.read(8_000).signal)
    }

    @Test
    fun silencedIsRedImmediatelyAndClears() {
        val m = MicLevelMonitor()
        m.start(0)
        feed(m, 3_000, 0, 1_000)
        m.setSilenced(true)
        val r = m.read(1_000)
        assertEquals(MicSignal.RED, r.signal)
        assertTrue(r.reason.contains("muting"))
        m.setSilenced(false)
        assertEquals(MicSignal.GREEN, m.read(1_000).signal)
    }

    @Test
    fun recoversWhenSoundReturns() {
        val m = MicLevelMonitor()
        m.start(0)
        feed(m, 0, 0, 6_000)
        assertEquals(MicSignal.RED, m.read(6_000).signal)
        m.onFrame(800, 6_100)
        assertEquals(MicSignal.GREEN, m.read(6_100).signal)
    }

    @Test
    fun restartClearsSilencedAndHistory() {
        val m = MicLevelMonitor()
        m.start(0)
        m.setSilenced(true)
        m.start(10_000)
        assertEquals(MicSignal.GREY, m.read(10_100).signal)
    }

    @Test
    fun stoppedMonitorIsGreyAndIgnoresFrames() {
        val m = MicLevelMonitor()
        m.start(0)
        m.stop()
        m.onFrame(3_000, 100)
        val r = m.read(100)
        assertEquals(MicSignal.GREY, r.signal)
        assertEquals(0f, r.level)
    }

    @Test
    fun levelScaleBounds() {
        assertEquals(0f, MicLevelMonitor.levelFor(0))
        assertEquals(1f, MicLevelMonitor.levelFor(32767), 0.001f)
        assertEquals(0f, MicLevelMonitor.levelFor(10))  // below -60 dBFS clamps to 0
        val mid = MicLevelMonitor.levelFor(300)
        assertTrue(mid > 0.2f && mid < 0.5f)
    }

    @Test
    fun peakOfReadsLittleEndianSignedSamples() {
        // samples: 0x0100 = 256, 0xFF00 = -256, 0x8000 = -32768
        val buf = byteArrayOf(0x00, 0x01, 0x00, 0xFF.toByte(), 0x00, 0x80.toByte())
        assertEquals(32768, MicLevelMonitor.peakOf(buf, 6))
        assertEquals(256, MicLevelMonitor.peakOf(buf, 4))
        assertEquals(0, MicLevelMonitor.peakOf(ByteArray(3200), 3200))
    }

    @Test
    fun greenWindowAndRedThresholdBoundaries() {
        val m = MicLevelMonitor()
        m.start(0)
        m.onFrame(1_000, 0)
        assertEquals(MicSignal.GREEN, m.read(2_000).signal)   // exactly at the 2 s window
        assertEquals(MicSignal.GREY, m.read(2_001).signal)
        assertEquals(MicSignal.GREY, m.read(4_999).signal)
        assertEquals(MicSignal.RED, m.read(5_000).signal)     // exactly at the 5 s threshold
    }

    @Test
    fun oneRealFrameResetsTheZeroRun() {
        val m = MicLevelMonitor()
        m.start(0)
        feed(m, 0, 0, 4_000)
        m.onFrame(600, 4_100)
        feed(m, 0, 4_200, 8_000)
        // 3.8 s of zeros since the real frame: not yet red, and past the green window.
        assertEquals(MicSignal.GREY, m.read(8_000).signal)
        feed(m, 0, 8_100, 9_200)
        assertEquals(MicSignal.RED, m.read(9_200).signal)
    }

    @Test
    fun missingFramesOutrankAllZeroReason() {
        val m = MicLevelMonitor()
        m.start(0)
        feed(m, 0, 0, 6_000)
        // Frames stop entirely after 6 s of zeros: the reason names the stall, not the zeros.
        assertTrue(m.read(11_000).reason.contains("no audio frames"))
    }

    @Test
    fun silencedOutranksFreshSound() {
        val m = MicLevelMonitor()
        m.start(0)
        m.setSilenced(true)
        feed(m, 9_000, 0, 1_000)
        assertEquals(MicSignal.RED, m.read(1_000).signal)
    }

    @Test
    fun startAfterStopForgetsEarlierSound() {
        val m = MicLevelMonitor()
        m.start(0)
        feed(m, 9_000, 0, 1_000)
        m.stop()
        m.start(1_100)
        val r = m.read(1_200)
        assertEquals(MicSignal.GREY, r.signal)
        assertEquals(0f, r.level)
    }

    @Test
    fun levelDecaysSlowlyAfterLoudSound() {
        val m = MicLevelMonitor()
        m.start(0)
        m.onFrame(30_000, 0)
        val loud = m.read(0).level
        m.onFrame(100, 100)
        val after = m.read(100).level
        assertTrue(after < loud && after > MicLevelMonitor.levelFor(100))
    }
}
