package com.genesis.meetingmic

import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.util.concurrent.TimeUnit

/** The sync pass against a fake edge: deletion only ever follows proof. */
class SegmentSyncPassTest {
    private lateinit var server: MockWebServer
    private lateinit var dir: File
    private val http = OkHttpClient.Builder().readTimeout(2, TimeUnit.SECONDS).build()
    private val state = object : SegmentSync.State {
        val m = HashMap<String, Long>()
        override fun getLong(key: String) = m[key] ?: 0L
        override fun putLong(key: String, value: Long) { m[key] = value }
        override fun remove(key: String) { m.remove(key) }
        override fun keys(): Set<String> = m.keys.toSet()
    }
    private val now = System.currentTimeMillis()
    private val a = "seg_20261021T180000_0f8fad5b-d9cb-469f-a165-70867728950e_s0"
    private val b = "seg_20261021T180500_0f8fad5b-d9cb-469f-a165-70867728950e_s4800000"

    @Before fun setUp() {
        server = MockWebServer().apply { start() }
        dir = Files.createTempDirectory("sync").toFile()
    }

    @After fun tearDown() {
        try { server.shutdown() } catch (_: Exception) {}
        dir.deleteRecursively()
    }

    private val ep get() = server.url("/meeting/tok").toString().trimEnd('/')

    private fun segment(base: String, bytes: ByteArray, synced: Boolean = false, ageMs: Long = 60_000): File {
        val f = File(dir, base + if (synced) ".synced.aac" else ".aac")
        f.writeBytes(bytes)
        File(dir, "$base.json").writeText("""{"v":1}""")
        f.setLastModified(now - ageMs)
        return f
    }

    private fun stored(bytes: ByteArray) = MockResponse().setBody(
        """{"status":"stored","sha256":"${SegmentSync.sha256(bytes)}","bytes":${bytes.size},"created":true}""",
    )

    /** Every file routes to the fake edge unless a test gives it its own route. */
    private var route: (File) -> String? = { ep }

    private fun pass(low: Boolean = false) = SegmentSync.runPass(dir, route, state, low, http) { now }

    @Test
    fun provenReceiptMarksTheFileSynced() {
        val bytes = byteArrayOf(1, 2, 3, 4)
        segment(a, bytes)
        server.enqueue(stored(bytes))
        assertTrue(pass())
        assertTrue(File(dir, "$a.synced.aac").exists())
        assertFalse(File(dir, "$a.aac").exists())
        val req = server.takeRequest()
        assertTrue(req.path!!.contains("name=$a"))
        assertTrue(req.path!!.contains("sha256=${SegmentSync.sha256(bytes)}"))
    }

    @Test
    fun aReceiptForOtherBytesLeavesTheFileWaiting() {
        val bytes = byteArrayOf(1, 2, 3, 4)
        segment(a, bytes)
        server.enqueue(stored(byteArrayOf(9, 9)))
        assertFalse(pass())
        assertTrue(File(dir, "$a.aac").exists())
    }

    @Test
    fun oneFileTheEdgeCannotTakeDoesNotBlockTheNext() {
        val one = byteArrayOf(1)
        val two = byteArrayOf(2, 2)
        segment(a, one)
        segment(b, two)
        server.enqueue(MockResponse().setResponseCode(500))
        server.enqueue(stored(two))
        assertFalse(pass())
        assertTrue(File(dir, "$a.aac").exists())
        assertTrue(File(dir, "$b.synced.aac").exists())
    }

    @Test
    fun noAnswerStopsThePassAndKeepsEverything() {
        segment(a, byteArrayOf(1))
        server.shutdown()
        assertFalse(pass())
        assertTrue(File(dir, "$a.aac").exists())
    }

    @Test
    fun aRefusedFileIsKeptAndRemembered() {
        segment(a, byteArrayOf(1))
        server.enqueue(MockResponse().setResponseCode(409))
        assertFalse(pass())
        assertTrue(File(dir, "$a.aac").exists())
        assertTrue(state.getLong("rejected:$a.aac") > 0)
    }

    @Test
    fun pastGraceAndStillHeldDeletesThePhoneCopy() {
        segment(a, byteArrayOf(5), synced = true, ageMs = SegmentSync.GRACE_MS + 1)
        server.enqueue(MockResponse().setBody("""{"have":true}"""))
        assertTrue(pass())
        assertFalse(File(dir, "$a.synced.aac").exists())
        assertFalse(File(dir, "$a.json").exists())
        assertTrue(server.takeRequest().path!!.startsWith("/meeting/tok/have"))
    }

    @Test
    fun pastGraceButNoLongerHeldGoesBackToTheQueue() {
        segment(a, byteArrayOf(5), synced = true, ageMs = SegmentSync.GRACE_MS + 1)
        server.enqueue(MockResponse().setBody("""{"have":false}"""))
        assertFalse(pass())
        assertTrue(File(dir, "$a.aac").exists())
        assertTrue(File(dir, "$a.json").exists())
    }

    @Test
    fun anUnclearAnswerKeepsTheSyncedFile() {
        segment(a, byteArrayOf(5), synced = true, ageMs = SegmentSync.GRACE_MS + 1)
        server.enqueue(MockResponse().setResponseCode(500))
        assertFalse(pass())
        assertTrue(File(dir, "$a.synced.aac").exists())
        server.enqueue(MockResponse().setBody("""{"something":"else"}"""))
        assertFalse(pass())
        assertTrue(File(dir, "$a.synced.aac").exists())
    }

    @Test
    fun withinGraceNothingIsAskedOrDeleted() {
        segment(a, byteArrayOf(5), synced = true, ageMs = 60_000)
        assertTrue(pass())
        assertTrue(File(dir, "$a.synced.aac").exists())
        assertEquals(0, server.requestCount)
    }

    @Test
    fun lowStorageVerifiesAndFreesEarly() {
        segment(a, byteArrayOf(5), synced = true, ageMs = 60_000)
        server.enqueue(MockResponse().setBody("""{"have":true}"""))
        assertTrue(pass(low = true))
        assertFalse(File(dir, "$a.synced.aac").exists())
    }

    @Test
    fun aFileTooFreshToTouchMeansNotDoneYet() {
        segment(a, byteArrayOf(1), ageMs = 1_000)
        assertFalse(pass())
        assertEquals(0, server.requestCount)
    }

    // ── each recording goes only to the bridge it was captured for ─────────

    private fun destOf(f: File) = SegmentSync.destinationOf(dir, f, legacyDefault = null)

    @Test
    fun eachRecordingIsSentOnlyToItsOwnDestination() {
        val one = byteArrayOf(1)
        val two = byteArrayOf(2, 2)
        segment(a, one)
        segment(b, two)
        File(dir, "$a.dest").writeText("first")
        File(dir, "$b.dest").writeText("second")
        val eps = mapOf("first" to server.url("/meeting/old").toString().trimEnd('/'), "second" to ep)
        route = { f -> destOf(f)?.let { eps[it] } }
        server.enqueue(stored(one))
        server.enqueue(stored(two))
        assertTrue(pass())
        val paths = listOf(server.takeRequest().path!!, server.takeRequest().path!!)
        assertTrue(paths.any { it.startsWith("/meeting/old/upload") && it.contains("name=$a") })
        assertTrue(paths.any { it.startsWith("/meeting/tok/upload") && it.contains("name=$b") })
    }

    @Test
    fun aRecordingWithNoSecureDestinationIsNeverSent() {
        segment(a, byteArrayOf(1))
        File(dir, "$a.dest").writeText(SegmentSync.DEST_NONE)
        route = { f -> destOf(f)?.let { ep } }
        assertTrue(pass())  // nothing a retry could change, so the job is not kept retrying
        assertEquals(0, server.requestCount)
        assertTrue(File(dir, "$a.aac").exists())
    }

    @Test
    fun anUnreachableOldBridgeDoesNotHoldBackTheCurrentOne() {
        val dead = MockWebServer().apply { start() }
        val deadEp = dead.url("/meeting/old").toString().trimEnd('/')
        dead.shutdown()
        val two = byteArrayOf(2, 2)
        segment(a, byteArrayOf(1))
        segment(b, two)
        route = { f -> if (f.name.startsWith(a)) deadEp else ep }
        server.enqueue(stored(two))
        assertFalse(pass())  // a is still waiting
        assertTrue(File(dir, "$a.aac").exists())
        assertTrue(File(dir, "$b.synced.aac").exists())
    }

    @Test
    fun theStillHeldCheckAsksTheRecordingsOwnBridge() {
        segment(a, byteArrayOf(5), synced = true, ageMs = SegmentSync.GRACE_MS + 1)
        File(dir, "$a.dest").writeText("first")
        route = { f -> destOf(f)?.let { server.url("/meeting/old").toString().trimEnd('/') } }
        server.enqueue(MockResponse().setBody("""{"have":true}"""))
        assertTrue(pass())
        assertTrue(server.takeRequest().path!!.startsWith("/meeting/old/have"))
        assertFalse(File(dir, "$a.synced.aac").exists())
        assertFalse(File(dir, "$a.dest").exists())  // companions go with the audio
        assertFalse(File(dir, "$a.json").exists())
    }

    @Test
    fun anOlderRecordingIsBoundOnceAndNeverRedirected() {
        val f = segment(a, byteArrayOf(1))
        assertEquals("first", SegmentSync.destinationOf(dir, f, legacyDefault = "first"))
        // The default changes later (a new bridge): the recording keeps its first destination.
        assertEquals("first", SegmentSync.destinationOf(dir, f, legacyDefault = "second"))
        assertEquals("first", File(dir, "$a.dest").readText())
        // With no default yet, an unbound recording simply waits (and stays unbound).
        val g = segment(b, byteArrayOf(2))
        assertEquals(null, SegmentSync.destinationOf(dir, g, legacyDefault = null))
        assertFalse(File(dir, "$b.dest").exists())
    }

    @Test
    fun anEndpointIdIsTheBridgeAddressOnly() {
        val id = SegmentSync.endpointId("wss://bridge.example.com/meeting/")
        assertEquals(16, id.length)
        assertEquals(id, SegmentSync.endpointId(" wss://bridge.example.com/meeting "))
        assertFalse(id == SegmentSync.endpointId("wss://other.example.com/meeting/"))
    }

    // ── review round: legacy adoption, cutoff, liveness, alert ──────────────

    @Test
    fun theOldBuildsEndpointBecomesTheBacklogsDefaultAheadOfANewBridge() {
        val p = MemPrefs()
        p.m["ws_base"] = "wss://old.example.com/meeting/"
        p.m["token"] = "oldtok"
        val newId = SegmentSync.registerEndpoint(p, "wss://new.example.com/meeting/", "newtok")
        val oldId = SegmentSync.endpointId("wss://old.example.com/meeting/")
        assertEquals(oldId, p.getString("default", null))  // where the backlog was really going
        assertEquals("https://old.example.com/meeting/oldtok", p.getString("ep:$oldId", null))
        assertEquals("https://new.example.com/meeting/newtok", p.getString("ep:$newId", null))
        assertFalse(p.contains("ws_base") || p.contains("token"))  // no stray token left behind
        SegmentSync.registerEndpoint(p, "wss://third.example.com/meeting/", "t3")
        assertEquals(oldId, p.getString("default", null))  // set once, never moved
    }

    @Test
    fun aNewRecordingThatLostItsDestinationIsNotGivenTheDefault() {
        val f = segment(a, byteArrayOf(1))
        File(dir, "$a.json").writeText("""{"v":1,"utc_start_ms":$now}""")
        assertEquals(null, SegmentSync.destinationOf(dir, f, "first", legacyCutoffMs = now - 1))
        assertFalse(File(dir, "$a.dest").exists())
        val old = segment(b, byteArrayOf(2))
        File(dir, "$b.json").writeText("""{"v":1,"utc_start_ms":${now - 86_400_000}}""")
        assertEquals("first", SegmentSync.destinationOf(dir, old, "first", legacyCutoffMs = now - 1))
    }

    @Test
    fun aStatusReadBindsNothing() {
        val f = segment(a, byteArrayOf(1))
        assertEquals("first", SegmentSync.destinationOf(dir, f, "first", bind = false))
        assertFalse(File(dir, "$a.dest").exists())
    }

    @Test
    fun aSyncedFileWithNoDestinationDoesNotKeepTheJobRetrying() {
        segment(a, byteArrayOf(5), synced = true, ageMs = SegmentSync.GRACE_MS + 1)
        route = { null }
        assertTrue(pass())
        assertTrue(File(dir, "$a.synced.aac").exists())  // kept, under the 90-day rule
        assertEquals(0, server.requestCount)
    }

    @Test
    fun recordingsThatCanNeverSyncRaiseTheAlertAfterADay() {
        val day = 24L * 60 * 60 * 1000
        val quiet = SegmentSync.Status(0, 0, 0, 0, 0, 0, unroutable = 2, oldestUnroutableMs = day - 1)
        assertEquals(null, SyncWorker.stuckText(quiet))
        val stuck = quiet.copy(oldestUnroutableMs = day)
        assertTrue(SyncWorker.stuckText(stuck)!!.contains("no secure (wss://) destination"))
        val waiting = SegmentSync.Status(3, day, 0, 0, 0, 0)
        assertTrue(SyncWorker.stuckText(waiting)!!.contains("not reached Genesis"))
    }

    @Test
    fun anInterruptedMigrationReunitesTheDestinationToo() {
        val old = "seg_20261021T182322_r1_s0"
        val fresh = SegmentSync.migratedBase(old)!!
        File(dir, "$fresh.aac").writeBytes(byteArrayOf(1))
        File(dir, "$old.dest").writeText("first")
        SegmentSync.migrateLegacyNames(dir)
        assertEquals("first", File(dir, "$fresh.dest").readText())
        assertFalse(File(dir, "$old.dest").exists())
    }

    @Test
    fun aNewTokenForTheSameBridgeCarriesTheWaitingRecordingsOver() {
        val p = MemPrefs()
        val url = "wss://bridge.example.com/meeting/"
        val before = SegmentSync.registerEndpoint(p, url, "oldtok")
        val after = SegmentSync.registerEndpoint(p, url, "newtok")
        assertEquals(before, after)  // recordings bound to it keep the same destination ...
        assertEquals("https://bridge.example.com/meeting/newtok", p.getString("ep:$after", null))  // ... with the new token
        val other = SegmentSync.registerEndpoint(p, "wss://other.example.com/meeting/", "newtok")
        assertFalse(other == after)  // a different address never receives them
    }

    @Test
    fun aNewTokenLiftsTheRefusalDelaySoRefusedFilesRetryAtOnce() {
        val p = MemPrefs()
        val url = "wss://bridge.example.com/meeting/"
        SegmentSync.registerEndpoint(p, url, "oldtok", state)
        state.putLong("rejected:$a.aac", now)
        state.putLong("retry:$b.aac", now)
        SegmentSync.registerEndpoint(p, url, "oldtok", state)  // same credential: delay kept
        assertTrue(state.getLong("rejected:$a.aac") > 0)
        SegmentSync.registerEndpoint(p, url, "newtok", state)
        assertEquals(0L, state.getLong("rejected:$a.aac"))
        assertTrue(state.getLong("retry:$b.aac") > 0)  // a transient retry is not a refusal
    }

    @Test
    fun theHeadlineIsNeverASuccessWhileSomeRecordingsCannotSync() {
        val onlyUnroutable = SegmentSync.Status(0, 0, 3, 0, 0, 0, unroutable = 1, oldestUnroutableMs = 1)
        val head = syncLine(onlyUnroutable).lineSequence().first()
        assertFalse(head.contains("✓") || head.contains("nothing waiting"))
        assertEquals("Synced to Genesis: all recordings ✓", syncLine(onlyUnroutable.copy(unroutable = 0)).lineSequence().first())
    }

    @Test
    fun anUnreadableSidecarHoldsBackOnlyItsOwnFile() {
        val two = byteArrayOf(2, 2)
        segment(a, byteArrayOf(1))
        File(dir, "$a.json").delete()
        File(dir, "$a.json").mkdir()  // exists, but reading it throws
        segment(b, two)
        server.enqueue(stored(two))
        assertFalse(pass())
        assertTrue(File(dir, "$a.aac").exists())
        assertTrue(File(dir, "$b.synced.aac").exists())
        assertTrue(state.getLong("retry:$a.aac") > 0)
    }

    @Test
    fun aRecordingWithAnUnreadableSidecarStillUploadsAfterADay() {
        val one = byteArrayOf(1)
        segment(a, one, ageMs = 24L * 60 * 60 * 1000 + 1)
        File(dir, "$a.json").delete()
        File(dir, "$a.json").mkdir()
        server.enqueue(stored(one))
        assertTrue(pass())
        assertTrue(File(dir, "$a.synced.aac").exists())
        assertFalse(server.takeRequest().path!!.contains("meta="))
    }
}
