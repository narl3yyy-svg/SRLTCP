# SRLTCP Android (python-for-android)

The Android app is built with **[python-for-android](https://python-for-android.readthedocs.io/)** via **Buildozer**. Chaquopy is no longer used.

## Architecture

| Component | Role |
|-----------|------|
| `service/srltcp_service.py` | Foreground Python service — runs `srltcp web` |
| `src/main/java/.../MainActivity.java` | WebView shell — loads `https://127.0.0.1:9876` |
| `org.kivy.android.PythonService` | P4A foreground service host (notification + process) |

Serial/USB transport is disabled on Android. TCP and the full Web UI work over localhost.

## CI builds

Pushing to `main` or tagging `v*` triggers [.github/workflows/build-apk.yml](../.github/workflows/build-apk.yml). Release tags upload `SRLTCP-<version>.apk` to GitHub Releases.

## Local build (Linux)

### Prerequisites

```bash
sudo apt install -y git zip unzip openjdk-17-jdk autoconf libtool pkg-config \
  zlib1g-dev libncurses-dev libncurses5-dev libncursesw5-dev libtinfo5 \
  cmake libffi-dev libssl-dev
pip install buildozer cython
```

### Build debug APK

```bash
cd android
buildozer android debug
```

Output: `android/bin/srltcp-0.1.20-debug.apk` (name may vary by version).

### Install on device

```bash
adb install -r bin/*debug*.apk
```

Open the app — it starts the Python service, then loads the Web UI in WebView.

## Troubleshooting

- **First build is slow** — Buildozer downloads the Android SDK/NDK and builds Python wheels.
- **Web UI blank** — wait a few seconds; the service binds HTTPS on 9876 (or next free port).
- **Logs** — `adb logcat -s SRLTCP python:D PythonService:D`

## Limitations

- No USB serial on Android in this build
- Self-signed localhost HTTPS requires WebView SSL bypass (implemented for 127.0.0.1 only)
- Foreground service notification is required on Android 8+