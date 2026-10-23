package com.genesis.meetingmic

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class SegmentSyncTest {
    private val sha = "a".repeat(64)

    // ── the receipt must prove OUR bytes ────────────────────────────────────
    @Test
    fun receiptWithMatchingHashAndLengthConfirms() {
        val body = """{"status":"stored","sha256":"$sha","bytes":1234,"created":true}"""
        assertTrue(SegmentSync.receiptConfirms(body, sha, 1234))
    }

    @Test
    fun aDifferentHashNeverConfirms() {
        val body = """{"status":"stored","sha256":"${"b".repeat(64)}","bytes":1234}"""
        assertFalse(SegmentSync.receiptConfirms(body, sha, 1234))
    }

    @Test
    fun aDifferentLengthNeverConfirms() {
        val body = """{"status":"stored","sha256":"$sha","bytes":1233}"""
        assertFalse(SegmentSync.receiptConfirms(body, sha, 1234))
    }

    @Test
    fun anythingButStoredNeverConfirms() {
        assertFalse(SegmentSync.receiptConfirms("""{"status":"rejected","sha256":"$sha","bytes":1234}""", sha, 1234))
        assertFalse(SegmentSync.receiptConfirms("""{"sha256":"$sha","bytes":1234}""", sha, 1234))
        assertFalse(SegmentSync.receiptConfirms("not json", sha, 1234))
        assertFalse(SegmentSync.receiptConfirms("", sha, 1234))
    }

    @Test
    fun outcomes() {
        assertEquals(SegmentSync.Outcome.CONFIRMED, SegmentSync.classify(200, true))
        // A 200 that does not prove our bytes is doubt: keep the file and try again.
        assertEquals(SegmentSync.Outcome.RETRY_FILE, SegmentSync.classify(200, false))
        for (code in listOf(408, 429, 500, 503, 507)) {
            assertEquals(SegmentSync.Outcome.RETRY_FILE, SegmentSync.classify(code, false))
        }
        for (code in listOf(400, 403, 409, 413, 422)) {
            assertEquals(SegmentSync.Outcome.REJECTED, SegmentSync.classify(code, false))
        }
    }

    // ── the edge is asked again a week after confirmation ──
    @Test
    fun theRecheckWaitsAWeek() {
        val now = 100L * 24 * 60 * 60 * 1000
        assertFalse(SegmentSync.recheckDue(now - SegmentSync.RECHECK_MS + 1, now))
        assertTrue(SegmentSync.recheckDue(now - SegmentSync.RECHECK_MS, now))
    }

    @Test
    fun syncedAndWaitingNamesShareOneBase() {
        val b = "seg_20261021T182322_0f8fad5b-d9cb-469f-a165-70867728950e_s0"
        assertEquals(b, SegmentSync.baseOf(File("$b.aac")))
        assertEquals(b, SegmentSync.baseOf(File("$b.synced.aac")))
        assertTrue(SegmentSync.isSynced(File("$b.synced.aac")))
        assertFalse(SegmentSync.isSynced(File("$b.aac")))
    }

    @Test
    fun sha256MatchesTheStandardVector() {
        assertEquals(
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            SegmentSync.sha256(ByteArray(0)),
        )
    }

    @Test
    fun legacyNamesGetAUniqueIdAndKeepTheirSidecar() {
        val dir = createTempDir()
        try {
            File(dir, "seg_20261021T182322_r1_s0.aac").writeBytes(byteArrayOf(1, 2, 3))
            File(dir, "seg_20261021T182322_r1_s0.json").writeText("{}")
            File(dir, "seg_20261021T182822_r1_s4800000.aac").writeBytes(byteArrayOf(4))
            SegmentSync.migrateLegacyNames(dir)
            val names = dir.list()!!.sorted()
            val rx = Regex("""seg_\d{8}T\d{6}_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}_s\d+\.(aac|json)""")
            assertEquals(3, names.size)
            assertTrue(names.all { rx.matches(it) })
            // the sidecar moved with its audio
            val aacBases = names.filter { it.endsWith(".aac") }.map { it.removeSuffix(".aac") }
            assertTrue(names.filter { it.endsWith(".json") }.all { it.removeSuffix(".json") in aacBases })
            assertTrue(File(dir, names.first { it.endsWith("_s0.aac") }).readBytes().contentEquals(byteArrayOf(1, 2, 3)))
        } finally {
            dir.deleteRecursively()
        }
    }

    @Test
    fun syncUsesOnlyAnEncryptedAddress() {
        assertEquals("https://bridge.example.com/meeting/t0k", SegmentSync.httpBase("wss://bridge.example.com/meeting/", "t0k"))
        assertEquals(null, SegmentSync.httpBase("ws://bridge.example.com/meeting/", "t0k"))
        assertEquals(null, SegmentSync.httpBase("wss://bridge.example.com/meeting/", " "))
    }

    @Test
    fun anInterruptedMigrationReunitesTheSidecarNextTime() {
        val dir = createTempDir()
        try {
            val old = "seg_20261021T182322_r1_s0"
            val fresh = SegmentSync.migratedBase(old)!!
            // The audio was renamed, then the app stopped before the sidecar followed.
            File(dir, "$fresh.synced.aac").writeBytes(byteArrayOf(1))
            File(dir, "$old.json").writeText("{\"utc_start_ms\":1}")
            SegmentSync.migrateLegacyNames(dir)
            assertEquals(listOf("$fresh.json", "$fresh.synced.aac"), dir.list()!!.sorted())
            assertEquals(fresh, SegmentSync.migratedBase(old))  // the same name every time
            assertEquals(null, SegmentSync.migratedBase(fresh))  // a migrated name is never renamed again
        } finally {
            dir.deleteRecursively()
        }
    }
}
