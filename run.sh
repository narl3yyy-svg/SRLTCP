#!/usr/bin/env bash
# SRLTCP launcher for Linux / macOS
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [[ ! -f .venv/bin/activate ]]; then
  echo "[srltcp] Creating virtual environment..."
  rm -rf .venv
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

pip install -q -e ".[dev]" 2>/dev/null || pip install -q -e .

# Usage: ./run.sh web [--debug] [--port 9876]
exec python -m srltcp "$@"