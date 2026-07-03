plugins {
    id("com.android.application")
    id("com.chaquo.python")
}

android {
    namespace = "com.srltcp.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.srltcp.app"
        minSdk = 24
        targetSdk = 34
        versionCode = 8
        versionName = "0.1.8"

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
}

chaquopy {
    defaultConfig {
        version = "3.12"
        buildPython("python3")
        pip {
            install("aiohttp")
            install("aiofiles")
            install("cryptography")
            install("pyserial")
            install("zstandard")
        }
    }
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.webkit:webkit:1.11.0")
    implementation("com.google.android.material:material:1.12.0")
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