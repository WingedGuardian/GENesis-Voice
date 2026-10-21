package com.genesis.meetingmic

import android.content.Context
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.TimeUnit
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock

/**
 * Mirrors every closed local recording to the edge, and deletes the phone copy only on proof.
 *
 * The recordings folder IS the queue: `X.aac` is waiting, `X.synced.aac` is held by the edge. No
 * other state can drift from the files. One pass ([runPass]):
 *
 * 1. Upload each waiting, closed file. The edge answers `stored` only after writing the bytes,
 *    re-reading them from disk and re-hashing them; the file becomes `X.synced.aac` only when that
 *    hash AND length equal the phone's own. Anything else leaves it waiting.
 * 2. For a synced file past the grace period (or sooner when storage is low), ask the edge again
 *    whether it still holds those exact bytes. Only a yes deletes the phone copy; a no puts the file
 *    back in the queue; no answer leaves it alone.
 *
 * The owner's rule: when there is any doubt, keep the file (the 90-day retention still applies).
 */
object SegmentSync {
    const val GRACE_MS = 7L * 24 * 60 * 60 * 1000
    private const val SETTLE_MS = 10_000L          // never touch a file modified this recently
    private const val REJECT_RETRY_MS = 24L * 60 * 60 * 1000
    private const val FILE_RETRY_MS = 10L * 60 * 1000  // a file the edge could not take right now
    const val CREDS = "sync_creds"
    private const val STATE = "sync_state"

    /** Guards every rename/delete in the recordings folder (sync and retention). */
    val folderLock = ReentrantLock()

    /** One pass at a time in this process, whichever job or kick started it. */
    private val passLock = ReentrantLock()

    enum class Outcome { CONFIRMED, RETRY_FILE, RETRY_ALL, REJECTED }

    data class Status(
        val waiting: Int,
        val oldestWaitingMs: Long,
        val synced: Int,
        val rejected: Int,
        val expiredUnsynced: Int,
        val lastSuccessUtcMs: Long,
        val hasEndpoint: Boolean = true,
    )

    /** Small persisted counters/timestamps; SharedPreferences in the app, a map in tests. */
    interface State {
        fun getLong(key: String): Long
        fun putLong(key: String, value: Long)
        fun remove(key: String)
        fun keys(): Set<String>
    }

    // ── pure decisions (unit-tested) ───────────────────────────────────────
    /** The upload is proven only if the edge's re-read hash and length equal ours. */
    fun receiptConfirms(body: String, sha256: String, size: Long): Boolean = try {
        val j = JSONObject(body)
        j.optString("status") == "stored" && j.optString("sha256") == sha256 && j.optLong("bytes", -1) == size
    } catch (e: Exception) {
        false
    }

    fun classify(httpCode: Int, receiptOk: Boolean): Outcome = when {
        httpCode == 200 && receiptOk -> Outcome.CONFIRMED
        httpCode == 200 -> Outcome.RETRY_FILE  // a 200 that does not prove our bytes is doubt
        httpCode == 408 || httpCode == 429 || httpCode >= 500 -> Outcome.RETRY_FILE
        else -> Outcome.REJECTED
    }

    fun deleteDue(syncedAtMs: Long, nowMs: Long, lowStorage: Boolean): Boolean =
        lowStorage || nowMs - syncedAtMs >= GRACE_MS

    /** `seg_x.synced.aac` and `seg_x.aac` share the base `seg_x` (and its sidecar `seg_x.json`). */
    fun baseOf(file: File): String = file.name.removeSuffix(".aac").removeSuffix(".synced")

    fun isSynced(file: File): Boolean = file.name.endsWith(".synced.aac")

    fun sha256(data: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(data).joinToString("") { "%02x".format(it) }

    // ── app wiring ──────────────────────────────────────────────────────────
    private val defaultHttp by lazy {
        OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .writeTimeout(60, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)
            // A redirect could point the recording at a plain http address: never follow one.
            .followRedirects(false)
            .followSslRedirects(false)
            .build()
    }

    /** Saved whenever an endpoint is known (and kept after Stop) so sync can always finish. */
    fun saveEndpoint(context: Context, wsBase: String, token: String) {
        // Only an encrypted address is kept: sync carries whole recordings and the token.
        if (token.isBlank() || !wsBase.trim().startsWith("wss://")) return
        context.getSharedPreferences(CREDS, Context.MODE_PRIVATE).edit()
            .putString("ws_base", wsBase.trim()).putString("token", token.trim()).apply()
    }

    fun hasEndpoint(context: Context): Boolean = endpoint(context) != null

    private fun endpoint(context: Context): String? {
        val p = context.getSharedPreferences(CREDS, Context.MODE_PRIVATE)
        return httpBase(p.getString("ws_base", "").orEmpty(), p.getString("token", "").orEmpty())
    }

    /**
     * `wss://host/meeting/` + token → `https://host/meeting/<token>`; null when unusable. A plain
     * `ws://` address is unusable: recordings never leave the phone unencrypted, they wait instead.
     */
    fun httpBase(wsBase: String, token: String): String? {
        val b = wsBase.trim()
        val t = token.trim()
        if (t.isEmpty() || !b.startsWith("wss://")) return null
        return ("https://" + b.removePrefix("wss://")).trimEnd('/') + "/" + t
    }

    private fun prefsState(context: Context): State {
        val p = context.getSharedPreferences(STATE, Context.MODE_PRIVATE)
        return object : State {
            override fun getLong(key: String) = try { p.getLong(key, 0) } catch (e: ClassCastException) { 0L }
            override fun putLong(key: String, value: Long) { p.edit().putLong(key, value).apply() }
            override fun remove(key: String) { p.edit().remove(key).apply() }
            override fun keys(): Set<String> = p.all.keys
        }
    }

    /** One sync pass for the app. Returns true only when nothing closed is left unsynced. */
    fun runOnce(context: Context): Boolean {
        val dir = LocalRecorder.dir(context)
        val ep = endpoint(context)
        if (ep == null) return unsyncedClosed(dir).isEmpty()  // files waiting with no endpoint = not done
        val low = LocalRecorder.allocatableBytes(context, dir) < LocalRecorder.LOW_STORAGE_BYTES
        return runPass(dir, ep, prefsState(context), low, defaultHttp, System::currentTimeMillis)
    }

    // ── the pass (Context-free, tested against a fake edge) ─────────────────
    fun runPass(
        dir: File,
        ep: String,
        state: State,
        lowStorage: Boolean,
        http: OkHttpClient,
        now: () -> Long,
    ): Boolean = passLock.withLock {
        migrateLegacyNames(dir)
        forgetStaleKeys(dir, state)
        var uploadedAny: Boolean
        do {  // files can close while we run; loop until a pass uploads nothing new
            uploadedAny = false
            for (f in waiting(dir, now())) {
                if (f.length() == 0L) {
                    folderLock.withLock { f.delete(); File(dir, baseOf(f) + ".json").delete() }
                    continue
                }
                val rejectedAt = state.getLong("rejected:" + f.name)
                if (rejectedAt > 0 && now() - rejectedAt < REJECT_RETRY_MS) continue
                val retryAt = state.getLong("retry:" + f.name)
                if (retryAt > 0 && now() < retryAt) continue
                when (upload(http, ep, dir, f, now())) {
                    Outcome.CONFIRMED -> {
                        uploadedAny = true
                        state.remove("rejected:" + f.name)
                        state.remove("retry:" + f.name)
                        state.putLong("last_success", now())
                    }
                    Outcome.REJECTED -> state.putLong("rejected:" + f.name, now())
                    // The edge answered but could not take this file now: skip it, keep going.
                    Outcome.RETRY_FILE -> state.putLong("retry:" + f.name, now() + FILE_RETRY_MS)
                    // No answer at all (network, VPN, edge down): stop the pass, retry later.
                    Outcome.RETRY_ALL -> return@withLock false
                }
            }
        } while (uploadedAny)

        // Synced files past their grace period: re-verify, then delete or re-queue.
        var allDone = true
        for (f in dir.listFiles { x -> isSynced(x) }.orEmpty()) {
            if (!deleteDue(f.lastModified(), now(), lowStorage)) continue
            when (stillHeld(http, ep, f)) {
                true -> folderLock.withLock {
                    if (f.exists()) {
                        f.delete()
                        File(dir, baseOf(f) + ".json").delete()
                    }
                }
                false -> folderLock.withLock {
                    // The edge no longer holds these bytes: back into the queue.
                    if (f.exists()) f.renameTo(File(dir, baseOf(f) + ".aac"))
                    allDone = false
                }
                null -> allDone = false
            }
        }
        // Not done while ANY closed file is unsynced, including one too fresh to touch this pass (the
        // final file at Stop): the caller retries shortly instead of waiting for the hourly pass.
        allDone && unsyncedClosed(dir).isEmpty()
    }

    private fun unsyncedClosed(dir: File): List<File> =
        dir.listFiles { f -> f.name.endsWith(".aac") && !isSynced(f) }.orEmpty()
            .filter { !LocalRecorder.isOpen(baseOf(it)) }

    private fun waiting(dir: File, now: Long): List<File> =
        unsyncedClosed(dir).filter { now - it.lastModified() > SETTLE_MS }.sortedBy { it.name }

    private fun forgetStaleKeys(dir: File, state: State) {
        val present = dir.list().orEmpty().toSet()
        for (k in state.keys()) {
            val name = k.substringAfter(':', "")
            if ((k.startsWith("rejected:") || k.startsWith("retry:")) && name !in present) state.remove(k)
        }
    }

    private val LEGACY = Regex("""^(seg_\d{8}T\d{6})_r\d+_(s\d+)$""")

    /**
     * Files from builds before 0.6.0 were named with the process-local run counter, which repeats
     * after every app restart, so the edge (which stores by name) refuses them. Give each one a unique
     * id once, sidecar included; the audio bytes are untouched.
     *
     * The new name is derived from the old one, so a migration interrupted between the two renames
     * is finished by the next call: a sidecar left under its old name follows its audio.
     */
    fun migrateLegacyNames(dir: File) {
        folderLock.withLock {
            for (f in dir.listFiles { x -> x.name.endsWith(".aac") }.orEmpty()) {
                val base = baseOf(f)
                val fresh = migratedBase(base) ?: continue
                if (LocalRecorder.isOpen(base)) continue
                val target = File(dir, fresh + (if (isSynced(f)) ".synced.aac" else ".aac"))
                if (target.exists()) continue  // never overwrite a recording
                // Audio first: if that rename fails, nothing moved and the next pass tries again.
                if (f.renameTo(target)) {
                    File(dir, "$base.json").takeIf { it.exists() }?.renameTo(File(dir, "$fresh.json"))
                }
            }
            for (side in dir.listFiles { x -> x.name.endsWith(".json") }.orEmpty()) {
                val fresh = migratedBase(side.nameWithoutExtension) ?: continue
                if (File(dir, "$fresh.json").exists()) continue
                if (File(dir, "$fresh.aac").exists() || File(dir, "$fresh.synced.aac").exists()) {
                    side.renameTo(File(dir, "$fresh.json"))
                }
            }
        }
    }

    /** The unique name a legacy segment migrates to; null when [base] is not a legacy name. */
    fun migratedBase(base: String): String? {
        val m = LEGACY.matchEntire(base) ?: return null
        val id = UUID.nameUUIDFromBytes("legacy-segment:$base".toByteArray())
        return "${m.groupValues[1]}_${id}_${m.groupValues[2]}"
    }

    private fun upload(http: OkHttpClient, ep: String, dir: File, f: File, nowMs: Long): Outcome {
        val data = try { f.readBytes() } catch (e: IOException) { return Outcome.RETRY_FILE }
        val sha = sha256(data)
        val base = baseOf(f)
        val side = File(dir, "$base.json").takeIf { it.exists() }?.readText()
        val url = "$ep/upload".toHttpUrlOrNull()?.newBuilder()
            ?.addQueryParameter("name", base)
            ?.addQueryParameter("sha256", sha)
            ?.addQueryParameter("bytes", data.size.toString())
            ?.apply { if (side != null && side.length <= 4096) addQueryParameter("meta", side) }
            ?.build() ?: return Outcome.REJECTED
        val req = Request.Builder().url(url).post(data.toRequestBody("audio/aac".toMediaType())).build()
        val outcome = try {
            http.newCall(req).execute().use { r ->
                val body = r.body?.string().orEmpty()
                classify(r.code, r.code == 200 && receiptConfirms(body, sha, data.size.toLong()))
            }
        } catch (e: IOException) {
            Outcome.RETRY_ALL
        }
        if (outcome != Outcome.CONFIRMED) return outcome
        // Proven for these exact bytes; record it only if the rename really happens.
        return folderLock.withLock {
            val synced = File(dir, "$base.synced.aac")
            if (f.exists() && f.length() == data.size.toLong() && f.renameTo(synced)) {
                synced.setLastModified(nowMs)  // the grace period counts from confirmation
                Outcome.CONFIRMED
            } else {
                Outcome.RETRY_FILE
            }
        }
    }

    /** true = edge re-hashed and holds exactly these bytes; false = it does not; null = no answer. */
    private fun stillHeld(http: OkHttpClient, ep: String, f: File): Boolean? {
        val sha = try { sha256(f.readBytes()) } catch (e: IOException) { return null }
        val url = "$ep/have".toHttpUrlOrNull()?.newBuilder()
            ?.addQueryParameter("name", baseOf(f))
            ?.addQueryParameter("sha256", sha)
            ?.build() ?: return null
        return try {
            http.newCall(Request.Builder().url(url).get().build()).execute().use { r ->
                if (r.code != 200) return null
                val j = JSONObject(r.body?.string().orEmpty())
                if (!j.has("have")) null else j.getBoolean("have")
            }
        } catch (e: Exception) {
            null
        }
    }

    fun status(context: Context, nowMs: Long = System.currentTimeMillis()): Status {
        val dir = LocalRecorder.dir(context)
        val files = dir.listFiles { f -> f.name.endsWith(".aac") }.orEmpty()
        val waitingFiles = files.filter { !isSynced(it) && !LocalRecorder.isOpen(baseOf(it)) }
        val state = prefsState(context)
        return Status(
            waiting = waitingFiles.size,
            oldestWaitingMs = waitingFiles.maxOfOrNull { nowMs - it.lastModified() } ?: 0,
            synced = files.count { isSynced(it) },
            rejected = waitingFiles.count { state.getLong("rejected:" + it.name) > 0 },
            expiredUnsynced = state.getLong("expired_unsynced").toInt(),
            lastSuccessUtcMs = state.getLong("last_success"),
            hasEndpoint = endpoint(context) != null,
        )
    }

    fun noteExpiredUnsynced(context: Context, count: Int) {
        if (count <= 0) return
        val s = prefsState(context)
        s.putLong("expired_unsynced", s.getLong("expired_unsynced") + count)
    }
}
