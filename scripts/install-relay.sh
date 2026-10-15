#!/usr/bin/env bash
# Install (or update) the SRLTCP internet relay as a service on a Linux server.
#
#   curl -fsSLO https://raw.githubusercontent.com/narl3yyy-svg/SRLTCP/main/scripts/install-relay.sh
#   less install-relay.sh            # read it first
#   sudo bash install-relay.sh       # options: --port 443  --public-host relay.example.com
#
# Any small server with a public IPv4 address works (a 1 GB cloud VM is plenty).
# The relay only forwards sealed, padded packets between your phones: it can't
# read messages or files, see names or fake anything. It listens on TCP 443 by
# default (the standard web port, open on most networks that allow internet).
# Run it again to update. At the end it prints the relay code and a QR code:
# in the app, Settings → Network → Scan relay code (or paste it), on one phone.
set -euo pipefail

PORT=443
HOST=""
REPO="https://github.com/narl3yyy-svg/SRLTCP.git"
BASE=/var/lib/srltcp-relay
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --public-host) HOST="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    -h|--help) sed -n 2,14p "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done
[ "$(id -u)" = 0 ] || { echo "Run as root (sudo bash $0)." >&2; exit 1; }
case "$PORT" in ''|*[!0-9]*) echo "--port must be a number" >&2; exit 2 ;; esac

if [ -z "$HOST" ]; then
  # This server's own address on its default route (no outside service asked).
  HOST=$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -1)
  case "$HOST" in
    ""|10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*|100.6[4-9].*|100.[7-9][0-9].*|100.1[01][0-9].*|100.12[0-7].*)
      echo "This server's address ($HOST) isn't public (behind NAT, e.g. a cloud VM with a" >&2
      echo "separate public IP). Run again with --public-host YOUR.PUBLIC.IP (or a hostname)." >&2
      exit 1 ;;
  esac
fi

echo "== Installing packages"
if command -v apt-get >/dev/null; then
  DEBIAN_FRONTEND=noninteractive apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git python3 python3-venv qrencode >/dev/null
elif command -v dnf >/dev/null; then
  dnf install -y -q git python3 qrencode >/dev/null || dnf install -y -q git python3 >/dev/null
fi
command -v git >/dev/null && command -v python3 >/dev/null ||
  { echo "Needs git and python3 (3.10 or newer)." >&2; exit 1; }

echo "== Relay user and code"
id srltcp >/dev/null 2>&1 || useradd --system --create-home --home-dir "$BASE" --shell /usr/sbin/nologin srltcp
install -d -o srltcp -g srltcp -m 700 "$BASE" "$BASE/keys"
if [ -d "$BASE/SRLTCP/.git" ]; then
  sudo -u srltcp git -C "$BASE/SRLTCP" pull -q --ff-only
else
  sudo -u srltcp git clone -q --depth 1 "$REPO" "$BASE/SRLTCP"
fi
sudo -u srltcp python3 -m venv "$BASE/venv"
sudo -u srltcp "$BASE/venv/bin/pip" install -q --upgrade pip
sudo -u srltcp "$BASE/venv/bin/pip" install -q -e "$BASE/SRLTCP"

echo "== Service (port $PORT, address $HOST)"
UNIT=/etc/systemd/system/srltcp-relay.service
sed -e "s|--port 7827|--port $PORT|" -e "s|--public-host YOUR.SERVER.IP|--public-host $HOST|" \
  "$BASE/SRLTCP/scripts/srltcp-relay.service" > "$UNIT"
if [ "$PORT" -lt 1024 ]; then
  # Only the right to use a low port, nothing else.
  sed -i -e 's|^CapabilityBoundingSet=$|CapabilityBoundingSet=CAP_NET_BIND_SERVICE\nAmbientCapabilities=CAP_NET_BIND_SERVICE|' "$UNIT"
fi
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow "$PORT/tcp" >/dev/null
fi
systemctl daemon-reload
systemctl enable -q srltcp-relay
systemctl restart srltcp-relay

echo "== Waiting for the relay code"
CODE=""
for _ in $(seq 1 30); do
  CODE=$(journalctl -u srltcp-relay -n 50 --no-pager -o cat 2>/dev/null | grep -o 'srltcp-relay://[^ ]*' | tail -1 || true)
  [ -n "$CODE" ] && break
  sleep 1
done
[ -n "$CODE" ] || { echo "The relay didn't start: journalctl -u srltcp-relay" >&2; exit 1; }

echo
echo "SRLTCP relay is running on $HOST:$PORT (starts by itself after a reboot)."
echo "If your cloud provider has its own firewall, allow TCP $PORT there too."
echo
echo "On ONE phone: Settings → Network → Scan relay code (or paste this code)."
echo "Keep it private: it contains the relay's access key."
echo
echo "  $CODE"
echo
command -v qrencode >/dev/null && qrencode -t ANSIUTF8 -m 2 "$CODE"
