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
2. `MainActivity` requests `POST_NOTIFICATIONS` on Android 13+ before starting the foreground service.
3. `SRLTCPService` sets `set_android_data_dir(filesDir)`, starts `srltcp web`, and keeps the node alive.
4. `WebView` loads `https://127.0.0.1:<port>/` with self-signed localhost TLS accepted.

### Startup timeline

- Foreground service starts the Python server on a background thread.
- WebView load begins once `is_android_server_ready()` is true (~1–5s).
- Ports 9876–9878 are tried automatically if the default port is busy.

## USB Serial on Android

USB host serial requires:

- USB-OTG capable device
- Runtime USB permission when a device is attached
- `pyserial` via Chaquopy (see `srltcp/transports/serial.py`)

If no USB device is present, TCP-only mode still works over Wi‑Fi.

## Permissions

- `INTERNET` — TCP transport and web UI
- `POST_NOTIFICATIONS` — foreground service notification (Android 13+)
- `FOREGROUND_SERVICE` / `FOREGROUND_SERVICE_DATA_SYNC` — keep node alive in background
- USB host — serial over USB-OTG

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| App closes immediately on open | Run `adb logcat -s SRLTCP SRLTCPService`. Ensure `srltcp/` was rsync'd before build. Grant notification permission when prompted (Android 13+). |
| `Python runtime not initialized` | Reinstall APK; verify Chaquopy Gradle sync completed without errors. |
| White screen / cannot reach UI | Server may be on 9877/9878; wait up to 60s. Check logcat for `Server ready on port`. |
| Foreground service failed | App falls back to direct server start; check battery optimization is off for SRLTCP. |
| No identities after reinstall | Data lives in app files dir; uninstall clears it. |
| Serial not detected | USB-OTG adapter + cable; enable USB host in device settings if applicable. |
| WebView SSL errors | Only `127.0.0.1` / `localhost` self-signed certs are accepted by design. |

## Debug

```bash
adb logcat -s SRLTCP SRLTCPService
adb shell am start -n com.srltcp.app/.MainActivity
```

Enable WebView debugging in debug builds (`WebView.setWebContentsDebuggingEnabled`).