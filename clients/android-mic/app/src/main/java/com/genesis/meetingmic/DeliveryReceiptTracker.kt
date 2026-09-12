package com.genesis.meetingmic

import java.util.concurrent.atomic.AtomicLong

enum class DeliveryReceiptStatus { WAITING, HEALTHY, UNCONFIRMED, STALE }

/** Pure receipt-progress state. Socket-open and local enqueue are deliberately not success. */
class DeliveryReceiptTracker(private val timeoutMs: Long) {
    var confirmedBytes: Long = 0
        private set
    private var openedAtMs: Long = 0
    private var lastAckAtMs: Long = 0
    private var ackSupported = false

    fun onSocketOpened(nowMs: Long) {
        openedAtMs = nowMs
        lastAckAtMs = 0
        confirmedBytes = 0
        ackSupported = false
    }

    fun onCapabilityConfirmed() { ackSupported = true }

    fun onAck(bytes: Long, nowMs: Long): Boolean {
        if (!ackSupported || bytes <= confirmedBytes) return false
        confirmedBytes = bytes
        lastAckAtMs = nowMs
        return true
    }

    fun status(nowMs: Long): DeliveryReceiptStatus = when {
        ackSupported && lastAckAtMs > 0 && nowMs - lastAckAtMs <= timeoutMs ->
            DeliveryReceiptStatus.HEALTHY
        ackSupported && nowMs - maxOf(openedAtMs, lastAckAtMs) > timeoutMs ->
            DeliveryReceiptStatus.STALE
        !ackSupported && nowMs - openedAtMs > timeoutMs ->
            DeliveryReceiptStatus.UNCONFIRMED
        else -> DeliveryReceiptStatus.WAITING
    }
}

/** Identifies one capture run so delayed callbacks cannot mutate a later run. */
class CaptureRunGeneration {
    private val value = AtomicLong(0)
    fun begin(): Long = value.incrementAndGet()
    fun invalidate() { value.incrementAndGet() }
    fun isCurrent(token: Long): Boolean = value.get() == token
}
