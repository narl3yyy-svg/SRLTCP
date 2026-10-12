#!/usr/bin/env bash
# End-to-end test over a real WireGuard VPN (no broadcast), like two phones on
# different carriers connected to the same WireGuard server.
#
#   phone1 (10.0.100.3/24, wg0) --\
#                                   VPN server (10.0.100.1) -- routes between peers
#   phone2 (10.0.100.5/32, tun0) -/
#
# Checks: discovery with one Announce press, trust + contact request, message
# delivery, and recovery when phone2's VPN address changes.
#
# Needs: root (network namespaces), iproute2, wireguard-tools (wg), and either
# the WireGuard kernel module or wireguard-go. Usage: sudo bash scripts/simulate_wireguard.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
W="$(mktemp -d /tmp/srltcp-wg.XXXX)"
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

mkwg() {  # mkwg NS IFNAME
  if ip -n "$1" link add "$2" type wireguard 2>/dev/null; then return; fi
  ip netns exec "$1" wireguard-go "$2"
}

for ns in srv p1 p2; do ip netns add $ns; ip -n $ns link set lo up; done
ip link add v1 netns srv type veth peer name e0 netns p1
ip link add v2 netns srv type veth peer name e0 netns p2
ip -n srv addr add 192.168.50.1/30 dev v1; ip -n srv link set v1 up
ip -n srv addr add 192.168.51.1/30 dev v2; ip -n srv link set v2 up
ip -n p1 addr add 192.168.50.2/30 dev e0; ip -n p1 link set e0 up
ip -n p2 addr add 192.168.51.2/30 dev e0; ip -n p2 link set e0 up
for n in srv p1 p2; do wg genkey > "$W/$n.key"; wg pubkey < "$W/$n.key" > "$W/$n.pub"; done
ip netns exec srv sysctl -q -w net.ipv4.ip_forward=1
mkwg srv wgsrv
ip -n srv addr add 10.0.100.1/24 dev wgsrv
ip netns exec srv wg set wgsrv listen-port 51820 private-key "$W/srv.key" \
  peer "$(cat "$W/p1.pub")" allowed-ips 10.0.100.3/32 \
  peer "$(cat "$W/p2.pub")" allowed-ips 10.0.100.5/32
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

start_node() {  # start_node NS NAME
  mkdir -p "$W/data-$1"
  # discovery off, like phones set up with v0.2.x: VPN replies must still work
  echo '{"setup_complete": true, "auto_announce": false, "version": "0.3.0"}' > "$W/data-$1/settings.json"
  (cd "$ROOT" && SRLTCP_DATA_DIR="$W/data-$1" ip netns exec "$1" "$PY" -m srltcp web --name "$2" \
     > "$W/$1.log" 2>&1 < /dev/null &)
}
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
q() { "$PY" -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
wait_for() {  # wait_for SECONDS CMD...
  local t=$1; shift
  for _ in $(seq 1 "$t"); do if "$@" >/dev/null 2>&1; then return 0; fi; sleep 1; done
  return 1
}
pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; tail -n 20 "$W"/p1.log "$W"/p2.log; exit 1; }

start_node p1 Phone1; start_node p2 Phone2
wait_for 30 test -s "$W/data-p1/web_token" && wait_for 30 test -s "$W/data-p2/web_token" || fail "nodes did not start"
sleep 3

api p1 POST "/api/announce?transport=tcp" >/dev/null
sees() { api "$1" GET /api/peers | q "[p['tcp_host'] for p in d]" | grep -q "$2"; }
wait_for 10 sees p2 10.0.100.3 && wait_for 10 sees p1 10.0.100.5 || fail "discovery over WireGuard"
pass "one Announce press: both phones discovered each other over WireGuard"

H1=$(api p1 GET /api/identities | q "d['tcp']['hash_id']")
H2=$(api p2 GET /api/identities | q "d['tcp']['hash_id']")
api p1 POST /api/trusted "{\"hash_id\":\"$H2\"}" >/dev/null
api p2 POST /api/trusted "{\"hash_id\":\"$H1\"}" >/dev/null
api p1 POST /api/messages "{\"recipient_hash\":\"$H2\",\"text\":\"hello over wireguard\"}" >/dev/null
got() { api p2 GET /api/messages | q "[m['text'] for m in d]" | grep -q "$1"; }
wait_for 15 got "hello over wireguard" || fail "message over WireGuard"
pass "message delivered over WireGuard"

ip -n p2 addr del 10.0.100.5/32 dev tun0; ip -n p2 addr add 10.0.100.7/32 dev tun0
ip -n p2 route replace 10.0.100.0/24 dev tun0
ip netns exec srv wg set wgsrv peer "$(cat "$W/p2.pub")" allowed-ips 10.0.100.7/32
sleep 1
api p1 POST /api/messages "{\"recipient_hash\":\"$H2\",\"text\":\"after ip change\"}" >/dev/null
start=$(date +%s)
wait_for 90 got "after ip change" || fail "recovery after VPN address change"
pass "message delivered $(( $(date +%s) - start ))s after phone2's VPN address changed"

# VPN switched off on phone2 (like turning WireGuard off on the phone): messages
# both ways wait in the outbox and go out by themselves once the VPN is back.
ip -n p2 link set tun0 down
sleep 2
api p1 POST /api/messages "{\"recipient_hash\":\"$H2\",\"text\":\"sent while your vpn was off\"}" >/dev/null
api p2 POST /api/messages "{\"recipient_hash\":\"$H1\",\"text\":\"written with my vpn off\"}" >/dev/null
sleep 10
got "sent while your vpn was off" && fail "delivered with no route (test setup broken)"
status() { api p1 GET /api/messages | q "[m['status'] for m in d if m['text']=='sent while your vpn was off'][0]"; }
echo "  while VPN is off, phone1 shows: $(status)"
ip -n p2 link set tun0 up
ip -n p2 route replace 10.0.100.0/24 dev tun0
start=$(date +%s)
got1() { api p1 GET /api/messages | q "[m['text'] for m in d]" | grep -q "$1"; }
wait_for 120 got "sent while your vpn was off" || fail "queued message after VPN came back"
wait_for 60 got1 "written with my vpn off" || fail "phone2's queued message after VPN came back"
pass "both queued messages delivered $(( $(date +%s) - start ))s after the VPN came back"
echo "All WireGuard checks passed."
