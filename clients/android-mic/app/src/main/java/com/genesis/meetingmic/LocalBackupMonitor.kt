package com.genesis.meetingmic

data class LocalReading(
    val signal: MicSignal,
    val reason: String,
    val bytesWritten: Long = 0,
    /** Result of re-reading the last closed file (e.g. "last file verified: 5:00"); empty until one closes. */
    val lastCheck: String = "",
)

/**
 * Fail-closed judgement of the local backup: GREEN only while bytes written to disk have advanced
 * within [freshMs]. A writer that stops making progress (encoder stall, full disk, crash) turns grey,
 * then red, even if it never reports an error itself.
 *
 * Each [start] opens a new generation; writer callbacks carry the generation they belong to, and
 * callbacks from an older writer that is still draining are ignored, so they can neither fake
 * progress nor hide a stall in the current run.
 */
class LocalBackupMonitor(
    private val freshMs: Long = 10_000,
    private val redAfterMs: Long = 20_000,
) {
    private var generation = 0L
    private var startedAtMs = -1L
    private var lastProgressMs = -1L
    private var bytes = 0L
    private var failure: String? = null
    private var lastCheck = ""

    /** Starts a new generation and returns it; pass it to every writer callback. */
    @Synchronized
    fun start(nowMs: Long): Long {
        generation += 1
        startedAtMs = nowMs
        lastProgressMs = -1L
        bytes = 0L
        failure = null
        lastCheck = ""
        return generation
    }

    @Synchronized
    fun stop() {
        startedAtMs = -1L
    }

    @Synchronized
    fun onBytesWritten(gen: Long, total: Long, nowMs: Long) {
        if (gen != generation || startedAtMs < 0) return
        if (total > bytes) {
            bytes = total
            lastProgressMs = nowMs
        }
    }

    /** A known reason the backup is off (storage full, encoder failed); the writer clears it on resume. */
    @Synchronized
    fun setFailure(gen: Long, reason: String?) {
        if (gen == generation) failure = reason
    }

    @Synchronized
    fun setLastCheck(gen: Long, text: String) {
        if (gen == generation) lastCheck = text
    }

    @Synchronized
    fun read(nowMs: Long): LocalReading {
        if (startedAtMs < 0) return LocalReading(MicSignal.GREY, "off", bytes, lastCheck)
        failure?.let { return LocalReading(MicSignal.RED, it, bytes, lastCheck) }
        if (lastProgressMs >= 0 && nowMs - lastProgressMs <= freshMs) {
            return LocalReading(MicSignal.GREEN, "saving on this phone", bytes, lastCheck)
        }
        val since = nowMs - (if (lastProgressMs >= 0) lastProgressMs else startedAtMs)
        if (since >= redAfterMs) {
            return LocalReading(MicSignal.RED, "Local backup is not being written", bytes, lastCheck)
        }
        return LocalReading(MicSignal.GREY, "starting local backup", bytes, lastCheck)
    }
}
