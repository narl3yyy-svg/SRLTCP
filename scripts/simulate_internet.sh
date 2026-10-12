#!/usr/bin/env bash
# Internet test: two phones that can only reach each other over WireGuard,
# plus an SRLTCP relay on the server's public address. Checks that once paired
# they keep talking when the VPN is switched off (via the relay), move back to
# a direct path when it is switched on, make a video call over the relay, and
# reconnect over the relay after an app restart with no VPN at all.
#
#   phone1 --veth-- server (WireGuard 10.0.100.1, relay 203.0.113.1:7827) --veth-- phone2
#   (phones have no route to each other except through the VPN)
#
# Needs root (network namespaces), iproute2, wireguard-tools, wireguard-go or
# the kernel module, and for the call: playwright + Chromium.
# Usage: sudo bash scripts/simulate_internet.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
W="$(mktemp -d /tmp/srltcp-inet.XXXX)"
umask 077
export WG_HIDE_BANNER=1 WG_I_PREFER_BUGGY_USERSPACE_TO_POLISHED_KMOD=1

cleanup() {
  for ns in p1 p2 srv; do
    ip netns pids "$ns" 2>/dev/null | xargs -r kill 2>/dev/null || true
    ip netns del "$ns" 2>/dev/null || true
  done
}
trap cleanup EXIT
cleanup

mkwg() { if ip -n "$1" link add "$2" type wireguard 2>/dev/null; then return; fi; ip netns exec "$1" wireguard-go "$2" 2>/dev/null; }

for ns in srv p1 p2; do ip netns add $ns; ip -n $ns link set lo up; done
ip link add v1 netns srv type veth peer name e0 netns p1
ip link add v2 netns srv type veth peer name e0 netns p2
ip -n srv addr add 192.168.50.1/30 dev v1; ip -n srv link set v1 up
ip -n srv addr add 192.168.51.1/30 dev v2; ip -n srv link set v2 up
ip -n srv addr add 203.0.113.1/32 dev lo           # the server's "public" address
ip -n p1 addr add 192.168.50.2/30 dev e0; ip -n p1 link set e0 up
ip -n p2 addr add 192.168.51.2/30 dev e0; ip -n p2 link set e0 up
ip -n p1 route add 203.0.113.1/32 via 192.168.50.1  # phones reach only the server
ip -n p2 route add 203.0.113.1/32 via 192.168.51.1
for n in srv p1 p2; do wg genkey > "$W/$n.key"; wg pubkey < "$W/$n.key" > "$W/$n.pub"; done
ip netns exec srv sysctl -q -w net.ipv4.ip_forward=1
mkwg srv wgsrv
ip -n srv addr add 10.0.100.1/24 dev wgsrv
ip netns exec srv wg set wgsrv listen-port 51820 private-key "$W/srv.key" \
  peer "$(cat "$W/p1.pub")" allowed-ips 10.0.100.3/32 peer "$(cat "$W/p2.pub")" allowed-ips 10.0.100.5/32
ip -n srv link set wgsrv up
mkwg p1 wg0
ip -n p1 addr add 10.0.100.3/24 dev wg0
ip netns exec p1 wg set wg0 private-key "$W/p1.key" peer "$(cat "$W/srv.pub")" \
  endpoint 192.168.50.1:51820 allowed-ips 10.0.100.0/24 persistent-keepalive 5
ip -n p1 link set wg0 up
mkwg p2 tun0
ip -n p2 addr add 10.0.100.5/32 dev tun0
ip netns exec p2 wg set tun0 private-key "$W/p2.key" peer "$(cat "$W/srv.pub")" \
  endpoint 192.168.51.1:51820 allowed-ips 10.0.100.0/24 persistent-keepalive 5
ip -n p2 link set tun0 up
ip -n p2 route add 10.0.100.0/24 dev tun0

pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; tail -n 30 "$W"/p1.log "$W"/p2.log "$W"/relay.log; exit 1; }
wait_for() { local t=$1; shift; for _ in $(seq 1 "$t"); do if "$@" >/dev/null 2>&1; then return 0; fi; sleep 1; done; return 1; }
q() { "$PY" -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
api() {  # api NS METHOD PATH [JSON]
  ip netns exec "$1" "$PY" - "$W/data-$1" "$2" "$3" "${4:-}" <<'PYEOF'
import json, ssl, sys, urllib.request
d, method, path, body = sys.argv[1:5]
tok = open(f"{d}/web_token").read().strip()
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
req = urllib.request.Request(f"https://127.0.0.1:9876{path}", method=method,
                             data=(body or "{}").encode() if method == "POST" else None)
req.add_header("Authorization", "Bearer " + tok); req.add_header("Content-Type", "application/json")
print(urllib.request.urlopen(req, context=ctx, timeout=30).read().decode())
PYEOF
}
start_node() {
  mkdir -p "$W/data-$1"
  [ -f "$W/data-$1/settings.json" ] || echo '{"setup_complete": true, "auto_announce": false, "version": "0.5.0", "display_name": "'"$2"'"}' > "$W/data-$1/settings.json"
  (cd "$ROOT" && SRLTCP_DATA_DIR="$W/data-$1" ip netns exec "$1" "$PY" -m srltcp web --name "$2" \
     >> "$W/$1.log" 2>&1 < /dev/null &)
  wait_for 30 test -s "$W/data-$1/web_token" && wait_for 30 api "$1" GET /api/version || fail "$2 did not start"
}
stop_node() { ip netns pids "$1" | xargs -r kill; sleep 2; }

# --- relay on the server ---
(cd "$ROOT" && ip netns exec srv "$PY" -m srltcp relay --port 7827 --public-host 203.0.113.1 \
   --data-dir "$W/relay" > "$W/relay.log" 2>&1 < /dev/null &)
wait_for 20 grep -q "srltcp-relay://" "$W/relay.log" || fail "relay did not start"
CODE=$(grep -o "srltcp-relay://[^ ]*" "$W/relay.log" | head -1)
pass "relay running at 203.0.113.1:7827"

start_node p1 Phone1; start_node p2 Phone2
# Only phone1 has the relay code; phone2 must learn it from phone1.
api p1 POST /api/settings "{\"relay_code\": \"$CODE\"}" >/dev/null
api p1 POST "/api/announce?transport=tcp" >/dev/null
sees() { api "$1" GET /api/peers | q "[p['tcp_host'] for p in d]" | grep -q "$2"; }
wait_for 15 sees p2 10.0.100.3 && wait_for 15 sees p1 10.0.100.5 || fail "discovery over WireGuard"
H1=$(api p1 GET /api/identities | q "d['tcp']['hash_id']")
H2=$(api p2 GET /api/identities | q "d['tcp']['hash_id']")
api p1 POST /api/trusted "{\"hash_id\":\"$H2\"}" >/dev/null
api p2 POST /api/trusted "{\"hash_id\":\"$H1\"}" >/dev/null
got() { api "$1" GET "/api/messages?peer=$2" | q "[m['text'] for m in d]" | grep -q "$3"; }
path() { api "$1" GET /api/trusted | q "[p.get('path') for p in d if p['hash_id']=='$2'][0]"; }
api p1 POST /api/messages "{\"recipient_hash\":\"$H2\",\"text\":\"hello over the vpn\"}" >/dev/null
wait_for 15 got p2 "$H1" "hello over the vpn" || fail "message over WireGuard"
pass "paired on the VPN; path phone1->phone2: $(path p1 "$H2")"
ready() { api "$1" GET /api/trusted | q "[p['relay_ready'] for p in d if p['hash_id']=='$2'][0]" | grep -q True; }
relay_up() { api "$1" GET /api/status | q "any(r['connected'] for r in d['relay']['relays'])" | grep -q True; }
wait_for 20 ready p1 "$H2" && wait_for 20 ready p2 "$H1" || fail "pair secret exchange"
wait_for 30 relay_up p2 || fail "phone2 never connected to phone1's relay"
pass "relay details exchanged inside the encrypted session; phone2 learned the relay by itself"

# --- VPN off on phone2 ---
ip -n p2 link set tun0 down
start=$(date +%s)
api p1 POST /api/messages "{\"recipient_hash\":\"$H2\",\"text\":\"vpn is off now\"}" >/dev/null
api p2 POST /api/messages "{\"recipient_hash\":\"$H1\",\"text\":\"still here without vpn\"}" >/dev/null
wait_for 60 got p2 "$H1" "vpn is off now" || fail "phone1 -> phone2 over the relay"
wait_for 60 got p1 "$H2" "still here without vpn" || fail "phone2 -> phone1 over the relay"
pass "both directions delivered $(( $(date +%s) - start ))s after the VPN went off; path: $(path p1 "$H2")"
[ "$(path p1 "$H2")" = "relay" ] || fail "expected the relay path"

if "$PY" -c "import playwright" 2>/dev/null; then
  F="$W/flags"; mkdir -p "$F"
  (ip netns exec p2 "$PY" "$ROOT/scripts/sim_call_side.py" callee "$W/data-p2" "$F" > "$F/callee.out" 2>&1 &)
  ip netns exec p1 timeout 120 "$PY" "$ROOT/scripts/sim_call_side.py" caller "$W/data-p1" "$F" > "$F/caller.out" 2>&1 || true
  sleep 3
  grep -q "call connected" "$F/caller.out" && grep -q "call connected" "$F/callee.out" || { cat "$F"/*.out; fail "video call over the relay"; }
  grep -h "call connected" "$F/caller.out" "$F/callee.out" | sed 's/^/      /'
  pass "video call over the relay (no VPN)"
fi

# --- VPN back on: back to a direct path ---
ip -n p2 link set tun0 up; ip -n p2 route replace 10.0.100.0/24 dev tun0
start=$(date +%s)
direct() { [ "$(path p1 "$H2")" = "direct" ] && [ "$(path p2 "$H1")" = "direct" ]; }
wait_for 120 direct || fail "never moved back to the direct path"
pass "moved back to the direct path $(( $(date +%s) - start ))s after the VPN came back"
api p2 POST /api/messages "{\"recipient_hash\":\"$H1\",\"text\":\"direct again\"}" >/dev/null
wait_for 20 got p1 "$H2" "direct again" || fail "message after moving back"

# --- no VPN at all, app restarted: reconnects over the relay ---
ip -n p1 link set wg0 down; ip -n p2 link set tun0 down
stop_node p2; start_node p2 Phone2
start=$(date +%s)
api p2 POST /api/messages "{\"recipient_hash\":\"$H1\",\"text\":\"restarted with no vpn\"}" >/dev/null
wait_for 60 got p1 "$H2" "restarted with no vpn" || fail "cold start over the relay"
pass "after an app restart with no VPN, delivered over the relay in $(( $(date +%s) - start ))s"
echo "All internet checks passed."
