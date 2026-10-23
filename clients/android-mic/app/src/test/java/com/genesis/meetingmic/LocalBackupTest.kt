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

    // ── retention (owner rule: keep 90 days; under pressure only copies the edge holds go early) ──
    private val day = RetentionPolicy.DAY_MS
    private val now = 1_000L * day
    private val segs = listOf(
        RetentionPolicy.Segment("d100", now - 100 * day),
        RetentionPolicy.Segment("d60", now - 60 * day),
        RetentionPolicy.Segment("d44", now - 44 * day),
        RetentionPolicy.Segment("today", now - 1000),
        RetentionPolicy.Segment("held30", now - 30 * day, synced = true),
        RetentionPolicy.Segment("held5", now - 5 * day, synced = true),
        RetentionPolicy.Segment("rechecked20", now - 20 * day, synced = true, checked = true),
    )

    /** Runs the sweep with room appearing after [roomAfter] removals; returns what it removed. */
    private fun sweep(roomAfter: Int): List<String> {
        val gone = mutableListOf<String>()
        RetentionPolicy.sweep(segs, now, hasRoom = { gone.size >= roomAfter }) { gone += it.id }
        return gone
    }

    @Test
    fun withRoomOnlyOlderThan90DaysGoEvenIfTheEdgeHoldsACopy() {
        assertEquals(listOf("d100"), sweep(roomAfter = 0))
    }

    @Test
    fun underPressureReconfirmedCopiesGoFirstThenOtherHeldCopies() {
        assertEquals(
            listOf("rechecked20", "held30", "held5"),
            RetentionPolicy.underPressure(segs, now).map { it.id },
        )
    }

    @Test
    fun underPressureTheSweepStopsOnceThereIsRoom() {
        assertEquals(listOf("d100", "rechecked20"), sweep(roomAfter = 2))
    }

    @Test
    fun theOnlyCopyIsNeverDeletedEarlyHoweverFullThePhoneIs() {
        val gone = sweep(roomAfter = Int.MAX_VALUE)
        assertEquals(listOf("d100", "rechecked20", "held30", "held5"), gone)
        assertTrue("d60" !in gone && "d44" !in gone && "today" !in gone)
    }

    @Test
    fun exactly90DaysIsKept() {
        val edge = listOf(RetentionPolicy.Segment("edge", now - 90 * day, synced = true))
        assertTrue(RetentionPolicy.expired(edge, now).isEmpty())
    }

    // ── purge on a real folder ──
    private fun recording(dir: java.io.File, base: String, ageDays: Long, synced: Boolean): java.io.File {
        val start = System.currentTimeMillis() - ageDays * day
        java.io.File(dir, "$base.json").writeText("""{"v":1,"utc_start_ms":$start}""")
        java.io.File(dir, "$base.dest").writeText("first")
        return java.io.File(dir, base + if (synced) ".synced.aac" else ".aac").apply { writeBytes(byteArrayOf(1)) }
    }

    @Test
    fun underPressurePurgeRemovesHeldCopiesWithTheirCompanionsAndKeepsTheOnlyCopy() {
        val dir = java.nio.file.Files.createTempDirectory("purge").toFile()
        try {
            recording(dir, "seg_held", ageDays = 3, synced = true)
            java.io.File(dir, "seg_held.checked").writeText("")
            recording(dir, "seg_only", ageDays = 80, synced = false)
            val r = LocalRecorder.purgeDir(dir, System.currentTimeMillis()) { false }
            assertEquals(1, r.removed)
            assertEquals(0, r.expiredUnsynced)
            assertEquals(listOf("seg_only.aac", "seg_only.dest", "seg_only.json"), dir.list()!!.sorted())
        } finally {
            dir.deleteRecursively()
        }
    }

    @Test
    fun aHeldCopyTheEdgeDisownsMidPurgeIsNotDeleted() {
        val dir = java.nio.file.Files.createTempDirectory("purge").toFile()
        try {
            val held = recording(dir, "seg_a", ageDays = 3, synced = true)
            // The sync pass re-queues it between purge's listing and its delete.
            val r = LocalRecorder.purgeDir(dir, System.currentTimeMillis()) {
                held.renameTo(java.io.File(dir, "seg_a.aac"))
                false
            }
            assertEquals(0, r.removed)
            assertTrue(java.io.File(dir, "seg_a.aac").exists())
            assertTrue(java.io.File(dir, "seg_a.json").exists())
        } finally {
            dir.deleteRecursively()
        }
    }

    @Test
    fun anExpiredUnsyncedRecordingIsCountedAsLost() {
        val dir = java.nio.file.Files.createTempDirectory("purge").toFile()
        try {
            recording(dir, "seg_old", ageDays = 91, synced = false)
            val r = LocalRecorder.purgeDir(dir, System.currentTimeMillis()) { true }
            assertEquals(1, r.removed)
            assertEquals(1, r.expiredUnsynced)
            assertTrue(dir.list()!!.isEmpty())
        } finally {
            dir.deleteRecursively()
        }
    }

    @Test
    fun aHeldCopyPast90DaysExpiresAndIsNotAlsoAPressureCandidate() {
        val old = listOf(RetentionPolicy.Segment("old", now - 91 * day, synced = true))
        assertEquals(listOf("old"), RetentionPolicy.expired(old, now).map { it.id })
        assertTrue(RetentionPolicy.underPressure(old, now).isEmpty())
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
