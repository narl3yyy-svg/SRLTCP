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
        versionCode = 130
        versionName = "0.17.5"

        ndk {
            // One APK for phones (arm64). For an emulator or Chromebook add
            // x86_64: -PsrltcpAbis=arm64-v8a,x86_64
            val abis = (project.findProperty("srltcpAbis") as String?)
                ?.split(",")?.map { it.trim() }?.filter { it.isNotEmpty() }
                ?: listOf("arm64-v8a")
            abiFilters += abis
        }
    }

    // Release key: every APK since 0.17.1 is signed with the same key, so
    // a new version installs over the old one (Android refuses an update
    // signed with another key). The key never goes into the repository: it
    // comes from the environment (a GitHub Actions secret, or a local file).
    //   SRLTCP_KEYSTORE           path to the .jks file
    //   SRLTCP_KEYSTORE_PASSWORD  its password (store and key)
    //   SRLTCP_KEY_ALIAS          key alias (default "srltcp")
    val releaseKeystore = System.getenv("SRLTCP_KEYSTORE")?.takeIf { it.isNotBlank() }?.let { file(it) }
    signingConfigs {
        if (releaseKeystore != null) {
            create("srltcp") {
                storeFile = releaseKeystore
                storePassword = System.getenv("SRLTCP_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("SRLTCP_KEY_ALIAS") ?: "srltcp"
                keyPassword = System.getenv("SRLTCP_KEYSTORE_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            if (releaseKeystore != null) {
                signingConfig = signingConfigs.getByName("srltcp")
            }
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
            install("cryptography")
            install("kyber-py")  // post-quantum ML-KEM (pure Python)
            install("dilithium-py")  // post-quantum ML-DSA (pure Python)
            install("pyserial")
            install("zstandard")
            install("tzdata")
        }
    }
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.webkit:webkit:1.11.0")
    // App lock: fingerprint/face or the screen lock (Settings → Privacy).
    implementation("androidx.biometric:biometric:1.1.0")
    implementation("com.google.android.material:material:1.12.0")
    // QR code scanning for in-person pairing (Apache-2.0, works offline).
    implementation("com.journeyapps:zxing-android-embedded:4.3.0")
    // Tor (onion services) to reach trusted contacts on any network without a
    // server or port forwarding. Pure Java; its Kotlin stdlib dependency isn't
    // used and would clash with this project's Kotlin version. 0.4.9.5 is the
    // newest build this toolchain (AGP 8.5, compileSdk 34) can package; later
    // ones need compileSdk 37 and Java 24 bytecode.
    implementation("info.guardianproject:tor-android:0.4.9.5") {
        exclude(group = "org.jetbrains.kotlin")
    }
    // USB serial adapters (FTDI, CP210x, CH34x, PL2303, CDC-ACM) through the
    // Android USB host API: apps can't open /dev/ttyUSB* on Android (MIT).
    implementation("com.github.mik3y:usb-serial-for-android:3.8.1")
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
    doFirst {
        check(System.getenv("SRLTCP_KEYSTORE")?.isNotBlank() == true) {
            "Set SRLTCP_KEYSTORE (and SRLTCP_KEYSTORE_PASSWORD): release APKs must be signed " +
                "with the SRLTCP release key, or phones can't update to them."
        }
    }
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


// The app's Python code lives in /srltcp; copy it in on every build so an APK
// never ships older code than its version name says.
val syncPython by tasks.registering(Sync::class) {
    from(rootProject.file("../srltcp")) {
        exclude("**/__pycache__/**", "**/*.pyc", "**/.mypy_cache/**", "**/.ruff_cache/**")
    }
    into(file("src/main/python/srltcp"))
    doLast {
        val init = file("src/main/python/srltcp/__init__.py").readText()
        val code = Regex("__version__ = \"([^\"]+)\"").find(init)?.groupValues?.get(1)
        val name = android.defaultConfig.versionName
        check(code == name) { "srltcp/__init__.py is $code but versionName is $name" }
    }
}
tasks.named("preBuild") { dependsOn(syncPython) }
tasks.matching { it.name.startsWith("merge") && it.name.endsWith("PythonSources") }
    .configureEach { dependsOn(syncPython) }
