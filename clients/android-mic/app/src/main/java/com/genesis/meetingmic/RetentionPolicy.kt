package com.genesis.meetingmic

/**
 * Which local recordings to delete (owner rule):
 * - every recording is kept [keepDays] (90), even after the edge confirms it holds a copy;
 * - anything older than that goes;
 * - under storage pressure, copies the edge confirmed holding may go early, only until there is room
 *   again (re-confirmed a week later first, then the rest; oldest first within each);
 * - an unsynced recording is the only copy and is never deleted early, however full the phone is:
 *   the local backup stops instead (see [LocalRecorder]).
 *
 * Nothing is pinned: a later stage that finds its audio purged reports it as lost, visibly. Pure, so
 * the rule is unit-tested.
 */
object RetentionPolicy {
    const val DAY_MS = 24L * 60 * 60 * 1000

    /**
     * [synced]: the edge returned a receipt proving it stored these exact bytes. [checked]: it said
     * so again a week later.
     */
    data class Segment(
        val id: String,
        val startUtcMs: Long,
        val synced: Boolean = false,
        val checked: Boolean = false,
    )

    /** Past the retention period: always deleted, oldest first. */
    fun expired(segments: List<Segment>, nowUtcMs: Long, keepDays: Int = 90): List<Segment> =
        segments.filter { it.startUtcMs < nowUtcMs - keepDays * DAY_MS }.sortedBy { it.startUtcMs }

    /** What may go early under storage pressure, in the order it goes: only copies the edge holds. */
    fun underPressure(segments: List<Segment>, nowUtcMs: Long, keepDays: Int = 90): List<Segment> =
        segments.filter { it.synced && it.startUtcMs >= nowUtcMs - keepDays * DAY_MS }
            .sortedWith(compareBy({ !it.checked }, { it.startUtcMs }))

    /**
     * Removes everything expired, then the early candidates one at a time while [hasRoom] says no,
     * asking again after each removal.
     */
    fun sweep(segments: List<Segment>, nowUtcMs: Long, hasRoom: () -> Boolean, remove: (Segment) -> Unit) {
        expired(segments, nowUtcMs).forEach(remove)
        for (s in underPressure(segments, nowUtcMs)) {
            if (hasRoom()) break
            remove(s)
        }
    }
}
