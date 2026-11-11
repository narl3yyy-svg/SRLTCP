#!/usr/bin/env bash
# Build the APK locally with Gradle + Chaquopy.
#
#   SRLTCP_KEYSTORE=/path/srltcp-release.jks SRLTCP_KEYSTORE_PASSWORD=... bash scripts/build-android.sh
#       -> release APK signed with the SRLTCP release key (installs as an update)
#   bash scripts/build-android.sh debug
#       -> debug APK (development only: phones with a release build can't update to it)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v java >/dev/null 2>&1; then
  echo "Java JDK 17+ is required. Install OpenJDK 17 and set JAVA_HOME." >&2
  exit 1
fi

if [[ -z "${ANDROID_HOME:-}" && -z "${ANDROID_SDK_ROOT:-}" ]]; then
  echo "ANDROID_HOME or ANDROID_SDK_ROOT must point to your Android SDK." >&2
  echo "Example: export ANDROID_HOME=\$HOME/Android/Sdk" >&2
  exit 1
fi

SDK="${ANDROID_HOME:-$ANDROID_SDK_ROOT}"
LOCAL_PROPS="$ROOT/android/local.properties"
if [[ ! -f "$LOCAL_PROPS" ]]; then
  printf 'sdk.dir=%s\n' "$SDK" > "$LOCAL_PROPS"
  echo "[srltcp] Wrote android/local.properties"
fi

"$ROOT/scripts/sync-android-python.sh"

cd "$ROOT/android"
if [[ ! -x ./gradlew ]]; then
  echo "Missing android/gradlew — run from a full git checkout." >&2
  exit 1
fi

if [[ "${1:-release}" == "debug" ]]; then
  ./gradlew assembleDebug renameDebugApk
  APK=$(ls "$ROOT"/android/app/build/outputs/apk/debug/SRLTCP-*.apk 2>/dev/null | head -1)
else
  if [[ -z "${SRLTCP_KEYSTORE:-}" || ! -f "$SRLTCP_KEYSTORE" || -z "${SRLTCP_KEYSTORE_PASSWORD:-}" ]]; then
    echo "Release APKs must be signed with the SRLTCP release key: set SRLTCP_KEYSTORE" >&2
    echo "(the .jks file) and SRLTCP_KEYSTORE_PASSWORD. For a development build: $0 debug" >&2
    exit 1
  fi
  ./gradlew assembleRelease renameReleaseApk
  APK=$(ls "$ROOT"/android/app/build/outputs/apk/release/SRLTCP-*.apk 2>/dev/null | head -1)
fi
echo ""
echo "Build complete:"
ls -lh "${APK:-$ROOT/android/app/build/outputs/apk/debug/}"
echo ""
echo "Install on a connected device:"
echo "  adb install -r $APK"