package com.genesis.meetingmic

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.util.concurrent.TimeUnit

/**
 * Runs [SegmentSync] in the background. WorkManager persists the schedule across app restarts and
 * reboots, so a recording made offline catches up on its own once a network is back. "Connected" does
 * not mean the edge is reachable (the VPN may be down), so a failed pass asks to be retried.
 */
class SyncWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val done = try {
            SegmentSync.runOnce(applicationContext)
        } catch (e: Exception) {
            false
        }
        if (done) {
            applicationContext.getSystemService(NotificationManager::class.java)?.cancel(NOTIF_ID)
        } else {
            warnIfStuck(applicationContext)
        }
        if (done) Result.success() else Result.retry()
    }

    /** A recording that has not reached the edge for a day gets a loud alert, not just a status line. */
    private fun warnIfStuck(context: Context) {
        val s = SegmentSync.status(context)
        if (s.waiting == 0 || s.oldestWaitingMs < STUCK_MS) return
        val nm = context.getSystemService(NotificationManager::class.java) ?: return
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && nm.getNotificationChannel(CHANNEL) == null) {
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL, "Recording sync problems", NotificationManager.IMPORTANCE_HIGH),
            )
        }
        val open = PendingIntent.getActivity(
            context, 19, Intent(context, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val text = "${s.waiting} recording file(s) have not reached Genesis for over a day. " +
            "They are safe on this phone. " +
            if (s.hasEndpoint) "Check Tailscale, and set battery use to Unrestricted."
            else "No secure (wss://) bridge address is saved: open the app and start a capture with one."
        nm.notify(
            NOTIF_ID,
            NotificationCompat.Builder(context, CHANNEL)
                .setSmallIcon(R.drawable.ic_mic)
                .setContentTitle("Meeting recordings not synced")
                .setContentText(text)
                .setStyle(NotificationCompat.BigTextStyle().bigText(text))
                .setContentIntent(open)
                .setOnlyAlertOnce(true)
                .setAutoCancel(true)
                .build(),
        )
    }

    companion object {
        private const val NOW = "segment-sync-now"
        private const val STUCK_MS = 24L * 60 * 60 * 1000
        private const val CHANNEL = "recording_sync"
        private const val NOTIF_ID = 7
        private const val PERIODIC = "segment-sync-hourly"

        private val online = Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()

        /**
         * A sync pass now (a file closed, capture stopped, app opened). REPLACE restarts a job that is
         * sitting in retry backoff, so a recovered network is used at once; passes never overlap
         * because [SegmentSync] runs one at a time per process.
         */
        fun kick(context: Context) {
            val req = OneTimeWorkRequestBuilder<SyncWorker>()
                .setConstraints(online)
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
                .build()
            WorkManager.getInstance(context).enqueueUniqueWork(NOW, ExistingWorkPolicy.REPLACE, req)
        }

        /** Calls [onChange] whenever a sync job (the kick or the hourly one) changes state. */
        fun observe(context: Context, owner: androidx.lifecycle.LifecycleOwner, onChange: () -> Unit) {
            val wm = WorkManager.getInstance(context)
            for (name in listOf(NOW, PERIODIC)) {
                wm.getWorkInfosForUniqueWorkLiveData(name).observe(owner) { onChange() }
            }
        }

        /** The hourly catch-up that survives restarts and reboots. Idempotent. */
        fun schedule(context: Context) {
            val req = PeriodicWorkRequestBuilder<SyncWorker>(1, TimeUnit.HOURS).setConstraints(online).build()
            WorkManager.getInstance(context)
                .enqueueUniquePeriodicWork(PERIODIC, ExistingPeriodicWorkPolicy.KEEP, req)
        }
    }
}
