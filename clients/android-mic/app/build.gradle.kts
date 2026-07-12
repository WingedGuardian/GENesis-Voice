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

android {
    namespace = "com.genesis.meetingmic"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.genesis.meetingmic"
        minSdk = 26
        targetSdk = 34
        versionCode = 2
        versionName = "0.2.0"

        // Surfaced to Kotlin as BuildConfig.MEETING_WS_URL / MEETING_TOKEN.
        buildConfigField("String", "MEETING_WS_URL", "\"$meetingWsUrl\"")
        buildConfigField("String", "MEETING_TOKEN", "\"$meetingToken\"")
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

    testImplementation("junit:junit:4.13.2")
}
