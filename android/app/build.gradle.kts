plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

android {
    namespace = "com.srltcp.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.srltcp.app"
        minSdk = 24
        targetSdk = 34
        versionCode = 61
        versionName = "0.4.0"

        ndk {
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
        debug {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        // Must match compileOptions, otherwise builds fail on JDK 21+.
        jvmTarget = "17"
    }

    buildFeatures {
        buildConfig = true
    }
}

chaquopy {
    defaultConfig {
        version = "3.12"
        val py = System.getenv("SRLTCP_BUILD_PYTHON") ?: "python3"
        buildPython(py)
        pip {
            install("aiohttp")
            install("aiofiles")
            install("cryptography")
            install("pyserial")
            install("zstandard")
            install("tzdata")
            install("noiseprotocol")
        }
    }
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.webkit:webkit:1.11.0")
    implementation("com.google.android.material:material:1.12.0")
    // QR code scanning for in-person pairing (Apache-2.0, works offline).
    implementation("com.journeyapps:zxing-android-embedded:4.3.0")
    testImplementation("junit:junit:4.13.2")
}

tasks.register("renameDebugApk") {
    dependsOn("assembleDebug")
    doLast {
        val outDir = layout.buildDirectory.dir("outputs/apk/debug").get().asFile
        val version = android.defaultConfig.versionName
        outDir.listFiles()
            ?.filter { it.isFile && it.extension == "apk" && !it.name.startsWith("SRLTCP-") }
            ?.forEach { src ->
                val dest = File(outDir, "SRLTCP-$version.apk")
                if (dest.exists()) dest.delete()
                src.renameTo(dest)
            }
    }
}

tasks.register("renameReleaseApk") {
    dependsOn("assembleRelease")
    doLast {
        val outDir = layout.buildDirectory.dir("outputs/apk/release").get().asFile
        val version = android.defaultConfig.versionName
        outDir.listFiles()
            ?.filter { it.isFile && it.extension == "apk" && !it.name.startsWith("SRLTCP-") }
            ?.forEach { src ->
                val dest = File(outDir, "SRLTCP-$version.apk")
                if (dest.exists()) dest.delete()
                src.renameTo(dest)
            }
    }
}
