package com.genesis.meetingmic

import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import androidx.core.app.NotificationCompat

/**
 * The loud, one-buzz-per-incident notifications for problems the user must act on: the mic hearing
 * nothing, Tailscale not starting, and the recorder having been closed by Android. They use the
 * high-importance delivery-failure channel and are separate notifications from the delivery alert, so
 * clearing one never hides another.
 */
class ProblemAlerts(
    private val context: Context,
    private val channelId: String,
    private val ensureChannel: () -> Unit,
) {
    private val nm get() = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

    fun showMic(reason: String) = post(
        MIC,
        "Meeting mic is not hearing anything",
        reason.replaceFirstChar { it.uppercase() } + ". Nothing is being recorded.",
        PendingIntent.getActivity(
            context, 15, Intent(context, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        ),
    )

    fun showLocal(reason: String) = post(
        LOCAL,
        "Meeting audio is not being saved on this phone",
        "$reason.",
        PendingIntent.getActivity(
            context, 18, Intent(context, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        ),
    )

    fun showTailscale(launch: Intent) = post(
        TAILSCALE,
        "Tailscale is off — meeting audio can't reach Genesis",
        "Tap to open Tailscale and connect.",
        PendingIntent.getActivity(
            context, 16, launch, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        ),
    )

    /**
     * Opens the app, which restarts capture from the foreground through its normal validated Start
     * path — never a background microphone start, which Android 14+ refuses.
     */
    fun showResume() = post(
        RESUME,
        "Meeting recording stopped unexpectedly",
        "Android closed the recorder. Tap to resume recording.",
        PendingIntent.getActivity(
            context, 17,
            Intent(context, MainActivity::class.java)
                .putExtra(MainActivity.EXTRA_RESUME, true)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        ),
        ongoing = false,
    )

    fun clear(id: Int) = nm.cancel(id)

    private fun post(id: Int, title: String, text: String, pi: PendingIntent, ongoing: Boolean = true) {
        ensureChannel()
        nm.notify(
            id,
            NotificationCompat.Builder(context, channelId)
                .setContentTitle(title)
                .setContentText(text)
                .setStyle(NotificationCompat.BigTextStyle().bigText(text))
                .setSmallIcon(R.drawable.ic_mic)
                .setPriority(NotificationCompat.PRIORITY_HIGH)
                .setContentIntent(pi)
                .setAutoCancel(!ongoing)
                .setVibrate(longArrayOf(0, 300))
                .setOnlyAlertOnce(true)
                .setOngoing(ongoing)
                .build(),
        )
    }

    companion object {
        const val MIC = 3
        const val TAILSCALE = 4
        const val RESUME = 5
        const val LOCAL = 6
    }
}
