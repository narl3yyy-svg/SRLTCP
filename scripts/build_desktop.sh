#!/usr/bin/env bash
# Build the SRLTCP desktop apps (on Linux, macOS or Windows/WSL):
#   desktop/dist/SRLTCP-<version>-windows-x64-setup.exe   (Windows 10/11, x64)
#   desktop/dist/SRLTCP-<version>-macos-arm64.zip         (macOS 12+, Apple silicon)
#
#   bash scripts/build_desktop.sh            # both
#   bash scripts/build_desktop.sh win        # only Windows
#   bash scripts/build_desktop.sh mac        # only macOS
#
# Needs: Node.js 20+, Python 3.12, and for the Windows installer on Linux/macOS
# Wine (64- and 32-bit: wine64 + wine32). The Mac app is ad-hoc signed with
# Apple's codesign on a Mac, or rcodesign (https://github.com/indygreg/apple-platform-rs)
# anywhere else; without either it isn't signed and macOS won't open it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/desktop"
WHAT="${1:-all}"
VERSION="$(python3 -c 'import re,sys; print(re.search(r"__version__ = \"([^\"]+)\"", open(sys.argv[1]).read()).group(1))' "$ROOT/srltcp/__init__.py")"
PY="${PYTHON:-python3.12}"

# The app's version follows SRLTCP's.
node -e '
  const fs = require("fs"); const p = JSON.parse(fs.readFileSync("package.json"));
  p.version = process.argv[1]; fs.writeFileSync("package.json", JSON.stringify(p, null, 2) + "\n");
' "$VERSION"

[[ -d node_modules ]] || npm ci --no-audit --no-fund

if [[ "$WHAT" == "all" || "$WHAT" == "win" ]]; then
  "$PY" scripts/prepare_backend.py win-x64
  WINEDEBUG=-all npx electron-builder --win --x64
  echo "[desktop] dist/SRLTCP-$VERSION-windows-x64-setup.exe"
fi

if [[ "$WHAT" == "all" || "$WHAT" == "mac" ]]; then
  "$PY" scripts/prepare_backend.py mac-arm64
  npx electron-builder --mac --arm64
  APP="dist/mac-arm64/SRLTCP.app"
  if [[ "$(uname -s)" == "Darwin" ]]; then
    codesign --force --deep --sign - "$APP"
  elif command -v rcodesign >/dev/null; then
    rcodesign sign "$APP" >/dev/null
  else
    echo "[desktop] WARNING: no codesign / rcodesign — the Mac app is NOT signed and won't open." >&2
  fi
  ZIP="dist/SRLTCP-$VERSION-macos-arm64.zip"
  rm -f "$ZIP"
  (cd dist/mac-arm64 && zip -qry "../$(basename "$ZIP")" SRLTCP.app)  # (-y: keep the frameworks' symlinks)
  echo "[desktop] $ZIP"
fi
