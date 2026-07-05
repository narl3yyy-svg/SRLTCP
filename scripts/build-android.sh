#!/usr/bin/env bash
# Build a debug APK locally with Gradle + Chaquopy (no GitHub required).
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

./gradlew assembleDebug renameDebugApk

APK="$ROOT/android/app/build/outputs/apk/debug/SRLTCP-"*"-debug.apk"
echo ""
echo "Build complete:"
ls -lh $APK 2>/dev/null || ls -lh "$ROOT/android/app/build/outputs/apk/debug/"*.apk
echo ""
echo "Install on a connected device:"
echo "  adb install -r $APK"