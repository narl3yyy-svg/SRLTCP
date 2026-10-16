pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
    plugins {
        id("com.android.application") version "8.5.2"
        id("com.chaquo.python") version "15.0.1"
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
        // Only for the USB serial driver library (not published elsewhere).
        maven("https://jitpack.io") {
            content { includeGroup("com.github.mik3y") }
        }
    }
}
rootProject.name = "SRLTCP"
include(":app")