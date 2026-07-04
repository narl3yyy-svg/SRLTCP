# SRLTCP Android (python-for-android)

The Android app is built with **[python-for-android](https://python-for-android.readthedocs.io/)** via **Buildozer**. Chaquopy is no longer used.

## Architecture

| Component | Role |
|-----------|------|
| `service/srltcp_service.py` | Foreground Python service — runs `srltcp web` |
| `src/main/java/.../MainActivity.java` | WebView shell — loads `https://127.0.0.1:9876` |
| `org.kivy.android.PythonService` | P4A foreground service host (notification + process) |

Serial/USB transport is disabled on Android. TCP and the full Web UI work over localhost.

## Build requirements

| Tool | Version |
|------|---------|
| Host Python | **3.12.x** (CI uses 3.12.8) |
| Android Python (P4A) | **3.12.8** (pinned in `buildozer.spec`) |
| Cython | 0.29.34 |
| Java JDK | 17 |
| NDK | 26b (via Buildozer) |
| API | 35 / Android 15 (min 24) |
| Arch | arm64-v8a |

**Important:** python-for-android `master` currently defaults to Python 3.14, which breaks Cython extensions such as `aiohttp._websocket`. This project pins `python3==3.12.8` and `hostpython3==3.12.8` in `buildozer.spec`. Do not remove those pins until aiohttp supports Python 3.14 on Android.

Pinned app deps for Android: `aiohttp==3.10.11`, `cryptography`, `pyopenssl`, `zstandard`.

## CI builds

Pushing to `main` or tagging `v*` triggers [.github/workflows/build-apk.yml](../.github/workflows/build-apk.yml). Release tags upload `SRLTCP-<version>.apk` to GitHub Releases.

The workflow:
- Uses Python 3.12.8 on the runner
- Installs Buildozer + python-for-android from the `master` branch
- Bootstraps API 35 / build-tools 35.0.0 via the runner `sdkmanager` (no `buildozer android update` pre-build)
- Builds **arm64-v8a** only (avoids armeabi-v7a Python `grp` compile failures)
- Clears stale P4A Python 3.14 / multi-arch build caches before compiling
- **`android.archs` must be in `[app]`** — buildozer ignores a separate `[android]` section (defaults to dual-arch otherwise)
- **SDK/NDK** — do not use `%(ENV_…)` in spec; CI symlinks the runner SDK to `~/.buildozer/android/platform/android-sdk` and NDK to `android-ndk-r26b`
- Uploads `buildozer.log` if the build fails

## Local build (Linux)

### Prerequisites (Ubuntu 24.04)

```bash
sudo apt install -y build-essential git zip unzip openjdk-17-jdk autoconf automake \
  libtool pkg-config zlib1g-dev libncurses-dev libtinfo6 cmake libffi-dev libssl-dev \
  libltdl-dev gettext autopoint
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
pip install "buildozer>=1.5.0" "cython==0.29.34" setuptools
pip install "git+https://github.com/kivy/python-for-android@master"
```

Use **Python 3.12** for Buildozer (not 3.14):

```bash
python3.12 --version   # should be 3.12.x
```

### Build debug APK

```bash
cd android
# If you previously built with Python 3.14, clear the cache first:
rm -rf ~/.buildozer/android/platform/build-python3 .buildozer/android/platform/build-python3
buildozer android debug
```

Output: `android/bin/srltcp-<version>-debug.apk`.

### Install on device

```bash
adb install -r bin/*debug*.apk
```

Open the app — it starts the Python service, then loads the Web UI in WebView.

## Troubleshooting

- **aiohttp / `_websocket.c` compile errors** — you are building against Python 3.14. Confirm `buildozer.spec` still has `python3==3.12.8` and clear `~/.buildozer/android/platform/build-python3`.
- **First build is slow** — Buildozer downloads the Android SDK/NDK and builds Python wheels.
- **Web UI blank** — wait a few seconds; the service binds HTTPS on 9876 (or next free port).
- **Logs** — `adb logcat -s SRLTCP python:D PythonService:D`

## Limitations

- No USB serial on Android in this build
- Self-signed localhost HTTPS requires WebView SSL bypass (implemented for 127.0.0.1 only)
- Foreground service notification is required on Android 8+
