#!/usr/bin/env bash
# Start a desktop app's bundled Tor the way SRLTCP does, and check it comes up
# (writes its control port file) instead of exiting at start. Used by the
# macOS, Windows and Linux build workflows; prints Tor's own output on failure.
#
#   bash scripts/check_bundled_tor.sh path/to/tor[.exe]
set -uo pipefail
TOR="$1"
WORK="$(mktemp -d)"
DATA="$WORK/data"
mkdir -p "$DATA"
chmod 700 "$DATA" 2>/dev/null || true
native() {  # Tor on Windows wants Windows paths
  if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi
}
# On Linux the Tor Expert Bundle keeps tor's own libraries (libevent,
# OpenSSL) next to it without a run path; SRLTCP points LD_LIBRARY_PATH there.
if [ "$(uname -s)" = Linux ] && ls "$(dirname "$TOR")"/libevent* >/dev/null 2>&1; then
  export LD_LIBRARY_PATH="$(cd "$(dirname "$TOR")" && pwd)${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
cat > "$WORK/torrc" <<EOF
DataDirectory $(native "$DATA")
SocksPort 127.0.0.1:auto
ControlPort 127.0.0.1:auto
ControlPortWriteToFile $(native "$WORK/control.port")
CookieAuthentication 1
CookieAuthFile $(native "$WORK/cookie")
ClientOnly 1
AvoidDiskWrites 1
Log notice stdout
EOF

echo "== $TOR --version"
"$TOR" --version 2>&1 | head -3 || true

"$TOR" -f "$(native "$WORK/torrc")" --ignore-missing-torrc > "$WORK/tor.log" 2>&1 &
PID=$!
ok=0
PORT=""
for _ in $(seq 1 100); do  # up to 20 s
  if [ -s "$WORK/control.port" ]; then ok=1; PORT="$(cat "$WORK/control.port")"; break; fi
  if ! kill -0 "$PID" 2>/dev/null; then break; fi
  sleep 0.2
done
kill "$PID" 2>/dev/null
wait "$PID" 2>/dev/null
echo "== tor output"
tail -n 25 "$WORK/tor.log"
if [ "$ok" = 1 ]; then
  echo "bundled Tor started: $PORT"  # (Tor removes the file again when it stops)
  exit 0
fi
echo "::error::The bundled Tor did not start (it exited or never opened its control port)."
if [ "$(uname -s)" = Darwin ]; then
  echo "== diagnostics (macOS)"
  DIR="$(dirname "$TOR")"
  ls -la "$DIR"
  otool -L "$TOR" || true
  for f in "$TOR" "$DIR"/*.dylib; do
    [ -e "$f" ] || continue
    echo "-- codesign $f"
    codesign -dv "$f" 2>&1 | grep -E "Identifier|TeamIdentifier|Signature|flags|Runtime" || true
  done
fi
exit 1
