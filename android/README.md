# SRLTCP Android (Chaquopy)

The Android APK wraps the same `srltcp/` Python package via [Chaquopy](https://chaquo.com/chaquopy/).

## Prerequisites

- Android Studio (Ladybug or newer)
- JDK 17
- Android SDK API 34+

## Setup

1. Open `android/` in Android Studio.
2. Stage the Python package into Chaquopy's default source dir:

```bash
mkdir -p android/app/src/main/python
rsync -a --delete srltcp/ android/app/src/main/python/srltcp/
```

3. Sync Gradle — Chaquopy installs Python 3.12 and pip dependencies.

## Build

```bash
mkdir -p android/app/src/main/python
rsync -a --delete srltcp/ android/app/src/main/python/srltcp/
cd android
./gradlew assembleDebug
```

APK output: `android/app/build/outputs/apk/debug/app-debug.apk`

CI builds tag releases as `SRLTCP-<version>.apk` on GitHub Actions.

## Runtime

1. `SRLTCPApplication` starts the Chaquopy Python runtime on app launch.
2. `MainActivity` calls `set_android_data_dir(filesDir)` so identities, settings, and transfers persist under the app private files directory.
3. A background thread runs `srltcp web --debug` (HTTPS on localhost, serial enabled when configured in settings).
4. `WebView` loads `https://127.0.0.1:<port>/` with self-signed localhost TLS accepted.

### Startup timeline

- Python server thread starts immediately after data dir is set.
- WebView load is delayed ~3s, then retries ports 9876–9878.
- Loading screen shows until the page finishes or a fatal error is displayed.

## USB Serial on Android

USB host serial requires:

- USB-OTG capable device
- Runtime USB permission when a device is attached
- `pyserial` via Chaquopy (see `srltcp/transports/serial.py`)

If no USB device is present, TCP-only mode still works over Wi‑Fi.

## Permissions

- `INTERNET` — TCP transport and web UI
- `USB` host — serial over USB-OTG

## Troubleshooting

| Symptom | Check |
|---------|--------|
| White screen / crash on open | Logcat tag `SRLTCP`; ensure `srltcp/` was rsync'd before build |
| Cannot reach web UI | Server may be on 9877/9878 if 9876 is busy; app retries automatically |
| No identities after reinstall | Data lives in app files dir; uninstall clears it |

## Debug

```bash
adb logcat -s SRLTCP
```

Enable WebView debugging in debug builds (`WebView.setWebContentsDebuggingEnabled`).