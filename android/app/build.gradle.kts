plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "ai.continuum.phone"
    compileSdk = 35

    defaultConfig {
        applicationId = "ai.continuum.phone"
        minSdk = 31
        targetSdk = 35
        // Bump versionCode on every build you want the phone to offer as an
        // update: Updater compares this against the installed build's code, and
        // build.sh copies it into dist/version.json for /api/app/version.
        // versionName is the human label shown in the update prompt.
        versionCode = 18
        versionName = "0.18"
    }

    buildTypes {
        debug {
            isMinifyEnabled = false
        }
        release {
            // Sideloaded and personal — the debug key is the shipping key here.
            // Kept unminified so a stack trace off the phone is readable.
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        viewBinding = false
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
    // OkHttp for the websocket: it handles the reconnect-friendly bits (ping
    // frames, TLS, redirects) that a hand-rolled socket gets wrong on mobile.
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    // Fused location is materially cheaper on battery than raw LocationManager
    // — it fuses cell/wifi/GPS and piggybacks on fixes other apps already asked
    // for, which matters a lot for something running all day.
    implementation("com.google.android.gms:play-services-location:21.3.0")
    // Health Connect — reading on-device exercise sessions (DownDog yoga, etc.).
    // Pinned to 1.1.0-alpha07: the later 1.1.x releases (rc/stable) require
    // compileSdk 36 + AGP 8.9.1, and this project is on 35 / 8.7.3. alpha07
    // predates that bump, resolves against the current toolchain, and carries
    // every API the reader uses. Bump it only alongside a compileSdk/AGP bump.
    // On Android 14+ the provider is in the OS; on 12/13 it's a separate app
    // HealthConnect.available() detects before any read.
    implementation("androidx.health.connect:connect-client:1.1.0-alpha07")
}
