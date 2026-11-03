#!/usr/bin/env bash
# SRLTCP launcher for macOS (Apple silicon and Intel) and Linux.
#
#   ./run.sh web          start SRLTCP (the first time, offers to download Tor)
#   ./run.sh tor-setup    download Tor now (Tor Expert Bundle from torproject.org)
#   ./run.sh stop         release ports from a stale instance
#
# On a Mac with Apple silicon (M1 and later) everything runs natively as
# arm64: a native Python is chosen, a virtual environment made by an Intel
# (Rosetta) Python is rebuilt, and the Apple-silicon build of Tor is used.
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
  if ! groups "${USER:-$(id -un)}" 2>/dev/null | grep -qw "$group"; then
    return 0
  fi
  echo "[srltcp] Activating '$group' group for serial port access (log out/in to skip this step)..."
  export SRLTCP_SERIAL_GROUP_REFRESH=1
  exec sg "$group" -c "export SRLTCP_SERIAL_GROUP_REFRESH=1; $(printf '%q ' "$0") $(printf '%q ' "$@")"
}

OS="$(uname -s)"
APPLE_SILICON=0
if [[ "$OS" == "Darwin" ]] && [[ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" == "1" ]]; then
  APPLE_SILICON=1
fi

# Run a Python natively (arm64 on Apple silicon, even from a Rosetta Terminal).
native() {
  if [[ "$APPLE_SILICON" == "1" ]]; then
    arch -arm64 "$@"
  else
    "$@"
  fi
}

# A Python 3.12 or newer (macOS's own python3 is older), native on Apple silicon.
find_python() {
  local c
  for c in python3.13 python3.12 /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
           /opt/homebrew/bin/python3 /usr/local/bin/python3.13 /usr/local/bin/python3.12 \
           /Library/Frameworks/Python.framework/Versions/Current/bin/python3 python3; do
    command -v "$c" &>/dev/null || continue
    native "$c" -c 'import sys; sys.exit(sys.version_info < (3, 12))' &>/dev/null || continue
    command -v "$c"
    return 0
  done
  return 1
}

maybe_refresh_serial_group "$@"

if [[ "$APPLE_SILICON" == "1" && -x .venv/bin/python ]] &&
   [[ "$(native .venv/bin/python -c 'import platform; print(platform.machine())' 2>/dev/null)" != "arm64" ]]; then
  echo "[srltcp] The virtual environment was made by an Intel (Rosetta) Python — rebuilding it for Apple silicon..."
  rm -rf .venv
fi

if [[ ! -f .venv/bin/activate ]]; then
  if ! PY="$(find_python)"; then
    if [[ "$OS" == "Darwin" ]]; then
      echo "[srltcp] SRLTCP needs Python 3.12 or newer$([[ "$APPLE_SILICON" == "1" ]] && echo " for Apple silicon (arm64)")."
      echo "         Install it from https://www.python.org/downloads/macos/ or with Homebrew: brew install python@3.12"
    else
      echo "[srltcp] SRLTCP needs Python 3.12 or newer (e.g. sudo apt install python3.12 python3.12-venv)."
    fi
    exit 1
  fi
  echo "[srltcp] Creating virtual environment ($PY)..."
  rm -rf .venv
  native "$PY" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

native python -m pip install -q -e ".[dev]" 2>/dev/null || native python -m pip install -q -e .

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
#        ./run.sh stop        — release ports from a stale instance
#        ./run.sh tor-setup   — download Tor (Tor Expert Bundle for this computer)
if [[ "${1:-}" == "tor-setup" ]]; then
  native python -m srltcp.utils.tor_setup --yes
  exit $?
fi

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

# Tor (to find contacts on other networks): offer to download it once if missing.
if [[ "${1:-}" == "web" ]]; then
  native python -m srltcp.utils.tor_setup || true
fi

cleanup() {
  if [[ -n "${PID:-}" ]] && kill -0 "$PID" 2>/dev/null; then
    kill -INT "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
  fi
}
trap cleanup INT TERM

native python -m srltcp "$@" &
PID=$!
wait "$PID"