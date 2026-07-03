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
        versionCode = 4
        versionName = "0.1.4"

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
            install("aiohttp", "aiofiles", "cryptography", "pyserial", "zstandard")
        }
    }
    sourceSets {
        getByName("main") {
            srcDir("../../")
        }
    }
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
}