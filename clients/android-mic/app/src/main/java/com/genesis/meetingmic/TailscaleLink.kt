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

    fun isVpnUp(): Boolean {
        val cm = context.getSystemService(ConnectivityManager::class.java) ?: return false
        val caps = cm.getNetworkCapabilities(cm.activeNetwork) ?: return false
        return caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)
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
        const val PACKAGE = "com.tailscale.ipn"
        private const val RECEIVER = "com.tailscale.ipn.IPNReceiver"
        private const val ACTION_CONNECT = "com.tailscale.ipn.CONNECT_VPN"
    }
}
