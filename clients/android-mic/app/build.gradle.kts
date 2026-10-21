import java.time.ZoneOffset
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// ── Build-time config injection ──────────────────────────────────────────────
// The capture endpoint + token are the user's own. They are NEVER committed:
// resolution order is (1) a gitignored `secrets.properties` in this module, then
// (2) a -P Gradle property, then (3) an env var, then (4) a harmless placeholder.
// The built APK embeds whatever was resolved — fine for a self-sideloaded debug
// build; the tracked source only ever contains the placeholder.
val secretsFile = rootProject.file("app/secrets.properties")
val secretProps = Properties().apply {
    if (secretsFile.exists()) secretsFile.inputStream().use { load(it) }
}
fun cfg(key: String, env: String, default: String): String =
    (secretProps.getProperty(key)
        ?: (project.findProperty(key) as String?)
        ?: System.getenv(env)
        ?: default)

// Default endpoint is a placeholder host — the real tailnet host is injected at
// build time or entered in-app. Kept as a compile-time string so a fresh clone builds.
val meetingWsUrl = cfg("meetingWsUrl", "MEETING_WS_URL", "wss://EDITME.example.ts.net/meeting/")
val meetingToken = cfg("meetingToken", "MEETING_TOKEN", "")
val buildTime: String = ZonedDateTime.now(ZoneOffset.UTC)
    .format(DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm 'UTC'"))

android {
    namespace = "com.genesis.meetingmic"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.genesis.meetingmic"
        minSdk = 26
        targetSdk = 34
        versionCode = 6
        versionName = "0.6.0"

        // Surfaced to Kotlin as BuildConfig.MEETING_WS_URL / MEETING_TOKEN.
        buildConfigField("String", "MEETING_WS_URL", "\"$meetingWsUrl\"")
        buildConfigField("String", "MEETING_TOKEN", "\"$meetingToken\"")
        // Shown beside the version so two sideloaded builds are never mistaken for each other.
        buildConfigField("String", "BUILD_TIME", "\"$buildTime\"")
    }

    buildFeatures {
        buildConfig = true
        viewBinding = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }

    buildTypes {
        release {
            // Debug-signed sideload build; no minification (keeps the build simple and
            // the OkHttp/AudioRecord paths intact). A signed release APK is a follow-on.
            isMinifyEnabled = false
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.activity:activity-ktx:1.9.1")
    // lifecycle-service gives LifecycleService; -runtime-ktx gives lifecycleScope + repeatOnLifecycle.
    implementation("androidx.lifecycle:lifecycle-service:2.8.4")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.4")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    // Persistent, reboot-surviving retry for mirroring recordings to the edge.
    implementation("androidx.work:work-runtime-ktx:2.9.1")

    testImplementation("junit:junit:4.13.2")
    // Android's org.json is a stub on the JVM; the real one lets receipt parsing be unit-tested.
    testImplementation("org.json:json:20240303")
    testImplementation("com.squareup.okhttp3:mockwebserver:4.12.0")
}
