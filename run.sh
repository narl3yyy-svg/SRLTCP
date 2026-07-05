#!/usr/bin/env bash
# SRLTCP launcher for Linux / macOS
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

maybe_refresh_serial_group() {
  [[ -n "${SRLTCP_SERIAL_GROUP_REFRESH:-}" ]] && return 0
  local group=""
  if getent group uucp &>/dev/null; then
    group=uucp
  elif getent group dialout &>/dev/null; then
    group=dialout
  else
    return 0
  fi
  if id -nG | grep -qw "$group"; then
    return 0
  fi
  if ! groups "$USER" 2>/dev/null | grep -qw "$group"; then
    return 0
  fi
  echo "[srltcp] Activating '$group' group for serial port access (log out/in to skip this step)..."
  export SRLTCP_SERIAL_GROUP_REFRESH=1
  exec sg "$group" -c "export SRLTCP_SERIAL_GROUP_REFRESH=1; $(printf '%q ' "$0") $(printf '%q ' "$@")"
}

maybe_refresh_serial_group "$@"

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

# Usage: ./run.sh [web] [--debug] [--port 9876]
#        ./run.sh hub --bind 0.0.0.0 --port 7825
#        ./run.sh stop   — release ports from a stale instance
if [[ "${1:-}" == "stop" ]]; then
  stop_stale_srltcp
  echo "[srltcp] Ports released (7825, 7826, 9876)."
  exit 0
fi

if [[ "${1:-}" == "web" || "${1:-}" == "hub" ]]; then
  stop_stale_srltcp
elif [[ -z "${1:-}" ]]; then
  set -- web
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