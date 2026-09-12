package com.genesis.meetingmic

import org.junit.Assert.assertEquals
import org.junit.Test

class DeliveryReceiptTrackerTest {
    @Test fun `new bridge becomes healthy only after advancing receipt`() {
        val tracker = DeliveryReceiptTracker(timeoutMs = 10_000)
        tracker.onSocketOpened(1_000)
        tracker.onCapabilityConfirmed()
        assertEquals(DeliveryReceiptStatus.WAITING, tracker.status(2_000))
        tracker.onAck(3_200, 2_100)
        assertEquals(DeliveryReceiptStatus.HEALTHY, tracker.status(9_000))
        assertEquals(3_200, tracker.confirmedBytes)
    }

    @Test fun `advertised ack that stops becomes stale`() {
        val tracker = DeliveryReceiptTracker(timeoutMs = 10_000)
        tracker.onSocketOpened(1_000)
        tracker.onCapabilityConfirmed()
        tracker.onAck(3_200, 2_000)
        assertEquals(DeliveryReceiptStatus.STALE, tracker.status(12_001))
    }

    @Test fun `old bridge is unconfirmed without being called healthy`() {
        val tracker = DeliveryReceiptTracker(timeoutMs = 10_000)
        tracker.onSocketOpened(1_000)
        assertEquals(DeliveryReceiptStatus.WAITING, tracker.status(10_999))
        assertEquals(DeliveryReceiptStatus.UNCONFIRMED, tracker.status(11_001))
    }

    @Test fun `stale and duplicate receipts do not move confirmed progress backward`() {
        val tracker = DeliveryReceiptTracker(timeoutMs = 10_000)
        tracker.onSocketOpened(1_000)
        tracker.onCapabilityConfirmed()
        tracker.onAck(6_400, 2_000)
        tracker.onAck(3_200, 3_000)
        assertEquals(6_400, tracker.confirmedBytes)
        assertEquals(DeliveryReceiptStatus.HEALTHY, tracker.status(11_999))
    }

    @Test fun `stopped run cannot apply a delayed callback`() {
        val generations = CaptureRunGeneration()
        val stopped = generations.begin()
        generations.invalidate()
        assertEquals(false, generations.isCurrent(stopped))
    }

    @Test fun `restarted run rejects callbacks from the prior run`() {
        val generations = CaptureRunGeneration()
        val old = generations.begin()
        val replacement = generations.begin()
        assertEquals(false, generations.isCurrent(old))
        assertEquals(true, generations.isCurrent(replacement))
    }
}
