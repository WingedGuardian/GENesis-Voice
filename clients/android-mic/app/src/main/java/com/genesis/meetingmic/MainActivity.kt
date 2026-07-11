package com.genesis.meetingmic

import android.Manifest
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import com.genesis.meetingmic.databinding.ActivityMainBinding
import kotlinx.coroutines.launch

/**
 * Thin control surface for [MicStreamService]: endpoint + token entry, Start/Stop, live status,
 * and — because the target device is a Samsung (aggressive background-killer) — a first-run
 * battery-optimization exemption prompt. Without that exemption Samsung "sleeps" the capture
 * service mid-meeting despite the foreground notification.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var b: ActivityMainBinding

    private val permLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions(),
    ) { grants ->
        if (grants[Manifest.permission.RECORD_AUDIO] == true) {
            startCapture()
        } else {
            b.status.text = getString(R.string.status_mic_denied)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivityMainBinding.inflate(layoutInflater)
        setContentView(b.root)

        // Pre-fill from the build-time defaults; the user can override either field.
        b.wsUrl.setText(BuildConfig.MEETING_WS_URL)
        b.token.setText(BuildConfig.MEETING_TOKEN)

        b.startBtn.setOnClickListener { requestThenStart() }
        b.stopBtn.setOnClickListener {
            startService(Intent(this, MicStreamService::class.java).setAction(MicStreamService.ACTION_STOP))
        }
        b.batteryBtn.setOnClickListener { requestBatteryExemption() }

        observeState()
    }

    override fun onResume() {
        super.onResume()
        refreshBatteryBanner()
    }

    private fun requestThenStart() {
        val needed = buildList {
            if (ContextCompat.checkSelfPermission(this@MainActivity, Manifest.permission.RECORD_AUDIO)
                != android.content.pm.PackageManager.PERMISSION_GRANTED
            ) add(Manifest.permission.RECORD_AUDIO)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
                ContextCompat.checkSelfPermission(this@MainActivity, Manifest.permission.POST_NOTIFICATIONS)
                != android.content.pm.PackageManager.PERMISSION_GRANTED
            ) add(Manifest.permission.POST_NOTIFICATIONS)
        }
        if (needed.isEmpty()) startCapture() else permLauncher.launch(needed.toTypedArray())
    }

    private fun startCapture() {
        val url = b.wsUrl.text.toString().trim()
        val token = b.token.text.toString().trim()
        if (token.isEmpty() || !(url.startsWith("ws://") || url.startsWith("wss://"))) {
            b.status.text = getString(R.string.status_bad_config)
            return
        }
        val i = Intent(this, MicStreamService::class.java)
            .setAction(MicStreamService.ACTION_START)
            .putExtra(MicStreamService.EXTRA_WS_URL, url)
            .putExtra(MicStreamService.EXTRA_TOKEN, token)
        // A microphone FGS must be started while the app is in the foreground (it is — this is a tap).
        ContextCompat.startForegroundService(this, i)
    }

    private fun observeState() {
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                MicStreamService.state.collect { s ->
                    val secs = if (s.startedAtMs > 0)
                        ((System.currentTimeMillis() - s.startedAtMs) / 1000) else 0
                    val kb = s.bytesSent / 1024
                    b.status.text = when (s.phase) {
                        MicStreamService.Companion.Phase.LIVE ->
                            "● Capturing — ${secs}s · ${kb} KB sent"
                        MicStreamService.Companion.Phase.RECONNECTING ->
                            "◍ Reconnecting… (${s.detail})"
                        MicStreamService.Companion.Phase.CONNECTING -> "◌ Connecting…"
                        MicStreamService.Companion.Phase.STOPPED -> "○ Stopped"
                        MicStreamService.Companion.Phase.ERROR -> "✕ Error: ${s.detail}"
                        MicStreamService.Companion.Phase.IDLE -> "○ Idle"
                    }
                    val live = s.phase == MicStreamService.Companion.Phase.LIVE ||
                        s.phase == MicStreamService.Companion.Phase.RECONNECTING ||
                        s.phase == MicStreamService.Companion.Phase.CONNECTING
                    b.startBtn.isEnabled = !live
                    b.stopBtn.isEnabled = live
                }
            }
        }
    }

    // ── battery optimization (Samsung) ─────────────────────────────────────────
    private fun isBatteryExempt(): Boolean {
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        return pm.isIgnoringBatteryOptimizations(packageName)
    }

    private fun refreshBatteryBanner() {
        val exempt = isBatteryExempt()
        b.batteryBanner.visibility = if (exempt) android.view.View.GONE else android.view.View.VISIBLE
        b.batteryBtn.visibility = if (exempt) android.view.View.GONE else android.view.View.VISIBLE
    }

    private fun requestBatteryExemption() {
        // Direct exemption dialog. On Samsung ALSO exclude from Device Care → Sleeping apps
        // (there is no API for that list — it is documented in the app's hint text + README).
        val i = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
            .setData(Uri.parse("package:$packageName"))
        try {
            startActivity(i)
        } catch (e: Exception) {
            // Fallback to the general battery-optimization settings list.
            startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
        }
    }
}
