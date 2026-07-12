# Genesis Meeting Mic (Android capture client)

A minimal Android app that captures the phone mic at **16 kHz mono PCM16** and streams it, raw,
over a WebSocket to the [meeting bridge](../../bridges/meeting_bridge/) — which runs it through
Speechmatics real-time diarization and writes a live `.md` transcript. Unlike the browser capture
page, this keeps recording **while the screen is locked and the app is backgrounded**, via a
`microphone`-typed foreground service. That survival is the whole reason the app exists.

It is a *client only*: all transcription happens on the edge. The app just moves audio.

## Wire contract

Targets the bridge's already-deployed ingress (see [`../../CONTRACTS.md`](../../CONTRACTS.md)):

- `wss://<edge-host>/meeting/<token>` — path-token auth (the browser can't set WS headers).
- **Binary** frames: raw little-endian PCM16 @ 16 kHz mono (`AudioRecord`, `VOICE_RECOGNITION`).
- **Text** frames: JSON control — `{"type":"marker"}` from the notification's **Mark** action.
- The bridge sends heartbeat pings; OkHttp auto-answers. On a dropped socket the app reconnects
  with backoff and resumes (audio during the gap is dropped, not buffered — buffering stale
  realtime audio would desync live diarization).
- **Model** rides as `?model=standard|enhanced` on the WS URL (the in-app toggle); the bridge
  validates + falls back to its default. The model is fixed per session — switching = stop & start.

## Behaviour notes

- **Enhanced ↔ Standard toggle** — picks the Speechmatics operating point for the *next* session
  (Enhanced ≈ higher accuracy/cost, Standard ≈ cheaper). It can't change mid-stream, so flip it,
  then Stop and Start. The choice is remembered.
- **One transcript per meeting (bridge-side VAD)** — the bridge opens a Speechmatics session on
  speech and finalizes it after a sustained silence, so a workday of capture produces one `.md` per
  meeting and only bills while someone is talking. The phone streams continuously; the segmentation
  happens on the edge. Speaker labels reset between meetings (across the silence gaps). This is
  controlled by `MEETING_VAD_THRESHOLD` on the bridge (0 = off = one file per capture).
- **14-hour runaway backstop** — a single continuous capture stops itself after 14 h (sized for a
  full out-of-house workday, not a per-meeting cap) so a forgotten stream can't run forever. Start
  again to continue. (A sticky restart resets the clock.)
- **Live status** — the label and notification refresh every ~1–2 s while capturing (elapsed + KB
  sent), so a frozen `0s · 0 KB` means the socket never actually connected, not just a stale label.

## Configuration (endpoint + token)

The endpoint and token are **your own** and are baked into the APK's `BuildConfig` at build time.
They are **never committed** — the tracked source only ships placeholders. Resolution order:

1. `app/secrets.properties` (gitignored) — copy from `app/secrets.properties.example`.
2. `-PmeetingWsUrl=… -PmeetingToken=…` Gradle properties.
3. `MEETING_WS_URL` / `MEETING_TOKEN` environment variables.
4. A harmless placeholder (so a fresh clone still builds; you then type the values in-app).

Both fields are also editable in the app UI, so an APK built with the placeholder still works —
you just paste the `wss://…/meeting/` base and the token on first launch.

## Build

Requires a JDK 17+ and the Android SDK (`platforms;android-34`, `build-tools;34.0.0`). Point the
build at your SDK with a `local.properties` (`sdk.dir=/path/to/android-sdk`) or the `ANDROID_HOME`
env var.

```bash
cp app/secrets.properties.example app/secrets.properties   # then edit in your host + token
./gradlew :app:assembleDebug
# -> app/build/outputs/apk/debug/app-debug.apk
```

The debug APK is signed with the standard Android debug key — fine for personal sideloading. (A
signed release APK is a follow-on.)

## Install (sideload)

1. Get `app-debug.apk` onto the phone (e.g. served over the tailnet).
2. Settings → allow "install unknown apps" for your browser/file manager, open the APK, install.
   Play Protect will warn about a self-built app — expected; choose install anyway.
3. Launch, grant **microphone** and **notifications** when prompted.

## ⚠️ Samsung / aggressive-OEM background survival — REQUIRED

Samsung (and Xiaomi/Oppo/OnePlus) will **stop the capture mid-meeting** when the screen locks
unless the app is whitelisted, even with the foreground notification showing. Do BOTH:

1. In the app, tap **"Allow background running"** (grants the battery-optimization exemption).
2. **Settings → Battery → Background usage limits → Sleeping apps** (a.k.a. Device Care) — make
   sure this app is **not** listed. Also turn off "Put unused apps to sleep" / "Adaptive battery"
   for it. On some One UI versions: long-press the app → App info → Battery → **Unrestricted**.

Without this, expect capture to die a few minutes after the screen turns off.

## Use

1. Confirm the `wss://…/meeting/` base + token are filled, tap **Start**.
2. Status shows `● Capturing`. Lock the screen — the notification persists and capture continues.
3. **Mark** (from the notification) drops a marker into the transcript; **Stop** ends the session.
4. On the edge, a `~/meeting-sessions/<timestamp>.md` grows live with diarized turns.

## Layout

```
clients/android-mic/
  app/src/main/
    AndroidManifest.xml                     # mic FGS type + permissions
    java/com/genesis/meetingmic/
      MainActivity.kt                       # config, Start/Stop, status, battery-exemption prompt
      MicStreamService.kt                   # foreground mic service: AudioRecord -> OkHttp WS
    res/…                                   # layout, strings, adaptive icon
  app/build.gradle.kts                      # AGP 8.5, compileSdk 34, OkHttp; BuildConfig injection
  build.gradle.kts / settings.gradle.kts    # plugin pins, module include
  gradle/wrapper/…, gradlew                 # committed Gradle 8.9 wrapper
```
