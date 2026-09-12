package com.genesis.meetingmic

enum class CaptureDeliveryPhase {
    IDLE,
    CONNECTING,
    AWAITING_RECEIPT,
    HEALTHY,
    UNCONFIRMED,
    RECONNECTING,
    CAPTURE_STOPPED,
    STOPPED,
}

enum class FailureAlertCommand {
    NONE,
    CLEAR,
    SHOW_RECONNECTING,
    SHOW_CAPTURE_STOPPED,
}

data class CaptureDeliverySnapshot(
    val runId: Long,
    val attemptId: Long,
    val phase: CaptureDeliveryPhase,
    val confirmedBytes: Long,
    val hadDeliveryGap: Boolean,
)

data class CaptureDeliveryUpdate(
    val snapshot: CaptureDeliverySnapshot,
    val alert: FailureAlertCommand = FailureAlertCommand.NONE,
    val closeSocket: Boolean = false,
)

/**
 * Pure owner of capture-run and socket-attempt transitions.
 *
 * Android callbacks carry both ids, so an old callback or deadline cannot mutate a newer run or
 * socket. The service serializes events on its main handler and applies the returned side effects.
 */
class CaptureDeliveryController(receiptTimeoutMs: Long) {
    private val receipts = DeliveryReceiptTracker(receiptTimeoutMs)
    private var runId = 0L
    private var attemptId = 0L
    private var active = false
    private var phase = CaptureDeliveryPhase.IDLE
    private var alert = FailureAlertCommand.NONE
    private var hadDeliveryGap = false

    @Synchronized
    fun start(): CaptureDeliveryUpdate {
        runId += 1
        attemptId = 0
        receipts.reset()
        active = true
        phase = CaptureDeliveryPhase.CONNECTING
        alert = FailureAlertCommand.NONE
        hadDeliveryGap = false
        return update(alert = FailureAlertCommand.CLEAR)
    }

    @Synchronized
    fun beginAttempt(forRunId: Long): Long? {
        if (!isCurrentRun(forRunId) || phase !in arrayOf(
                CaptureDeliveryPhase.CONNECTING,
                CaptureDeliveryPhase.RECONNECTING,
            )
        ) return null
        attemptId += 1
        phase = CaptureDeliveryPhase.CONNECTING
        return attemptId
    }

    @Synchronized
    fun socketOpened(forRunId: Long, forAttemptId: Long, nowMs: Long): CaptureDeliveryUpdate? {
        if (!isCurrentAttempt(forRunId, forAttemptId) || phase != CaptureDeliveryPhase.CONNECTING) {
            return null
        }
        receipts.onSocketOpened(nowMs)
        phase = CaptureDeliveryPhase.AWAITING_RECEIPT
        return update()
    }

    @Synchronized
    fun capabilityConfirmed(forRunId: Long, forAttemptId: Long): Boolean {
        if (!isCurrentOpenAttempt(forRunId, forAttemptId)) return false
        receipts.onCapabilityConfirmed()
        return true
    }

    @Synchronized
    fun ack(
        forRunId: Long,
        forAttemptId: Long,
        bytes: Long,
        nowMs: Long,
    ): CaptureDeliveryUpdate? {
        if (!isCurrentOpenAttempt(forRunId, forAttemptId)) return null
        if (!receipts.onAck(bytes, nowMs)) return null
        phase = CaptureDeliveryPhase.HEALTHY
        return deliveryRestoredUpdate()
    }

    @Synchronized
    fun receiptTick(forRunId: Long, forAttemptId: Long, nowMs: Long): CaptureDeliveryUpdate? {
        if (!isCurrentOpenAttempt(forRunId, forAttemptId)) return null
        return when (receipts.status(nowMs)) {
            DeliveryReceiptStatus.WAITING -> null
            DeliveryReceiptStatus.HEALTHY -> {
                phase = CaptureDeliveryPhase.HEALTHY
                null
            }
            DeliveryReceiptStatus.UNCONFIRMED -> {
                if (phase == CaptureDeliveryPhase.UNCONFIRMED) return null
                phase = CaptureDeliveryPhase.UNCONFIRMED
                deliveryRestoredUpdate()
            }
            DeliveryReceiptStatus.STALE -> reconnect(closeSocket = true)
        }
    }

    @Synchronized
    fun socketDown(forRunId: Long, forAttemptId: Long): CaptureDeliveryUpdate? {
        if (!isCurrentAttempt(forRunId, forAttemptId) || phase !in arrayOf(
                CaptureDeliveryPhase.CONNECTING,
                CaptureDeliveryPhase.AWAITING_RECEIPT,
                CaptureDeliveryPhase.HEALTHY,
                CaptureDeliveryPhase.UNCONFIRMED,
            )
        ) return null
        return reconnect(closeSocket = false)
    }

    @Synchronized
    fun handshakeTimedOut(forRunId: Long, forAttemptId: Long): CaptureDeliveryUpdate? {
        if (!isCurrentAttempt(forRunId, forAttemptId) || phase != CaptureDeliveryPhase.CONNECTING) {
            return null
        }
        return reconnect(closeSocket = true)
    }

    @Synchronized
    fun captureFailed(forRunId: Long): CaptureDeliveryUpdate? {
        if (!isCurrentRun(forRunId)) return null
        active = false
        phase = CaptureDeliveryPhase.CAPTURE_STOPPED
        alert = FailureAlertCommand.SHOW_CAPTURE_STOPPED
        hadDeliveryGap = true
        return update(alert = FailureAlertCommand.SHOW_CAPTURE_STOPPED, closeSocket = true)
    }

    @Synchronized
    fun stop(): CaptureDeliveryUpdate {
        runId += 1
        attemptId = 0
        active = false
        phase = CaptureDeliveryPhase.STOPPED
        alert = FailureAlertCommand.NONE
        return update(alert = FailureAlertCommand.CLEAR, closeSocket = true)
    }

    @Synchronized
    fun canSend(forRunId: Long): Boolean = isCurrentRun(forRunId) && phase in arrayOf(
        CaptureDeliveryPhase.AWAITING_RECEIPT,
        CaptureDeliveryPhase.HEALTHY,
        CaptureDeliveryPhase.UNCONFIRMED,
    )

    @Synchronized
    fun needsReconnect(forRunId: Long): Boolean =
        isCurrentRun(forRunId) && phase == CaptureDeliveryPhase.RECONNECTING

    @Synchronized
    fun currentAttempt(forRunId: Long): Long? =
        if (isCurrentRun(forRunId)) attemptId else null

    @Synchronized
    fun receiptChecksActive(forRunId: Long, forAttemptId: Long): Boolean =
        isCurrentOpenAttempt(forRunId, forAttemptId)

    @Synchronized
    fun snapshot(): CaptureDeliverySnapshot = snapshotUnlocked()

    @Synchronized
    fun markerNotification(): String = when (phase) {
        CaptureDeliveryPhase.UNCONFIRMED -> "Marked ✓ — delivery unconfirmed"
        CaptureDeliveryPhase.AWAITING_RECEIPT -> "Marked ✓ — confirming audio delivery"
        else -> "Marked ✓"
    }

    private fun reconnect(closeSocket: Boolean): CaptureDeliveryUpdate {
        phase = CaptureDeliveryPhase.RECONNECTING
        hadDeliveryGap = true
        val alertCommand = if (alert == FailureAlertCommand.SHOW_RECONNECTING) {
            FailureAlertCommand.NONE
        } else {
            FailureAlertCommand.SHOW_RECONNECTING
        }
        alert = FailureAlertCommand.SHOW_RECONNECTING
        return update(alert = alertCommand, closeSocket = closeSocket)
    }

    private fun deliveryRestoredUpdate(): CaptureDeliveryUpdate {
        val alertCommand = if (alert == FailureAlertCommand.NONE) {
            FailureAlertCommand.NONE
        } else {
            FailureAlertCommand.CLEAR
        }
        alert = FailureAlertCommand.NONE
        return update(alert = alertCommand)
    }

    private fun isCurrentRun(forRunId: Long): Boolean = active && runId == forRunId

    private fun isCurrentAttempt(forRunId: Long, forAttemptId: Long): Boolean =
        isCurrentRun(forRunId) && attemptId == forAttemptId

    private fun isCurrentOpenAttempt(forRunId: Long, forAttemptId: Long): Boolean =
        isCurrentAttempt(forRunId, forAttemptId) && phase in arrayOf(
            CaptureDeliveryPhase.AWAITING_RECEIPT,
            CaptureDeliveryPhase.HEALTHY,
            CaptureDeliveryPhase.UNCONFIRMED,
        )

    private fun update(
        alert: FailureAlertCommand = FailureAlertCommand.NONE,
        closeSocket: Boolean = false,
    ) = CaptureDeliveryUpdate(snapshotUnlocked(), alert, closeSocket)

    private fun snapshotUnlocked() = CaptureDeliverySnapshot(
        runId = runId,
        attemptId = attemptId,
        phase = phase,
        confirmedBytes = receipts.confirmedBytes,
        hadDeliveryGap = hadDeliveryGap,
    )
}
