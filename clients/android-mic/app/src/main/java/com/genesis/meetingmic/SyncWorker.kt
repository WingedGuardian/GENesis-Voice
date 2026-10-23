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
        // (StuckCheckWorker raises the same alert when no network lets this job run at all.)
        if (done) Result.success() else Result.retry()
    }

    companion object {
        private const val NOW = "segment-sync-now"
        private const val STUCK_MS = 24L * 60 * 60 * 1000
        private const val CHANNEL = "recording_sync"
        private const val NOTIF_ID = 7
        private const val PERIODIC = "segment-sync-hourly"
        private const val STUCK_CHECK = "segment-sync-stuck-check"

        private val online = Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()

        /** A recording that has not reached the edge for a day gets a loud alert, not just a status line. */
        fun warnIfStuck(context: Context) {
            val s = SegmentSync.status(context)
            val text = stuckText(s) ?: return
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

        /**
         * The hourly catch-up that survives restarts and reboots, plus the stuck-recording check.
         * The check has no network constraint: a phone offline for days is exactly when the alert
         * matters, and the upload jobs never run then. Idempotent.
         */
        fun schedule(context: Context) {
            val wm = WorkManager.getInstance(context)
            val req = PeriodicWorkRequestBuilder<SyncWorker>(1, TimeUnit.HOURS).setConstraints(online).build()
            wm.enqueueUniquePeriodicWork(PERIODIC, ExistingPeriodicWorkPolicy.KEEP, req)
            val check = PeriodicWorkRequestBuilder<StuckCheckWorker>(STUCK_CHECK_HOURS, TimeUnit.HOURS).build()
            wm.enqueueUniquePeriodicWork(STUCK_CHECK, ExistingPeriodicWorkPolicy.KEEP, check)
        }

        /** Every 3 h, so a day-old recording is reported within about 27 h of capture. */
        private const val STUCK_CHECK_HOURS = 3L

        /** Clears the alert once nothing has been waiting for a day. */
        fun clearIfNotStuck(context: Context) {
            if (stuckText(SegmentSync.status(context)) == null) {
                context.getSystemService(NotificationManager::class.java)?.cancel(NOTIF_ID)
            }
        }

        /**
         * The alert text, or null when nothing is stuck: a recording waiting over a day, or one with
         * no secure destination (it will never sync) for over a day.
         */
        fun stuckText(s: SegmentSync.Status): String? {
            val parts = mutableListOf<String>()
            if (s.waiting > 0 && s.oldestWaitingMs >= STUCK_MS) {
                parts += "${s.waiting} recording file(s) have not reached Genesis for over a day. " +
                    "They are safe on this phone. Check Tailscale, and set battery use to Unrestricted."
            }
            if (s.unroutable > 0 && s.oldestUnroutableMs >= STUCK_MS) {
                parts += "${s.unroutable} recording file(s) have no secure (wss://) destination and will " +
                    "not sync. They stay on this phone for 90 days."
            }
            return parts.takeIf { it.isNotEmpty() }?.joinToString(" ")
        }
    }
}

/** Posts the stuck-recording alert on its own schedule, with no connectivity requirement. */
class StuckCheckWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        try {
            SyncWorker.warnIfStuck(applicationContext)
            SyncWorker.clearIfNotStuck(applicationContext)
        } catch (_: Exception) {
            // A failed check retries at its next period; it never touches recordings.
        }
        Result.success()
    }
}
