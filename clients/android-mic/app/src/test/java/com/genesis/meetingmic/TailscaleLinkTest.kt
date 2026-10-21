package com.genesis.meetingmic

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** Boundary vectors are built from bytes: the ranges are public definitions, not real addresses. */
class TailscaleLinkTest {
    private fun v4(a: Int, b: Int, c: Int, d: Int) = byteArrayOf(a.toByte(), b.toByte(), c.toByte(), d.toByte())

    private fun v6(vararg head: Int) = ByteArray(16).also { out ->
        head.forEachIndexed { i, x -> out[i] = x.toByte() }
        out[15] = 1
    }

    @Test
    fun tailscaleRangesAreRecognized() {
        assertTrue(TailscaleLink.isTailscaleAddress(v4(100, 64, 0, 1)))      // first of the /10
        assertTrue(TailscaleLink.isTailscaleAddress(v4(100, 127, 255, 254))) // last of the /10
        assertTrue(TailscaleLink.isTailscaleAddress(v6(0xfd, 0x7a, 0x11, 0x5c, 0xa1, 0xe0)))
    }

    @Test
    fun otherVpnAndLanRangesAreNot() {
        assertFalse(TailscaleLink.isTailscaleAddress(v4(100, 63, 255, 255)))  // just below the range
        assertFalse(TailscaleLink.isTailscaleAddress(v4(100, 128, 0, 1)))     // just above it
        assertFalse(TailscaleLink.isTailscaleAddress(v4(198, 51, 100, 20)))   // documentation range
        assertFalse(TailscaleLink.isTailscaleAddress(v6(0x20, 0x01, 0x0d, 0xb8)))
        assertFalse(TailscaleLink.isTailscaleAddress(v6(0xfd, 0x7a, 0x11, 0x5c, 0xa1, 0xe1)))  // off by one
    }
}
