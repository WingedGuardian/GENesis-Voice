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

    private fun pass(low: Boolean = false) = SegmentSync.runPass(dir, ep, state, low, http) { now }

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
}
