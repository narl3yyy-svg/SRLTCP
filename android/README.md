# SRLTCP Android (Chaquopy)

The Android APK wraps the same `srltcp/` Python package via [Chaquopy](https://chaquo.com/chaquopy/).

## Prerequisites

- Android Studio (Ladybug or newer)
- JDK 17
- Android SDK API 34+

## Setup

1. Open `android/` in Android Studio.
2. Ensure `settings.gradle` points `srcDir` to the repo-root `srltcp/` package.
3. Sync Gradle — Chaquopy installs Python 3.12 and pip dependencies from `pyproject.toml`.

## Build

```bash
cd android
./gradlew assembleDebug
```

APK output: `android/app/build/outputs/apk/debug/app-debug.apk`

## Runtime

The Android app starts `srltcp web --host 127.0.0.1 --serial` in a background service.
USB serial uses the Chaquopy USB host API shim (see `srltcp/utils/platform.py`).

## Permissions

- `INTERNET` — TCP transport and web UI
- `USB` host — serial over USB-OTG