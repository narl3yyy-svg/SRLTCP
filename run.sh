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
cleanup() {
  if [[ -n "${PID:-}" ]] && kill -0 "$PID" 2>/dev/null; then
    kill -INT "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
  fi
}
trap cleanup INT TERM

python -m srltcp "$@" &
PID=$!
wait "$PID"