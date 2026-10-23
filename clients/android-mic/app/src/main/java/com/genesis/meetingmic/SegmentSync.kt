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
 *
 * Each recording goes to the bridge it was captured for, never to whatever is configured later: the
 * capture run registers its endpoint and every segment gets a `X.dest` file naming it (see
 * [registerEndpoint]). A recording captured over plain `ws://` is marked [DEST_NONE] and stays on the
 * phone. Recordings from builds that wrote no `X.dest` are bound once, on first sight, to the
 * endpoint saved first after the upgrade.
 */
object SegmentSync {
    const val GRACE_MS = 7L * 24 * 60 * 60 * 1000
    private const val SETTLE_MS = 10_000L          // never touch a file modified this recently
    private const val REJECT_RETRY_MS = 24L * 60 * 60 * 1000
    private const val FILE_RETRY_MS = 10L * 60 * 1000  // a file the edge could not take right now
    private const val SIDECAR_WAIT_MS = 24L * 60 * 60 * 1000  // then upload without unreadable metadata
    const val CREDS = "sync_creds"
    /** A recording with no encrypted destination (captured over `ws://`): it never leaves the phone. */
    const val DEST_NONE = "none"
    private const val DEFAULT_KEY = "default"
    /** Recordings made before this moment, with no `X.dest`, may take the default; later ones never. */
    private const val CUTOFF_KEY = "default_cutoff_ms"
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
        /** Captured with no encrypted destination: kept on the phone, never uploaded. */
        val unroutable: Int = 0,
        val oldestUnroutableMs: Long = 0,
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

    /** The small files that belong to a recording and go wherever it goes. */
    private val COMPANIONS = listOf(".json", ".dest")

    /** Deletes a recording's companions; call only once its audio is gone. */
    fun deleteCompanions(dir: File, base: String) {
        for (ext in COMPANIONS) File(dir, base + ext).delete()
    }

    /**
     * Stable id of a bridge: its address only. A new token for the same address is the same server,
     * so recordings waiting for it follow the new token ([registerEndpoint] overwrites it); a
     * different address is a different id and never receives them. Holds no secret.
     */
    fun endpointId(wsBase: String): String =
        sha256(wsBase.trim().trimEnd('/').toByteArray()).take(16)

    /**
     * The destination id recorded for [file]. A recording with none takes [legacyDefault] only if it
     * was recorded before [legacyCutoffMs] (made by a build that wrote no `X.dest`); with [bind] the
     * choice is written once and never changes. A newer recording that has lost its `X.dest` gets
     * nothing: it waits rather than go to a bridge it may not have been captured for. Null = no
     * usable destination.
     */
    fun destinationOf(
        dir: File,
        file: File,
        legacyDefault: String?,
        legacyCutoffMs: Long = Long.MAX_VALUE,
        bind: Boolean = true,
    ): String? {
        val dest = File(dir, baseOf(file) + ".dest")
        val id = try {
            if (dest.exists()) dest.readText().trim() else null
        } catch (e: IOException) {
            return null
        }
        if (id != null) return id.takeIf { it.isNotEmpty() && it != DEST_NONE }
        if (legacyDefault == null || recordedAtMs(dir, file) >= legacyCutoffMs) return null
        if (bind) writeDest(dest, legacyDefault)
        return legacyDefault
    }

    /** When a recording started: its sidecar's `utc_start_ms`, else the file's modification time. */
    private fun recordedAtMs(dir: File, file: File): Long = try {
        JSONObject(File(dir, baseOf(file) + ".json").readText()).getLong("utc_start_ms")
    } catch (e: Exception) {
        file.lastModified()
    }

    /** Writes and syncs a destination file; one write per recording, before any upload. */
    fun writeDest(dest: File, id: String) {
        java.io.FileOutputStream(dest).use { out ->
            out.write(id.toByteArray())
            out.fd.sync()
        }
    }
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

    /**
     * Registers the endpoint a capture run streams to and returns the id its recordings are bound to,
     * or [DEST_NONE] when it is not encrypted (recordings never leave the phone over `ws://`). The
     * first endpoint registered after an upgrade also becomes the destination of older recordings
     * that have none; it never changes after that, so a later address change cannot capture them.
     */
    fun registerEndpoint(context: Context, wsBase: String, token: String): String =
        registerEndpoint(context.getSharedPreferences(CREDS, Context.MODE_PRIVATE), wsBase, token, prefsState(context))

    /** [registerEndpoint] against the credentials store itself (tests pass an in-memory one). */
    fun registerEndpoint(
        p: android.content.SharedPreferences,
        wsBase: String,
        token: String,
        state: State? = null,
    ): String {
        adoptLegacyEndpoint(p)
        val ep = httpBase(wsBase, token) ?: return DEST_NONE
        val id = endpointId(wsBase)
        val previous = p.getString("ep:$id", null)
        val edit = p.edit().putString("ep:$id", ep)
        if (p.getString(DEFAULT_KEY, null) == null) {
            edit.putString(DEFAULT_KEY, id).putLong(CUTOFF_KEY, System.currentTimeMillis())
        }
        // Durable before any recording names it. Callers run on the main thread (onResume and
        // startCapture), which is also what makes the check-then-set of the default race-free.
        edit.commit()
        // A new credential for a known bridge: files refused under the old one are tried again on the
        // next pass, instead of waiting out their day-long refusal delay.
        if (state != null && previous != null && previous != ep) {
            for (k in state.keys()) if (k.startsWith("rejected:")) state.remove(k)
        }
        return id
    }

    /**
     * Builds before per-recording destinations kept ONE endpoint (`ws_base` + `token`): that is where
     * their recordings were going, so it becomes the default for them, ahead of anything newer. The
     * old keys are then removed. Idempotent.
     */
    fun adoptLegacyEndpoint(p: android.content.SharedPreferences) {
        val base = p.getString("ws_base", null) ?: return
        val token = p.getString("token", null).orEmpty()
        val edit = p.edit().remove("ws_base").remove("token")
        val ep = httpBase(base, token)
        if (ep != null && p.getString(DEFAULT_KEY, null) == null) {
            val id = endpointId(base)
            edit.putString("ep:$id", ep).putString(DEFAULT_KEY, id).putLong(CUTOFF_KEY, System.currentTimeMillis())
        }
        edit.commit()
    }

    /** True once any encrypted endpoint is known (capture streams to one, or an older build saved one). */
    fun hasEndpoint(context: Context): Boolean =
        context.getSharedPreferences(CREDS, Context.MODE_PRIVATE).all.keys.any { it.startsWith("ep:") || it == "ws_base" }

    /**
     * Maps each recording to the https base of ITS destination; null = cannot be sent. With
     * [bind] false (status reads) nothing is written.
     */
    private fun router(context: Context, dir: File, bind: Boolean = true): (File) -> String? {
        val p = context.getSharedPreferences(CREDS, Context.MODE_PRIVATE)
        if (bind) adoptLegacyEndpoint(p)
        val legacyDefault = p.getString(DEFAULT_KEY, null)
        val cutoff = p.getLong(CUTOFF_KEY, Long.MAX_VALUE)
        return { f -> destinationOf(dir, f, legacyDefault, cutoff, bind)?.let { p.getString("ep:$it", null) } }
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
        val low = LocalRecorder.allocatableBytes(context, dir) < LocalRecorder.LOW_STORAGE_BYTES
        return runPass(dir, router(context, dir), prefsState(context), low, defaultHttp, System::currentTimeMillis)
    }

    // ── the pass (Context-free, tested against a fake edge) ─────────────────
    fun runPass(
        dir: File,
        route: (File) -> String?,
        state: State,
        lowStorage: Boolean,
        http: OkHttpClient,
        now: () -> Long,
    ): Boolean = passLock.withLock {
        migrateLegacyNames(dir)
        forgetStaleKeys(dir, state)
        // An endpoint that gave no answer is skipped for the rest of the pass; the others go on.
        val unreachable = HashSet<String>()
        var uploadedAny: Boolean
        do {  // files can close while we run; loop until a pass uploads nothing new
            uploadedAny = false
            for (f in waiting(dir, now())) {
                if (f.length() == 0L) {
                    folderLock.withLock { if (f.delete() || !f.exists()) deleteCompanions(dir, baseOf(f)) }
                    continue
                }
                val ep = route(f)
                // No destination of its own: it waits. Never sent to whatever is configured now.
                if (ep == null) continue
                if (ep in unreachable) continue
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
                    // No answer at all (network, VPN, edge down): skip that edge, retry later.
                    Outcome.RETRY_ALL -> unreachable.add(ep)
                }
            }
        } while (uploadedAny)

        // Synced files past their grace period: re-verify, then delete or re-queue.
        var allDone = true
        for (f in dir.listFiles { x -> isSynced(x) }.orEmpty()) {
            if (!deleteDue(f.lastModified(), now(), lowStorage)) continue
            // Asked only of the edge it was sent to: another edge's "yes" proves nothing.
            val ep = route(f)
            // No destination: nothing a retry can do, so it alone does not keep the job retrying;
            // the copy stays under the 90-day rule. An edge that did not answer is worth a retry.
            if (ep == null) continue
            if (ep in unreachable) {
                allDone = false
                continue
            }
            when (stillHeld(http, ep, f)) {
                true -> folderLock.withLock {
                    if (f.exists() && f.delete()) deleteCompanions(dir, baseOf(f))
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
        // A recording with no destination never can be, so it alone does not keep the job retrying.
        allDone && unreachable.isEmpty() && unsyncedClosed(dir).all { route(it) == null }
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
                    for (ext in COMPANIONS) {
                        File(dir, base + ext).takeIf { it.exists() }?.renameTo(File(dir, fresh + ext))
                    }
                }
            }
            for (side in dir.listFiles { x -> COMPANIONS.any { x.name.endsWith(it) } }.orEmpty()) {
                val fresh = migratedBase(side.nameWithoutExtension) ?: continue
                val target = File(dir, fresh + "." + side.extension)
                if (target.exists()) continue
                if (File(dir, "$fresh.aac").exists() || File(dir, "$fresh.synced.aac").exists()) {
                    side.renameTo(target)
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
        val base = baseOf(f)
        // An unreadable sidecar holds back only this file, never the queue behind it. For a day it is
        // retried, since the edge cannot recover the timing later; after that the audio goes without
        // it, because losing the timing is better than the phone holding the only copy.
        val side = try {
            File(dir, "$base.json").takeIf { it.exists() }?.readText()
        } catch (e: IOException) {
            if (nowMs - f.lastModified() < SIDECAR_WAIT_MS) return Outcome.RETRY_FILE
            null
        }
        val data = try { f.readBytes() } catch (e: IOException) { return Outcome.RETRY_FILE }
        val sha = sha256(data)
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
        val route = router(context, dir, bind = false)
        val (waitingFiles, unroutable) = files
            .filter { !isSynced(it) && !LocalRecorder.isOpen(baseOf(it)) }
            .partition { route(it) != null }
        val state = prefsState(context)
        return Status(
            unroutable = unroutable.size,
            oldestUnroutableMs = unroutable.maxOfOrNull { nowMs - it.lastModified() } ?: 0,
            waiting = waitingFiles.size,
            oldestWaitingMs = waitingFiles.maxOfOrNull { nowMs - it.lastModified() } ?: 0,
            synced = files.count { isSynced(it) },
            rejected = waitingFiles.count { state.getLong("rejected:" + it.name) > 0 },
            expiredUnsynced = state.getLong("expired_unsynced").toInt(),
            lastSuccessUtcMs = state.getLong("last_success"),
            hasEndpoint = hasEndpoint(context),
        )
    }

    fun noteExpiredUnsynced(context: Context, count: Int) {
        if (count <= 0) return
        val s = prefsState(context)
        s.putLong("expired_unsynced", s.getLong("expired_unsynced") + count)
    }
}
