package com.genesis.meetingmic

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleService
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import java.util.concurrent.atomic.AtomicReference

/**
 * Foreground service that captures the mic at 16 kHz mono PCM16 and streams it, raw, over a
 * WebSocket to the meeting bridge (`wss://<host>/meeting/<token>`). A `microphone`-typed
 * foreground service is the only sanctioned way to keep the mic open while the screen is locked
 * or the app is backgrounded — that survival is the entire reason this app exists.
 *
 * Wire contract (matches the bridge, already deployed + verified):
 *  - binary frames = raw little-endian PCM16 @ 16 kHz mono
 *  - text frames   = JSON control, e.g. {"type":"marker"}
 *
 * On a dropped connection the capture loop keeps running but audio is DROPPED (not buffered)
 * until the socket reconnects — buffering stale realtime audio would desync the bridge's live
 * diarization worse than a short gap does.
 */
class MicStreamService : LifecycleService() {

    companion object {
        const val ACTION_START = "com.genesis.meetingmic.START"
        const val ACTION_STOP = "com.genesis.meetingmic.STOP"
        const val ACTION_MARK = "com.genesis.meetingmic.MARK"
        const val EXTRA_WS_URL = "ws_url"   // full base ending in .../meeting/
        const val EXTRA_TOKEN = "token"
        const val EXTRA_MODEL = "model"     // "standard" | "enhanced" (fixed per session)

        private const val CHANNEL_ID = "capture"
        private const val NOTIF_ID = 1

        // Persisted so a START_STICKY restart (OS kills the process mid-meeting, redelivers a null
        // intent) can resume with the same endpoint/model instead of erroring out. App-private storage.
        private const val PREFS = "capture_creds"
        private const val KEY_URL = "ws_url"
        private const val KEY_TOKEN = "token"
        private const val KEY_MODEL = "model"

        // Runaway backstop: cap a single continuous capture so a forgotten/left-running stream can't
        // stream forever. Sized for a full workday of capture (out-of-house use, ~8 h + commute +
        // buffer) — NOT a per-meeting limit; the bridge's VAD lifecycle already keeps a mostly-silent
        // day cheap (it only opens/bills a cloud session while someone is talking). Resets on each
        // fresh Start (or sticky restart). Once scheduling lands, the scheduled off-time is the
        // primary stop and this stays only as the runaway guard.
        private const val MAX_SESSION_MS = 14L * 60 * 60 * 1000  // 14 hours

        // 16 kHz mono PCM16 — the bridge's native ambient/meeting rate, sent without resampling.
        const val SAMPLE_RATE = 16000
        private const val CHANNEL = AudioFormat.CHANNEL_IN_MONO
        private const val ENCODING = AudioFormat.ENCODING_PCM_16BIT
        // ~100 ms per frame (16000 samples/s * 0.1 s * 2 bytes) — matches the ambient contract's pacing.
        private const val FRAME_BYTES = 3200

        private const val RECONNECT_MIN_MS = 1_000L
        private const val RECONNECT_MAX_MS = 15_000L

        enum class Phase { IDLE, CONNECTING, LIVE, RECONNECTING, STOPPED, ERROR }

        data class CaptureState(
            val phase: Phase = Phase.IDLE,
            val detail: String = "",
            val bytesSent: Long = 0,
            val startedAtMs: Long = 0,
        )

        private val _state = MutableStateFlow(CaptureState())
        /** Observed by [MainActivity] to render live status. */
        val state: StateFlow<CaptureState> = _state.asStateFlow()
    }

    private val ws = AtomicReference<WebSocket?>(null)
    @Volatile private var wsConnected = false
    @Volatile private var bytesSent = 0L
    @Volatile private var startedAtMs = 0L
    // @Volatile: read from OkHttp callback threads (onSocketDown) as well as the main thread.
    @Volatile private var captureJob: Job? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private val mainHandler = Handler(Looper.getMainLooper())

    private val http: OkHttpClient by lazy {
        // No read timeout: a WebSocket is long-lived. The bridge sends heartbeat pings and OkHttp
        // auto-answers them at the protocol level, so the server detects a dead peer; our own
        // onFailure/onClosed drives reconnect.
        OkHttpClient.Builder()
            .readTimeout(0, java.util.concurrent.TimeUnit.MILLISECONDS)
            .retryOnConnectionFailure(true)
            .build()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        super.onStartCommand(intent, flags, startId)
        when (intent?.action) {
            ACTION_STOP -> { stopCapture("stopped"); return START_NOT_STICKY }
            ACTION_MARK -> { sendMarker(); return START_STICKY }
            ACTION_START, null -> {
                var base = intent?.getStringExtra(EXTRA_WS_URL).orEmpty()
                var token = intent?.getStringExtra(EXTRA_TOKEN).orEmpty()
                var model = intent?.getStringExtra(EXTRA_MODEL).orEmpty()
                if (base.isEmpty() || token.isEmpty()) {
                    // Null/empty intent == a START_STICKY restart after an OS kill. Restore the last
                    // creds so capture actually resumes instead of erroring out. (Whether Android
                    // permits re-opening a mic FGS from this background restart is device-dependent;
                    // if it's blocked, startCapture surfaces an error rather than failing silently.)
                    val p = getSharedPreferences(PREFS, MODE_PRIVATE)
                    base = p.getString(KEY_URL, "").orEmpty()
                    token = p.getString(KEY_TOKEN, "").orEmpty()
                    model = p.getString(KEY_MODEL, "").orEmpty()
                }
                startCapture(base, token, model)
            }
        }
        // START_STICKY: if the OS kills us for memory, Android restarts the service (null intent);
        // the restore above gives it the creds to resume.
        return START_STICKY
    }

    // ── capture lifecycle ────────────────────────────────────────────────────
    private fun startCapture(base: String, token: String, model: String) {
        if (captureJob?.isActive == true) return  // already running

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED
        ) {
            publish(Phase.ERROR, "mic permission not granted")
            stopSelf()
            return
        }

        val url = buildUrl(base, token, model)
        if (url == null) {
            publish(Phase.ERROR, "invalid capture URL")
            stopSelf()
            return
        }

        // Persist creds+model so a sticky restart after an OS kill can resume (cleared on explicit stop).
        getSharedPreferences(PREFS, MODE_PRIVATE).edit()
            .putString(KEY_URL, base).putString(KEY_TOKEN, token).putString(KEY_MODEL, model).apply()

        startForegroundNotif()
        acquireWakeLock()
        bytesSent = 0L
        startedAtMs = System.currentTimeMillis()
        publish(Phase.CONNECTING, "connecting")

        captureJob = lifecycleScope.launch(Dispatchers.IO) {
            connectAndStream(url)
        }
    }

    /** Owns the AudioRecord for its whole lifetime; reconnects the socket underneath it. */
    private suspend fun connectAndStream(url: String) {
        val minBuf = AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNEL, ENCODING)
        if (minBuf <= 0) { publish(Phase.ERROR, "AudioRecord unsupported"); stopSelf(); return }
        val recordBuf = maxOf(minBuf, FRAME_BYTES * 2)

        val recorder = try {
            AudioRecord(
                // VOICE_RECOGNITION: tuned for ASR — minimal AGC/processing, the cleanest source
                // for the bridge's Speechmatics diarization.
                MediaRecorder.AudioSource.VOICE_RECOGNITION,
                SAMPLE_RATE, CHANNEL, ENCODING, recordBuf,
            )
        } catch (e: Exception) {
            publish(Phase.ERROR, "AudioRecord init failed"); stopSelf(); return
        }
        if (recorder.state != AudioRecord.STATE_INITIALIZED) {
            publish(Phase.ERROR, "AudioRecord not initialized"); recorder.release(); stopSelf(); return
        }

        var backoff = RECONNECT_MIN_MS
        var ticks = 0  // frame counter; ~10 frames ≈ 1 s at 100 ms/frame
        try {
            recorder.startRecording()
            val buf = ByteArray(FRAME_BYTES)
            openSocket(url)
            // Gate on THIS coroutine's own liveness (not captureJob, which is assigned after launch()
            // returns and could still be null when the body first runs). Cancellation of the job
            // flips this to false and drains the loop.
            while (currentCoroutineContext().isActive) {
                val n = recorder.read(buf, 0, buf.size)
                if (n <= 0) { delay(20); continue }
                // Safety auto-stop after the max continuous duration (routed to the main thread so the
                // FGS teardown runs there; break out of this loop immediately).
                if (startedAtMs > 0 && System.currentTimeMillis() - startedAtMs >= MAX_SESSION_MS) {
                    mainHandler.post { stopCapture("auto-stopped (8h limit)") }
                    break
                }
                val sock = ws.get()
                when {
                    wsConnected && sock != null -> {
                        if (sock.send(buf.toByteString(0, n))) {
                            bytesSent += n
                            backoff = RECONNECT_MIN_MS  // a good send means we're healthy — reset backoff
                            // Refresh the live UI counters ~once/sec (the StateFlow otherwise only
                            // emits on phase changes, so bytes/elapsed would appear frozen at 0).
                            if (++ticks % 10 == 0) publish(Phase.LIVE, "capturing")
                            // ~every 2 s: the notification is the PRIMARY display while the screen
                            // is locked (the app's core use case), so it must not look frozen.
                            if (ticks % 20 == 0) updateNotif(liveNotifText())
                        } else {
                            // send() refused → socket is closing/closed; fall into reconnect.
                            onSocketDown("send failed")
                        }
                    }
                    sock == null && _state.value.phase == Phase.RECONNECTING -> {
                        // Socket is down; pace reconnect attempts with backoff (audio in this window is dropped).
                        delay(backoff)
                        backoff = (backoff * 2).coerceAtMost(RECONNECT_MAX_MS)
                        if (currentCoroutineContext().isActive) openSocket(url)
                    }
                    // else: connected socket not yet open (CONNECTING) → drop this frame, keep reading.
                }
            }
        } catch (e: CancellationException) {
            throw e  // a normal stop (job cancelled) — don't clobber the STOPPED status with ERROR
        } catch (e: Exception) {
            publish(Phase.ERROR, e.message ?: "capture error")
        } finally {
            try { recorder.stop() } catch (_: Exception) {}
            recorder.release()
        }
    }

    private fun openSocket(url: String) {
        // Close any prior socket before opening a fresh one.
        ws.getAndSet(null)?.cancel()
        wsConnected = false
        publish(if (startedAtMs == 0L) Phase.CONNECTING else _state.value.phase)
        val req = Request.Builder().url(url).build()
        val sock = http.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                wsConnected = true
                publish(Phase.LIVE, "capturing")
                updateNotif("Capturing — connected")
            }
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                onSocketDown("closed ${code}")
            }
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                onSocketDown(t.message ?: "ws failure")
            }
        })
        ws.set(sock)
    }

    private fun onSocketDown(reason: String) {
        wsConnected = false
        ws.getAndSet(null)
        if (captureJob?.isActive == true) {
            publish(Phase.RECONNECTING, "reconnecting ($reason)")
            updateNotif("Reconnecting…")
        }
    }

    private fun sendMarker() {
        val sock = ws.get()
        if (wsConnected && sock != null) {
            sock.send("{\"type\":\"marker\"}")
            updateNotif("Marked ✓")
        }
    }

    private fun stopCapture(reason: String) {
        // Explicit stop → forget creds so a later sticky restart doesn't silently resume capture.
        getSharedPreferences(PREFS, MODE_PRIVATE).edit().clear().apply()
        captureJob?.cancel()
        captureJob = null
        wsConnected = false
        ws.getAndSet(null)?.close(1000, "client stop")
        releaseWakeLock()
        publish(Phase.STOPPED, reason)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            stopForeground(STOP_FOREGROUND_REMOVE)
        } else {
            @Suppress("DEPRECATION") stopForeground(true)
        }
        stopSelf()
    }

    override fun onDestroy() {
        // Belt-and-suspenders: never leak the wake lock or socket if the OS tears us down.
        captureJob?.cancel()
        ws.getAndSet(null)?.cancel()
        releaseWakeLock()
        super.onDestroy()
    }

    // ── helpers ──────────────────────────────────────────────────────────────
    private fun buildUrl(base: String, token: String, model: String): String? {
        val b = base.trim()
        val t = token.trim()
        if (t.isEmpty()) return null
        if (!b.startsWith("ws://") && !b.startsWith("wss://")) return null
        val sep = if (b.endsWith("/")) "" else "/"
        // Model rides as a query param; the bridge validates it against a whitelist and falls back
        // to its default if it's anything other than standard/enhanced, so an empty value is safe.
        val m = model.trim().lowercase()
        val query = if (m == "standard" || m == "enhanced") "?model=$m" else ""
        return b + sep + t + query
    }

    private fun publish(phase: Phase, detail: String = _state.value.detail) {
        _state.value = CaptureState(phase, detail, bytesSent, startedAtMs)
    }

    private fun acquireWakeLock() {
        if (wakeLock?.isHeld == true) return
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "GenesisMeetingMic::capture").apply {
            setReferenceCounted(false)
            acquire()
        }
    }

    private fun releaseWakeLock() {
        try { if (wakeLock?.isHeld == true) wakeLock?.release() } catch (_: Exception) {}
        wakeLock = null
    }

    // ── notification ─────────────────────────────────────────────────────────
    private fun startForegroundNotif() {
        createChannel()
        val notif = buildNotif("Starting…")
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIF_ID, notif, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        } else {
            startForeground(NOTIF_ID, notif)
        }
    }

    private fun updateNotif(text: String) {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.notify(NOTIF_ID, buildNotif(text))
    }

    private fun liveNotifText(): String {
        val secs = if (startedAtMs > 0) (System.currentTimeMillis() - startedAtMs) / 1000 else 0L
        val kb = bytesSent / 1024
        return "Capturing — ${secs / 60}:${(secs % 60).toString().padStart(2, '0')} · $kb KB"
    }

    private fun buildNotif(text: String): Notification {
        val stopPi = servicePendingIntent(ACTION_STOP, 10)
        val markPi = servicePendingIntent(ACTION_MARK, 11)
        val openPi = PendingIntent.getActivity(
            this, 12, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("Genesis meeting capture")
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_mic)
            .setOngoing(true)
            .setContentIntent(openPi)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .addAction(R.drawable.ic_mic, "Mark", markPi)
            .addAction(R.drawable.ic_mic, "Stop", stopPi)
            .build()
    }

    private fun servicePendingIntent(action: String, req: Int): PendingIntent {
        val i = Intent(this, MicStreamService::class.java).setAction(action)
        return PendingIntent.getService(
            this, req, i,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            if (nm.getNotificationChannel(CHANNEL_ID) == null) {
                nm.createNotificationChannel(
                    NotificationChannel(
                        CHANNEL_ID, "Meeting capture", NotificationManager.IMPORTANCE_LOW,
                    ).apply { description = "Ongoing while streaming meeting audio" },
                )
            }
        }
    }
}
