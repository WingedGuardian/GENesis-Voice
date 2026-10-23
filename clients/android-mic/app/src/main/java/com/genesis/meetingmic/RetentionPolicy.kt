package com.genesis.meetingmic

/**
 * Which local recordings to delete (owner rule):
 * - anything older than [keepDays] (90) goes;
 * - when storage is low, anything older than [lowStorageKeepDays] (45) also goes;
 * - nothing newer than [lowStorageKeepDays] is ever deleted, however full the phone is.
 *
 * Nothing is pinned: a later stage that finds its audio purged reports it as lost, visibly. Pure, so
 * the rule is unit-tested.
 */
object RetentionPolicy {
    const val DAY_MS = 24L * 60 * 60 * 1000

    data class Segment(val id: String, val startUtcMs: Long)

    fun toDelete(
        segments: List<Segment>,
        nowUtcMs: Long,
        lowStorage: Boolean,
        keepDays: Int = 90,
        lowStorageKeepDays: Int = 45,
    ): List<Segment> {
        val cutoff = nowUtcMs - (if (lowStorage) lowStorageKeepDays else keepDays) * DAY_MS
        return segments.filter { it.startUtcMs < cutoff }.sortedBy { it.startUtcMs }
    }
}
