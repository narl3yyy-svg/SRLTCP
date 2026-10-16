# SRLTCP Android (Chaquopy + Gradle)

The Android app embeds the same `srltcp/` Python package using [Chaquopy](https://chaquo.com/chaquopy/) and builds with **Gradle** (`./gradlew`). No Buildozer or python-for-android is required.

## Prerequisites

| Tool | Version |
|------|---------|
| **JDK** | 17 (recommended; set `JAVA_HOME`) |
| **Android SDK** | API 34 platform + build-tools 34.x |
| **Python** | 3.12 on your build machine (Chaquopy uses it to compile the embedded runtime) |

### Install Android SDK (Linux)

1. Install [Android Studio](https://developer.android.com/studio) **or** command-line tools only.
2. In Android Studio: **Settings → Languages & Frameworks → Android SDK**
   - SDK Platforms: **Android 14 (API 34)**
   - SDK Tools: **Android SDK Build-Tools 34**, **Platform-Tools**
3. Export the SDK path:

```bash
export ANDROID_HOME="$HOME/Android/Sdk"
export PATH="$PATH:$ANDROID_HOME/platform-tools"
```

4. Accept licenses (once):

```bash
yes | sdkmanager --licenses
```

## Build APK locally (recommended)

From the **repository root**:

```bash
# One-shot build (syncs Python + runs Gradle)
bash scripts/build-android.sh
```

Output:

```
android/app/build/outputs/apk/debug/SRLTCP-0.5.0.apk
```

### Manual steps

```bash
# 1. Copy Python sources into the Android project
bash scripts/sync-android-python.sh

# 2. Point Gradle at your SDK (auto-created by build-android.sh if ANDROID_HOME is set)
echo "sdk.dir=$ANDROID_HOME" > android/local.properties

# 3. Build
cd android
./gradlew assembleDebug renameDebugApk
```

### Install on a phone

Enable **USB debugging**, connect the device, then:

```bash
adb install -r android/app/build/outputs/apk/debug/SRLTCP-*.apk
```

## Build with Android Studio

1. Run `bash scripts/sync-android-python.sh` whenever you change Python code.
2. Open the `android/` folder in Android Studio.
3. **File → Sync Project with Gradle Files**
4. **Build → Build Bundle(s) / APK(s) → Build APK(s)**

## Runtime architecture

1. `SRLTCPApplication` starts the Chaquopy Python runtime.
2. `MainActivity` starts `srltcp web` on a background thread.
3. A `WebView` loads `https://127.0.0.1:<port>/?token=…` (self-signed localhost TLS, certificate pinned; the token keeps other apps out of the local API).
4. `SRLTCPService` keeps the node alive in the background (foreground notification).

Startup usually takes **5–30 seconds** on first launch while Python initializes.

## Mobile UI (phone layout)

The app reuses the same web UI as desktop, but `MainActivity` enables a **mobile layout** in the WebView:

| Control | Action |
|---------|--------|
| **☰** (top-left) | Open/close the contacts sidebar (peers, announce, add contact) |
| **⚙** (top-right) | Open Settings (full-screen on phone) |
| **←** (in chat header) | Back to contacts sidebar |
| Tap outside sidebar | Close the slide-out panel |

On first launch the sidebar opens automatically so you can reach peers and settings immediately. Settings tabs, folder pickers, and other modals use the full screen height on Android.

Incoming and shared files default to `Downloads/SRLTCP/` when storage access is granted (see storage permission prompt on first launch).

## Pairing, NFC and calls

- **Pair** (sidebar) shows your QR code; *Scan their QR code* opens the camera scanner.
- **NFC** — open *Pair* on **both** phones and hold them back to back (NFC on, phones unlocked). While the Pair screen is open each phone alternates every ~0.5–1.1 s between reading (reader mode) and answering as a contactless card (Host Card Emulation, AID `F053524C54435001`), so the phones line up within a second or two and swap contact cards; a short vibration confirms it. If NFC is off, the screen offers a button to open NFC settings. NFC only answers while the Pair screen is open and the phone is unlocked. The APDU protocol lives in `PairingProtocol.kt` and is covered by JVM unit tests (`./gradlew testDebugUnitTest`).
- **Calls** need the microphone (and camera for video) permission; Android asks the first time.
- The app holds a Wi-Fi multicast lock while running so LAN discovery broadcasts are not dropped.
- Network interfaces (including the WireGuard `tun0`) are read through Java `NetworkInterface`, because Android does not let apps read `/sys/class/net`.
- The node keeps running after the UI is closed (foreground service, notifications posted from Python when no UI is open). **Quit** on the notification stops it.

## Internet / network on Android

- **Other networks:** phones connect directly (UDP over IPv4 and IPv6), finding each other through the built-in Tor; no relays or servers. See `docs/NETWORKING.md`.
- The foreground service keeps the node running so messages arrive with the app closed. Swipe the notification away (Android 14+) or tap **Quit** to stop everything.
- Serial transport is disabled on Android (`srltcp/app.py` sets `enable_serial=False`).

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `SDK location not found` | Set `ANDROID_HOME` or create `android/local.properties` with `sdk.dir=...` |
| `Python version not found` | Install Python 3.12; or `export SRLTCP_BUILD_PYTHON=/usr/bin/python3.12` before building |
| Gradle / Java errors | Use **JDK 17** (`export JAVA_HOME=/usr/lib/jvm/java-17-openjdk`) |
| App shows "Python failed to start" | Re-run `sync-android-python.sh` and rebuild |
| White screen | Wait 30s; check `adb logcat -s SRLTCP SRLTCPService python` |
| Sidebar / settings not visible | Tap **☰** or **⚙** in the top bar — desktop sidebar is hidden off-screen on phones |
| Chaquopy pip failures | Ensure network access during first Gradle build (downloads wheels) |

## Debug logcat

```bash
adb logcat -s SRLTCP SRLTCPService python:D
adb shell am start -n com.srltcp.app/.MainActivity
```

## Release build

```bash
bash scripts/sync-android-python.sh
cd android
./gradlew assembleRelease renameReleaseApk
```

Sign the release APK with your own keystore before publishing (not included in this repo).