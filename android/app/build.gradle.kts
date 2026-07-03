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
        versionCode = 2
        versionName = "0.1.1"

        ndk {
            abiFilters += listOf("arm64-v8a", "x86_64")
        }

        python {
            version = "3.12"
            pip {
                install("aiohttp", "aiofiles", "cryptography", "pyserial", "zstandard")
            }
            srcDir("../../")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
}