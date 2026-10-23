package com.genesis.meetingmic

import android.content.Context
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.os.PowerManager
import android.os.SystemClock
import android.os.storage.StorageManager
import android.util.Log
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.text.SimpleDateFormat
import java.util.Collections
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit

/**
 * The phone-side copy of every capture, independent of the live stream.
 *
 * The capture loop hands each PCM frame to [offer], which copies it into a bounded queue and never
 * blocks; a dedicated writer thread encodes AAC-LC (32 kbps, 16 kHz mono) and writes raw ADTS frames
 * into 5-minute segment files. Either path can fail without stopping the other: a full disk or an
 * encoder failure turns the local backup off (visibly, via [LocalBackupMonitor]) while the stream
 * continues, and a lost connection never touches local recording.
 *
 * Each segment has a JSON sidecar written when it OPENS (UTC start, capture-run id, the run's sample
 * position at its first frame), so a crash mid-segment still leaves a dated, positioned file. Frames
 * the queue had to drop are carried IN ORDER as a count on the next frame, and the writer encodes that
 * much silence in their place, so sample positions track real capture time without fragmenting files.
 */
class LocalRecorder(
    private val context: Context,
    private val monitor: LocalBackupMonitor,
    private val onSegmentClosed: () -> Unit = {},
) {

    companion object {
        private const val TAG = "LocalRecorder"
        const val SAMPLE_RATE = 16_000
        const val BIT_RATE = 32_000
        const val SEGMENT_SAMPLES = 5L * 60 * SAMPLE_RATE
        // Below this the writer stops (and frees space by the retention rule) so the phone keeps room;
        // it resumes only above RESUME_BYTES, so it does not flap at the edge.
        const val FLOOR_BYTES = 300L * 1024 * 1024
        const val RESUME_BYTES = 400L * 1024 * 1024
        // Below this, retention frees room early by removing copies the edge holds, only until there
        // is room again. Unsynced recordings are never removed early.
        const val LOW_STORAGE_BYTES = 1024L * 1024 * 1024
        private const val STORAGE_CHECK_MS = 30_000L
        private const val SYNC_MS = 5_000L
        private const val QUEUE_FRAMES = 150  // ~15 s of 100 ms frames
        // Dropped audio up to this long is filled with silence in place; a longer gap starts a new file.
        private const val MAX_SILENCE_FILL_SAMPLES = 60L * SAMPLE_RATE
        private const val FEED_DEADLINE_MS = 2_000L
        private const val ENCODER_RETRY_MS = 30_000L
        private const val ENCODER_MAX_FAILURES = 3
        private const val WAKE_LEASE_MS = 60_000L
        private const val PREVIOUS_WRITER_WAIT_MS = 10_000L
        private const val MIME = MediaFormat.MIMETYPE_AUDIO_AAC

        /** Segments a writer currently has open; retention never touches them. */
        private val openBases: MutableSet<String> = Collections.newSetFromMap(ConcurrentHashMap())

        /** True while a writer still has the segment with this base name open. */
        fun isOpen(base: String): Boolean = base in openBases

        /**
         * The most recently started writer in this process, whichever service instance started it: a
         * Stop ends the service, so the next Start has a new recorder but must still wait for the old
         * writer to release its encoder.
         */
        private val lastWriter = java.util.concurrent.atomic.AtomicReference<Thread?>(null)

        fun dir(context: Context): File =
            (context.getExternalFilesDir("recordings") ?: File(context.filesDir, "recordings"))
                .apply { mkdirs() }

        /** Bytes the app could allocate in [dir]; Long.MAX_VALUE when unknown (never blocks recording). */
        fun allocatableBytes(context: Context, dir: File): Long = try {
            val sm = context.getSystemService(StorageManager::class.java)
            sm.getAllocatableBytes(sm.getUuidForPath(dir))
        } catch (e: Exception) {
            dir.usableSpace.takeIf { it > 0 } ?: Long.MAX_VALUE
        }

        /**
         * Applies [RetentionPolicy] to the recordings folder and returns how many segments it removed.
         * Never touches an open segment or one dated in the future (a clock that was wrong when it was
         * written), and clears sidecars whose audio file is gone.
         */
        fun purge(context: Context, nowUtcMs: Long = System.currentTimeMillis()): Int {
            val d = dir(context)
            val r = purgeDir(d, nowUtcMs) { allocatableBytes(context, d) >= LOW_STORAGE_BYTES }
            // Never silent: a recording that expired before reaching the edge is reported.
            SegmentSync.noteExpiredUnsynced(context, r.expiredUnsynced)
            return r.removed
        }

        class Purged(val removed: Int, val expiredUnsynced: Int)

        /** [purge] on a folder, with [hasRoom] answering whether storage is fine (Context-free, tested). */
        fun purgeDir(d: File, nowUtcMs: Long, hasRoom: () -> Boolean): Purged {
            // Finish any interrupted rename first, so its sidecar is not swept as an orphan below.
            SegmentSync.migrateLegacyNames(d)
            val files = d.listFiles { f -> f.name.endsWith(".aac") }.orEmpty()
                .filter { SegmentSync.baseOf(it) !in openBases }
                .associateBy { it.name }
            val segments = files.values
                .map {
                    val checked = File(d, SegmentSync.baseOf(it) + ".checked").exists()
                    RetentionPolicy.Segment(it.name, segmentStartMs(it), SegmentSync.isSynced(it), checked)
                }
                .filter { it.startUtcMs <= nowUtcMs + RetentionPolicy.DAY_MS }
            var expiredUnsynced = 0
            var removed = 0
            SegmentSync.folderLock.lock()
            try {
                RetentionPolicy.sweep(segments, nowUtcMs, hasRoom) { s ->
                    val f = files.getValue(s.id)
                    // Renamed by sync since the listing (or already gone): judged next time. This is what
                    // keeps a copy the edge disowned (renamed back to X.aac) from going as a held one.
                    if (!f.exists()) return@sweep
                    // The sidecar goes only once the audio is really gone: a recording that survives a
                    // failed delete keeps its timing.
                    if (!f.delete() && f.exists()) return@sweep
                    if (!SegmentSync.isSynced(f)) expiredUnsynced += 1
                    SegmentSync.deleteCompanions(d, SegmentSync.baseOf(f))
                    removed += 1
                }
                d.listFiles { f -> SegmentSync.COMPANIONS.any { f.name.endsWith(it) } }.orEmpty()
                    .filter { it.nameWithoutExtension !in openBases }
                    .filter {
                        !File(d, it.nameWithoutExtension + ".aac").exists() &&
                            !File(d, it.nameWithoutExtension + ".synced.aac").exists()
                    }
                    .filter { nowUtcMs - it.lastModified() > 60 * 60 * 1000L }
                    .forEach { it.delete() }
            } finally {
                SegmentSync.folderLock.unlock()
            }
            return Purged(removed, expiredUnsynced)
        }

        /** One line describing what is stored on this phone, read straight from the folder. */
        fun summary(context: Context, nowUtcMs: Long = System.currentTimeMillis()): String {
            val files = dir(context).listFiles { f -> f.name.endsWith(".aac") }.orEmpty()
            if (files.isEmpty()) return "On this phone: no recordings yet"
            val mb = files.sumOf { it.length() } / (1024.0 * 1024.0)
            val oldestDays = (nowUtcMs - files.minOf { segmentStartMs(it) }) / RetentionPolicy.DAY_MS
            val newest = files.maxByOrNull { segmentStartMs(it) }
            val frames = newest?.let { Adts.frameOffsets(it.readBytes()).size } ?: 0
            val secs = frames.toLong() * Adts.SAMPLES_PER_FRAME / SAMPLE_RATE
            return "On this phone: %d recording files, %.1f MB, oldest %d days (kept up to 90). Newest: %d:%02d of valid audio."
                .format(files.size, mb, oldestDays, secs / 60, secs % 60)
        }

        private fun segmentStartMs(aac: File): Long {
            val side = File(aac.parentFile, SegmentSync.baseOf(aac) + ".json")
            return try {
                JSONObject(side.readText()).getLong("utc_start_ms")
            } catch (e: Exception) {
                aac.lastModified()
            }
        }
    }

    /** One captured frame, with its capture wall time (the writer may run up to ~15 s behind). */
    private class Frame(val data: ByteArray, val droppedBefore: Long, val capturedUtcMs: Long)

    @Volatile private var writer: Writer? = null

    /**
     * Starts a writer for capture run [runId]. A previous writer finishes on its own thread, and the
     * new one opens no encoder until the old one has released its own (some phones have only one).
     */
    @Synchronized
    fun start(runId: Long, generation: Long, runUuid: String, dest: String) {
        writer?.finish()
        val w = Writer(runId, generation, runUuid, dest)
        w.previous = lastWriter.getAndSet(w)
        writer = w
        w.start()
    }

    /** Non-blocking hand-off from run [runId]'s capture loop; drops (and records) the frame if the queue is full. */
    fun offer(runId: Long, buf: ByteArray, n: Int) {
        writer?.takeIf { it.runId == runId }?.offer(buf, n)
    }

    /**
     * Non-blocking: run [runId]'s writer drains what it has, closes its segment, and exits. Called by
     * the capture loop once it can offer nothing more, so the last frame read before Stop is kept;
     * a later run's writer is never touched.
     */
    @Synchronized
    fun stop(runId: Long) {
        val w = writer ?: return
        if (w.runId != runId) return
        w.finish()
        writer = null
    }

    private enum class Off { STORAGE, IO, ENCODER, ENCODER_FATAL }

    /** How a segment close went; anything but OK means the file may be missing audio. */
    private enum class Closed { OK, ENCODER, IO }

    /**
     * One capture run's writer. Every monitor call carries [generation], so a writer still draining
     * after the next run started can never change the new run's status.
     */
    private inner class Writer(
        val runId: Long,
        private val generation: Long,
        // Globally unique per capture run: segment names must never repeat across app restarts
        // (the numeric runId restarts at 1 with the process), because the edge stores by name.
        private val runUuid: String,
        // Where this run's recordings sync to (an endpoint id, or SegmentSync.DEST_NONE), fixed at
        // capture so a later change of bridge or token cannot redirect them.
        private val dest: String,
    ) : Thread("local-recorder-$runId") {
        /** The writer started before this one, possibly by another service instance; joined before any encoder opens. */
        @Volatile var previous: Thread? = null
        private val queue = ArrayBlockingQueue<Frame>(QUEUE_FRAMES)
        private val stopFrame = Frame(ByteArray(0), 0, 0)
        @Volatile private var finishing = false
        @Volatile private var finishedAtUtcMs = 0L
        // Written by the capture thread; the writer takes what is left over once capture has finished.
        @Volatile private var pendingDropped = 0L

        private var codec: MediaCodec? = null
        private var out: FileOutputStream? = null
        private var currentBase: String? = null
        private var segmentSamples = 0L
        private var runSamples = 0L
        private var bytesWritten = 0L
        private var off: Off? = null
        private var encoderFailures = 0
        private var retryAtMs = 0L
        private var lastStorageCheck = 0L
        private var lastSync = 0L
        private val info = MediaCodec.BufferInfo()
        private val silence = ByteArray(3200)

        fun offer(buf: ByteArray, n: Int) {
            if (finishing) return
            if (queue.offer(Frame(buf.copyOf(n), pendingDropped, System.currentTimeMillis()))) {
                pendingDropped = 0
            } else {
                pendingDropped += n / 2
            }
        }

        fun finish() {
            finishedAtUtcMs = System.currentTimeMillis()
            finishing = true
            queue.offer(stopFrame)  // may fail on a full queue; the loop also exits once drained
        }

        override fun run() {
            // Hold our own wake lock: after Stop the service lets go of its lock right away, but
            // the queued audio still has to be encoded, written and fsynced.
            // Renewed in short leases for as long as the writer runs (a single long lease would expire in
            // a long meeting, exactly when the final drain needs it).
            var wake: PowerManager.WakeLock? = null
            var leasedAt = 0L
            fun renewWake() {
                try {
                    val w = wake ?: (context.getSystemService(Context.POWER_SERVICE) as PowerManager)
                        .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "GenesisMeetingMic::local-writer")
                        .apply { setReferenceCounted(false) }
                        .also { wake = it }
                    w.acquire(WAKE_LEASE_MS)
                    leasedAt = SystemClock.elapsedRealtime()
                } catch (_: Throwable) {
                    // No wake lock is a degraded backup, never a reason to stop writing.
                }
            }
            renewWake()
            try {
                // The previous run's writer is draining at most ~15 s of queued audio; frames for this
                // run wait in our own queue meanwhile.
                previous?.join(PREVIOUS_WRITER_WAIT_MS)
                previous = null
                try { purge(context) } catch (e: Exception) { Log.w(TAG, "retention purge failed", e) }
                checkStorage(force = true)
                while (true) {
                    val f = queue.poll(500, TimeUnit.MILLISECONDS)
                    if (SystemClock.elapsedRealtime() - leasedAt > WAKE_LEASE_MS / 2) renewWake()
                    if (f === stopFrame || (f == null && finishing)) break
                    checkStorage(force = false)
                    if (f == null) continue
                    if (f.droppedBefore > 0) fillDropped(f.droppedBefore, f.capturedUtcMs)
                    if (writable()) write(f.data, f.capturedUtcMs)
                    runSamples += f.data.size / 2
                }
                // Audio dropped after the last queued frame still happened: keep the file as long as
                // the capture, ending at the moment Stop was pressed.
                val tail = pendingDropped
                pendingDropped = 0
                if (tail > 0) fillDropped(tail, finishedAtUtcMs)
            } catch (e: InterruptedException) {
                // fall through to close
            } catch (t: Throwable) {
                // Never let the backup take down the process (and with it the live stream).
                Log.e(TAG, "local recorder crashed", t)
                monitor.setFailure(generation, "Local backup OFF: recorder error")
            } finally {
                try { closeSegment() } catch (_: Throwable) {}
                try { wake?.let { if (it.isHeld) it.release() } } catch (_: Throwable) {}
            }
        }

        private fun writable(): Boolean = when (off) {
            null -> true
            Off.ENCODER -> SystemClock.elapsedRealtime() >= retryAtMs
            else -> false
        }

        /** Keeps sample positions true after a queue overflow: silence in place, or a new file. */
        private fun fillDropped(samples: Long, nextFrameUtcMs: Long) {
            Log.w(TAG, "local backup queue overflowed; $samples samples dropped")
            if (samples > MAX_SILENCE_FILL_SAMPLES || !writable()) {
                closeSegment()
                runSamples += samples
                return
            }
            var left = samples
            while (left > 0) {
                if (!writable()) {  // the writer went off mid-fill: account for the rest, stop
                    runSamples += left
                    return
                }
                val n = minOf(left, (silence.size / 2).toLong()).toInt()
                write(silence.copyOf(n * 2), nextFrameUtcMs - left * 1000 / SAMPLE_RATE)
                runSamples += n
                left -= n
            }
        }

        private fun write(frame: ByteArray, capturedUtcMs: Long) {
            try {
                if (out == null) openSegment(capturedUtcMs)
                feed(frame)
                segmentSamples += frame.size / 2
                if (off == Off.ENCODER) {
                    off = null
                    encoderFailures = 0
                    monitor.setFailure(generation, null)
                }
                if (segmentSamples >= SEGMENT_SAMPLES) {
                    when (closeSegment()) {
                        Closed.OK -> encoderFailures = 0
                        Closed.IO -> throw IOException("segment was not saved to storage")
                        // The finished file lost its tail (reported in its last-file check); the next
                        // frame opens a fresh encoder, so keep recording unless this keeps happening.
                        Closed.ENCODER -> {
                            encoderFailures += 1
                            if (encoderFailures >= ENCODER_MAX_FAILURES) {
                                throw IllegalStateException("encoder keeps failing to finish segments")
                            }
                        }
                    }
                }
            } catch (e: IOException) {
                Log.w(TAG, "local write failed", e)
                closeSegment()
                off = Off.IO
                monitor.setFailure(generation, "Local backup OFF: cannot write to phone storage")
            } catch (e: Exception) {
                Log.e(TAG, "local encoder failed", e)
                closeSegment()
                encoderFailures += 1
                if (encoderFailures >= ENCODER_MAX_FAILURES) {
                    off = Off.ENCODER_FATAL
                    monitor.setFailure(generation, "Local backup OFF: audio encoder keeps failing")
                } else {
                    off = Off.ENCODER
                    retryAtMs = SystemClock.elapsedRealtime() + ENCODER_RETRY_MS
                    monitor.setFailure(generation, "Local backup paused: audio encoder failed, retrying")
                }
            }
        }

        private fun openSegment(capturedUtcMs: Long) {
            val d = dir(context)
            val utc = capturedUtcMs  // when this audio was captured, not when it was encoded
            val stamp = SimpleDateFormat("yyyyMMdd'T'HHmmss", Locale.US)
                .apply { timeZone = TimeZone.getTimeZone("UTC") }.format(Date(utc))
            val base = "seg_${stamp}_${runUuid}_s$runSamples"
            openBases.add(base)
            currentBase = base
            SegmentSync.writeDest(File(d, "$base.dest"), dest)
            FileOutputStream(File(d, "$base.json")).use { side ->
                side.write(
                    JSONObject()
                        .put("v", 1)
                        .put("run_uuid", runUuid)
                        .put("utc_start_ms", utc)
                        .put("elapsed_start_ms", SystemClock.elapsedRealtime())
                        .put("start_sample", runSamples)
                        .put("sample_rate", SAMPLE_RATE)
                        .put("bit_rate", BIT_RATE)
                        .put("format", "aac-lc adts")
                        .toString()
                        .toByteArray(),
                )
                side.fd.sync()
            }
            out = FileOutputStream(File(d, "$base.aac"))
            val format = MediaFormat.createAudioFormat(MIME, SAMPLE_RATE, 1).apply {
                setInteger(MediaFormat.KEY_AAC_PROFILE, MediaCodecInfo.CodecProfileLevel.AACObjectLC)
                setInteger(MediaFormat.KEY_BIT_RATE, BIT_RATE)
                setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, 16 * 1024)
            }
            // A missing encoder is an encoder failure, never a storage failure (IOException here).
            val c = try {
                MediaCodec.createEncoderByType(MIME)
            } catch (e: IOException) {
                throw IllegalStateException("no AAC encoder", e)
            }
            try {
                c.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
                c.start()
            } catch (e: Exception) {
                c.release()  // allocated but never usable: release it before the retry path
                throw e
            }
            codec = c
            segmentSamples = 0
        }

        private fun feed(frame: ByteArray) {
            val c = codec ?: return
            val deadline = SystemClock.elapsedRealtime() + FEED_DEADLINE_MS
            var offset = 0
            while (offset < frame.size) {
                val idx = c.dequeueInputBuffer(10_000)
                if (idx < 0) {
                    drain(endOfStream = false)
                    if (SystemClock.elapsedRealtime() > deadline) {
                        throw IllegalStateException("encoder input stalled")
                    }
                    continue
                }
                val input = c.getInputBuffer(idx) ?: throw IllegalStateException("no encoder input buffer")
                val chunk = minOf(input.capacity(), frame.size - offset)
                input.clear()
                input.put(frame, offset, chunk)
                val ptsUs = (segmentSamples + offset / 2) * 1_000_000L / SAMPLE_RATE
                c.queueInputBuffer(idx, 0, chunk, ptsUs, 0)
                offset += chunk
            }
            drain(endOfStream = false)
            maybeSync()
        }

        private fun drain(endOfStream: Boolean) {
            val c = codec ?: return
            val o = out ?: return
            var waits = 0
            while (true) {
                val idx = c.dequeueOutputBuffer(info, if (endOfStream) 10_000 else 0)
                if (idx == MediaCodec.INFO_TRY_AGAIN_LATER) {
                    if (!endOfStream) return
                    // Bounded: an encoder that never signals end-of-stream must not hang the writer,
                    // and audio it still holds is lost, so that is a failure, not a finished file.
                    if (++waits < 50) continue
                    throw IllegalStateException("encoder did not signal end of stream")
                }
                if (idx < 0) continue  // format / buffers changed
                val buffer = c.getOutputBuffer(idx)
                val isConfig = info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0
                if (buffer != null && info.size > 0 && !isConfig) {
                    val payload = ByteArray(info.size)
                    buffer.position(info.offset)
                    buffer.get(payload)
                    o.write(Adts.header(payload.size))
                    o.write(payload)
                    bytesWritten += payload.size + Adts.HEADER_BYTES
                    monitor.onBytesWritten(generation, bytesWritten, SystemClock.elapsedRealtime())
                }
                c.releaseOutputBuffer(idx, false)
                if (info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) return
            }
        }

        private fun maybeSync() {
            val now = SystemClock.elapsedRealtime()
            if (now - lastSync < SYNC_MS) return
            lastSync = now
            // A failed sync means written audio may not be on storage: it throws into write()'s
            // IOException path, which turns the backup off and alerts.
            out?.fd?.sync()
        }

        private fun closeSegment(): Closed {
            var result = Closed.OK
            val c = codec
            if (c != null) {
                try {
                    var idx = -1
                    for (attempt in 0 until 5) {
                        idx = c.dequeueInputBuffer(10_000)
                        if (idx >= 0) break
                        drain(endOfStream = false)
                    }
                    if (idx < 0) throw IllegalStateException("encoder took no end-of-stream input")
                    c.queueInputBuffer(idx, 0, 0, 0, MediaCodec.BUFFER_FLAG_END_OF_STREAM)
                    drain(endOfStream = true)
                } catch (e: IOException) {
                    Log.w(TAG, "write during encoder drain on close failed", e)
                    result = Closed.IO
                } catch (e: Exception) {
                    Log.w(TAG, "encoder drain on close failed", e)
                    result = Closed.ENCODER
                }
                try { c.stop() } catch (_: Exception) {}
                try { c.release() } catch (_: Exception) {}
            }
            codec = null
            try {
                out?.flush()
                out?.fd?.sync()
            } catch (e: Exception) {
                Log.w(TAG, "final sync of segment failed", e)
                result = Closed.IO
            }
            try { out?.close() } catch (_: Exception) {}
            out = null
            segmentSamples = 0
            val base = currentBase ?: return result
            currentBase = null
            val d = dir(context)
            val aac = File(d, "$base.aac")
            when {
                !aac.exists() -> monitor.setLastCheck(generation, "last file is missing from storage")
                result == Closed.IO -> monitor.setLastCheck(generation, "last file may not be fully saved: storage error")
                result == Closed.ENCODER -> monitor.setLastCheck(generation, "last file is missing its final audio: encoder did not finish")
                aac.length() == 0L -> {
                    // A segment that never received a frame is noise in the folder; drop it with its sidecar.
                    aac.delete()
                    SegmentSync.deleteCompanions(d, base)
                }
                else -> {
                    verify(aac)
                    openBases.remove(base)
                    // A closed file is ready to mirror to the edge. Scheduling that must never be
                    // mistaken for an encoder failure: the recording itself is already safe.
                    try { onSegmentClosed() } catch (e: Exception) { Log.w(TAG, "sync kick failed", e) }
                }
            }
            openBases.remove(base)
            return result
        }

        /**
         * Re-reads a closed segment and counts its valid ADTS frames, so the status shows evidence
         * that well-formed audio is on disk rather than an assumption that the encoder worked.
         */
        private fun verify(aac: File) {
            try {
                val frames = Adts.frameOffsets(aac.readBytes()).size
                val secs = frames.toLong() * Adts.SAMPLES_PER_FRAME / SAMPLE_RATE
                monitor.setLastCheck(
                    generation,
                    if (frames > 0) "last file verified: %d:%02d".format(secs / 60, secs % 60)
                    else "last file has no valid audio",
                )
            } catch (e: Throwable) {
                monitor.setLastCheck(generation, "last file could not be read back")
            }
        }

        private fun checkStorage(force: Boolean) {
            val now = SystemClock.elapsedRealtime()
            if (!force && now - lastStorageCheck < STORAGE_CHECK_MS) return
            lastStorageCheck = now
            val d = dir(context)
            var free = allocatableBytes(context, d)
            if (free < FLOOR_BYTES) {
                // Free what the retention rule allows under pressure, then re-measure.
                try { purge(context) } catch (_: Exception) {}
                free = allocatableBytes(context, d)
            }
            when {
                off == Off.ENCODER || off == Off.ENCODER_FATAL -> Unit  // keep the encoder's message
                free < FLOOR_BYTES -> {
                    if (off == null) closeSegment()
                    off = Off.STORAGE
                    monitor.setFailure(generation, "Local backup OFF: phone storage is full — the live stream continues")
                }
                (off == Off.STORAGE && free >= RESUME_BYTES) || (off == Off.IO && free >= FLOOR_BYTES) -> {
                    off = null
                    monitor.setFailure(generation, null)
                }
            }
        }
    }
}
