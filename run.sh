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

stop_stale_srltcp() {
  local pids
  pids=$(pgrep -f '[p]ython -m srltcp' 2>/dev/null || true)
  if [[ -z "$pids" ]]; then
    return 0
  fi
  echo "[srltcp] Stopping previous instance(s): $pids"
  kill -INT $pids 2>/dev/null || true
  local i
  for i in 1 2 3 4 5 6 7 8; do
    if ! pgrep -f '[p]ython -m srltcp' >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  kill -TERM $pids 2>/dev/null || true
  sleep 0.5
  kill -KILL $pids 2>/dev/null || true
}

# Usage: ./run.sh web [--debug] [--port 9876]
#        ./run.sh stop   — release ports from a stale instance
if [[ "${1:-}" == "stop" ]]; then
  stop_stale_srltcp
  echo "[srltcp] Ports released (7825, 7826, 9876)."
  exit 0
fi

if [[ "${1:-}" == "web" ]]; then
  stop_stale_srltcp
fi

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