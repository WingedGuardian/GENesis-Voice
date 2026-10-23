package com.genesis.meetingmic

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class LocalBackupTest {

    // ── ADTS ────────────────────────────────────────────────────────────────
    @Test
    fun headerMatchesTheReferenceEncoderForLc16kMono() {
        // A 0x2a3f-style header from a reference AAC-LC 16 kHz mono ADTS file: fff1 6040 .. .. fc.
        val h = Adts.header(100)
        assertEquals(7, h.size)
        assertArrayEquals(byteArrayOf(0xFF.toByte(), 0xF1.toByte(), 0x60, 0x40), h.copyOfRange(0, 4))
        assertEquals(0xFC.toByte(), h[6])
    }

    @Test
    fun headerEncodesTheFrameLength() {
        val h = Adts.header(300)
        val len = ((h[3].toInt() and 0x03) shl 11) or
            ((h[4].toInt() and 0xFF) shl 3) or
            ((h[5].toInt() and 0xE0) shr 5)
        assertEquals(307, len)
    }

    private fun frame(payload: Int) = Adts.header(payload) + ByteArray(payload) { 1 }

    @Test
    fun frameOffsetsFindsEveryCompleteFrame() {
        val data = frame(50) + frame(80) + frame(60)
        assertEquals(listOf(0, 57, 144), Adts.frameOffsets(data))
    }

    @Test
    fun aTornLastFrameIsDropped() {
        val whole = frame(50) + frame(80)
        val torn = whole.copyOf(whole.size - 10)
        assertEquals(listOf(0), Adts.frameOffsets(torn))
    }

    @Test
    fun scanStopsAtGarbage() {
        val data = frame(50) + byteArrayOf(1, 2, 3, 4, 5, 6, 7, 8) + frame(50)
        assertEquals(listOf(0), Adts.frameOffsets(data))
    }

    // ── retention (owner rule: 90 days; 45 when storage is low; never newer than 45) ──
    private val day = RetentionPolicy.DAY_MS
    private val now = 1_000L * day
    private val segs = listOf(
        RetentionPolicy.Segment("d100", now - 100 * day),
        RetentionPolicy.Segment("d60", now - 60 * day),
        RetentionPolicy.Segment("d44", now - 44 * day),
        RetentionPolicy.Segment("today", now - 1000),
    )

    @Test
    fun normallyOnlyOlderThan90DaysGo() {
        assertEquals(listOf("d100"), RetentionPolicy.toDelete(segs, now, lowStorage = false).map { it.id })
    }

    @Test
    fun lowStorageAlsoRemovesOlderThan45Days() {
        assertEquals(listOf("d100", "d60"), RetentionPolicy.toDelete(segs, now, lowStorage = true).map { it.id })
    }

    @Test
    fun nothingNewerThan45DaysIsEverDeleted() {
        val kept = RetentionPolicy.toDelete(segs, now, lowStorage = true).map { it.id }
        assertTrue("d44" !in kept && "today" !in kept)
    }

    @Test
    fun exactly90DaysIsKept() {
        val edge = listOf(RetentionPolicy.Segment("edge", now - 90 * day))
        assertTrue(RetentionPolicy.toDelete(edge, now, lowStorage = false).isEmpty())
    }

    // ── local-backup signal (fail-closed) ──────────────────────────────────
    @Test
    fun greenOnlyWhileBytesAdvance() {
        val m = LocalBackupMonitor()
        val g = m.start(0)
        assertEquals(MicSignal.GREY, m.read(1_000).signal)
        m.onBytesWritten(g, 4_000, 2_000)
        assertEquals(MicSignal.GREEN, m.read(5_000).signal)
        m.onBytesWritten(g, 4_000, 9_000)  // no progress
        assertEquals(MicSignal.GREY, m.read(13_000).signal)
        assertEquals(MicSignal.RED, m.read(22_000).signal)
    }

    @Test
    fun neverWritingTurnsRed() {
        val m = LocalBackupMonitor()
        m.start(0)
        assertEquals(MicSignal.RED, m.read(20_000).signal)
    }

    @Test
    fun aNamedFailureWinsAndClears() {
        val m = LocalBackupMonitor()
        val g = m.start(0)
        m.onBytesWritten(g, 10, 100)
        m.setFailure(g, "Local backup OFF: phone storage is full")
        val r = m.read(200)
        assertEquals(MicSignal.RED, r.signal)
        assertTrue(r.reason.contains("storage is full"))
        m.setFailure(g, null)
        m.onBytesWritten(g, 20, 300)
        assertEquals(MicSignal.GREEN, m.read(400).signal)
    }

    @Test
    fun stoppedIsGreyOff() {
        val m = LocalBackupMonitor()
        m.start(0)
        m.stop()
        assertEquals("off", m.read(100).reason)
    }

    @Test
    fun aDrainingOldWriterCannotFakeOrHideProgress() {
        val m = LocalBackupMonitor()
        val old = m.start(0)
        m.onBytesWritten(old, 900_000, 100)
        val current = m.start(1_000)
        // The old writer's final flush (bigger totals) must not mark the new run as saving...
        m.onBytesWritten(old, 950_000, 1_100)
        m.setFailure(old, "Local backup OFF: recorder error")
        assertEquals(MicSignal.GREY, m.read(1_200).signal)
        // ...and the new writer's small totals must still count as progress.
        m.onBytesWritten(current, 4_000, 2_000)
        assertEquals(MicSignal.GREEN, m.read(2_100).signal)
    }

    @Test
    fun lastCheckFromAnOldWriterIsIgnored() {
        val m = LocalBackupMonitor()
        val old = m.start(0)
        m.start(10)
        m.setLastCheck(old, "last file verified: 5:00")
        assertEquals("", m.read(20).lastCheck)
    }
}
