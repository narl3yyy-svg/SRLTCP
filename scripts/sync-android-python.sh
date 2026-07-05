#!/usr/bin/env bash
# Copy the srltcp Python package into the Chaquopy source tree before Gradle builds.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$ROOT/android/app/src/main/python/srltcp"

mkdir -p "$(dirname "$DEST")"
rm -rf "$DEST"

if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.mypy_cache' \
    --exclude '.ruff_cache' \
    "$ROOT/srltcp/" "$DEST/"
else
  cp -a "$ROOT/srltcp" "$DEST"
  find "$DEST" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
  find "$DEST" -name '*.pyc' -delete 2>/dev/null || true
fi

echo "[srltcp] Synced Python package to android/app/src/main/python/srltcp/"