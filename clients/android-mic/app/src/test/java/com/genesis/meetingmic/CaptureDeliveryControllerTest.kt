package com.genesis.meetingmic

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class CaptureDeliveryControllerTest {
    private val controller = CaptureDeliveryController(receiptTimeoutMs = 10_000)

    @Test fun `receipt advances a new run from connecting to healthy`() {
        val run = controller.start().snapshot.runId
        val attempt = controller.beginAttempt(run)!!
        assertEquals(CaptureDeliveryPhase.AWAITING_RECEIPT, controller.socketOpened(run, attempt, 1_000)!!.snapshot.phase)
        assertTrue(controller.capabilityConfirmed(run, attempt))
        val healthy = controller.ack(run, attempt, 3_200, 2_000)!!
        assertEquals(CaptureDeliveryPhase.HEALTHY, healthy.snapshot.phase)
        assertEquals(3_200, healthy.snapshot.confirmedBytes)
        assertTrue(controller.canSend(run))
    }

    @Test fun `receipt deadline expires without any further audio frames`() {
        val (run, attempt) = healthyConnection()
        assertTrue(controller.receiptChecksActive(run, attempt))
        val stale = controller.receiptTick(run, attempt, 12_001)!!
        assertEquals(CaptureDeliveryPhase.RECONNECTING, stale.snapshot.phase)
        assertEquals(FailureAlertCommand.SHOW_RECONNECTING, stale.alert)
        assertTrue(stale.closeSocket)
        assertTrue(stale.snapshot.hadDeliveryGap)
        assertFalse(controller.receiptChecksActive(run, attempt))
        assertTrue(controller.needsReconnect(run))
        assertNull(controller.socketDown(run, attempt))
    }

    @Test fun `advancing receipt after reconnect clears alert and records earlier gap`() {
        val (run, oldAttempt) = healthyConnection()
        controller.socketDown(run, oldAttempt)
        val attempt = controller.beginAttempt(run)!!
        controller.socketOpened(run, attempt, 20_000)
        controller.capabilityConfirmed(run, attempt)
        val restored = controller.ack(run, attempt, 3_200, 21_000)!!
        assertEquals(FailureAlertCommand.CLEAR, restored.alert)
        assertEquals(CaptureDeliveryPhase.HEALTHY, restored.snapshot.phase)
        assertTrue(restored.snapshot.hadDeliveryGap)
    }

    @Test fun `legacy connection becomes unconfirmed and clears prior outage alert`() {
        val run = controller.start().snapshot.runId
        val first = controller.beginAttempt(run)!!
        controller.socketDown(run, first)
        val legacy = controller.beginAttempt(run)!!
        controller.socketOpened(run, legacy, 20_000)
        val unconfirmed = controller.receiptTick(run, legacy, 30_001)!!
        assertEquals(CaptureDeliveryPhase.UNCONFIRMED, unconfirmed.snapshot.phase)
        assertEquals(FailureAlertCommand.CLEAR, unconfirmed.alert)
        assertEquals("Marked ✓ — delivery unconfirmed", controller.markerNotification())
    }

    @Test fun `repeated failure callbacks do not repeat the outage alert`() {
        val run = controller.start().snapshot.runId
        val attempt = controller.beginAttempt(run)!!
        val first = controller.socketDown(run, attempt)!!
        assertEquals(FailureAlertCommand.SHOW_RECONNECTING, first.alert)
        assertNull(controller.socketDown(run, attempt))
    }

    @Test fun `fatal capture failure supersedes reconnect and restart resets alert`() {
        val run = controller.start().snapshot.runId
        val attempt = controller.beginAttempt(run)!!
        controller.socketDown(run, attempt)
        val fatal = controller.captureFailed(run)!!
        assertEquals(CaptureDeliveryPhase.CAPTURE_STOPPED, fatal.snapshot.phase)
        assertEquals(FailureAlertCommand.SHOW_CAPTURE_STOPPED, fatal.alert)
        assertTrue(fatal.closeSocket)

        val restarted = controller.start()
        assertEquals(FailureAlertCommand.CLEAR, restarted.alert)
        val nextAttempt = controller.beginAttempt(restarted.snapshot.runId)!!
        assertEquals(
            FailureAlertCommand.SHOW_RECONNECTING,
            controller.socketDown(restarted.snapshot.runId, nextAttempt)!!.alert,
        )
    }

    @Test fun `successful open wins against its queued handshake timeout`() {
        val run = controller.start().snapshot.runId
        val attempt = controller.beginAttempt(run)!!
        assertTrue(controller.socketOpened(run, attempt, 1_000) != null)
        assertNull(controller.handshakeTimedOut(run, attempt))
        assertEquals(CaptureDeliveryPhase.AWAITING_RECEIPT, controller.snapshot().phase)
    }

    @Test fun `handshake timeout wins against its queued open callback`() {
        val run = controller.start().snapshot.runId
        val attempt = controller.beginAttempt(run)!!
        assertEquals(CaptureDeliveryPhase.RECONNECTING, controller.handshakeTimedOut(run, attempt)!!.snapshot.phase)
        assertNull(controller.socketOpened(run, attempt, 1_000))
    }

    @Test fun `new attempt rejects every callback from the prior attempt`() {
        val run = controller.start().snapshot.runId
        val oldAttempt = controller.beginAttempt(run)!!
        controller.socketDown(run, oldAttempt)
        val currentAttempt = controller.beginAttempt(run)!!
        assertNull(controller.socketOpened(run, oldAttempt, 1_000))
        assertFalse(controller.capabilityConfirmed(run, oldAttempt))
        assertNull(controller.ack(run, oldAttempt, 3_200, 2_000))
        assertNull(controller.receiptTick(run, oldAttempt, 20_000))
        assertNull(controller.handshakeTimedOut(run, oldAttempt))
        assertEquals(currentAttempt, controller.currentAttempt(run))
    }

    @Test fun `stop and restart reject every callback from the prior run`() {
        val oldRun = controller.start().snapshot.runId
        val oldAttempt = controller.beginAttempt(oldRun)!!
        controller.stop()
        val newRun = controller.start().snapshot.runId
        assertNull(controller.socketOpened(oldRun, oldAttempt, 1_000))
        assertNull(controller.socketDown(oldRun, oldAttempt))
        assertNull(controller.captureFailed(oldRun))
        assertFalse(controller.canSend(oldRun))
        assertEquals(newRun, controller.snapshot().runId)
    }

    @Test fun `new run clears confirmed byte count`() {
        val (run, _) = healthyConnection()
        assertEquals(3_200, controller.snapshot().confirmedBytes)
        assertTrue(controller.stop().snapshot.runId > run)
        assertEquals(0, controller.start().snapshot.confirmedBytes)
    }

    private fun healthyConnection(): Pair<Long, Long> {
        val run = controller.start().snapshot.runId
        val attempt = controller.beginAttempt(run)!!
        controller.socketOpened(run, attempt, 1_000)
        controller.capabilityConfirmed(run, attempt)
        controller.ack(run, attempt, 3_200, 2_000)
        return run to attempt
    }
}
