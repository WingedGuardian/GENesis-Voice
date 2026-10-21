package com.genesis.meetingmic

import android.content.Context
import android.content.Intent
import android.net.ConnectivityManager
import android.net.NetworkCapabilities

/**
 * The bridge is reachable only over the tailnet, so a stopped Tailscale is the most common reason audio
 * never arrives. Tailscale's Android app exports a receiver for exactly this:
 * `com.tailscale.ipn/.IPNReceiver` handles `com.tailscale.ipn.CONNECT_VPN` with no permission or extras,
 * and starts the VPN when it is logged in and already has VPN consent (otherwise Tailscale posts its own
 * "open Tailscale" notification). Package visibility is declared in the manifest `<queries>`.
 */
class TailscaleLink(private val context: Context) {

    /**
     * True only when a VPN network carrying a Tailscale address is up. Any VPN shows as
     * TRANSPORT_VPN, so a different VPN must not count: Tailscale assigns addresses from its own
     * ranges (the RFC 6598 shared-address /10 and its own IPv6 /48), seen in the link properties.
     */
    @Suppress("DEPRECATION")  // allNetworks: the VPN need not be the default network
    fun isTailnetUp(): Boolean {
        val cm = context.getSystemService(ConnectivityManager::class.java) ?: return false
        return cm.allNetworks.any { n ->
            val caps = cm.getNetworkCapabilities(n)
            caps != null && caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN) &&
                cm.getLinkProperties(n)?.linkAddresses.orEmpty()
                    .any { isTailscaleAddress(it.address.address) }
        }
    }

    fun isInstalled(): Boolean = launchIntent() != null

    fun requestConnect() {
        if (!isInstalled()) return
        context.sendBroadcast(
            Intent(ACTION_CONNECT).setClassName(PACKAGE, RECEIVER),
        )
    }

    fun launchIntent(): Intent? = context.packageManager.getLaunchIntentForPackage(PACKAGE)

    companion object {
        /** The RFC 6598 shared-address /10 (IPv4) or Tailscale's documented IPv6 /48 (checked bytewise). */
        fun isTailscaleAddress(raw: ByteArray): Boolean = when (raw.size) {
            4 -> (raw[0].toInt() and 0xFF) == 100 && (raw[1].toInt() and 0xC0) == 0x40
            16 -> raw[0] == 0xfd.toByte() && raw[1] == 0x7a.toByte() && raw[2] == 0x11.toByte() &&
                raw[3] == 0x5c.toByte() && raw[4] == 0xa1.toByte() && raw[5] == 0xe0.toByte()
            else -> false
        }

        const val PACKAGE = "com.tailscale.ipn"
        private const val RECEIVER = "com.tailscale.ipn.IPNReceiver"
        private const val ACTION_CONNECT = "com.tailscale.ipn.CONNECT_VPN"
    }
}
