#!/usr/bin/env bash
# Different networks, no port forwarding anywhere.
#
# Part 1 — two homes:   phoneA - routerA (NAT) - internet (Tor doorbell, STUN) - routerB (NAT) - phoneB
#   The phones pair in person (temporary shared link), then each goes home.
#   Checks: they find each other over a private Tor network and punch a DIRECT
#   connection through both NATs (no port forwarding, no relay); with a strict
#   NAT on one side the message waits, and goes out directly once it's normal.
# Part 2 — one phone on the VPN, the other on the home Wi-Fi:
#   phoneL (home LAN) - home router (WireGuard server + NAT) - internet - phoneV (VPN only)
#   Checks: after pairing at home, phoneV away on the VPN reaches phoneL directly
#   at its LAN address (learnt inside the encrypted session).
#
# Needs root, iproute2, iptables, tor + tor-gencert, wireguard-tools (+ wireguard-go or kmod).
# Usage: sudo bash scripts/simulate_nat.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
W="$(mktemp -d /tmp/srltcp-nat.XXXX)"
umask 077
export WG_HIDE_BANNER=1 WG_I_PREFER_BUGGY_USERSPACE_TO_POLISHED_KMOD=1
NSS="inet ra rb ha hb home hl hv"
cleanup() {
  for ns in $NSS; do
    ip netns pids "$ns" 2>/dev/null | xargs -r kill 2>/dev/null || true
    ip netns del "$ns" 2>/dev/null || true
  done
}
trap cleanup EXIT
cleanup

pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; for f in "$W"/*.log; do echo "== $f"; tail -n 25 "$f"; done; exit 1; }
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
start_node() {  # start_node NS NAME
  mkdir -p "$W/data-$1"
  [ -f "$W/data-$1/settings.json" ] ||  # keep settings across restarts
    echo '{"setup_complete": true, "auto_announce": false, "version": "0.6.0", "display_name": "'"$2"'",
           "tor_enabled": true, "stun_servers": "198.51.100.1:3478, 198.51.100.1:3479"}' > "$W/data-$1/settings.json"
  (cd "$ROOT" && SRLTCP_DATA_DIR="$W/data-$1" SRLTCP_TOR_EXTRA_TORRC="$W/tornet/client.torrc" \
     ip netns exec "$1" "$PY" -m srltcp web --name "$2" \
     >> "$W/$1.log" 2>&1 < /dev/null &)
  wait_for 30 test -s "$W/data-$1/web_token" && wait_for 30 api "$1" GET /api/version || fail "$2 did not start"
}
hash_of() { api "$1" GET /api/identities | q "d['tcp']['hash_id']"; }
got() { api "$1" GET "/api/messages?peer=$2" | q "[m['text'] for m in d]" | grep -q "$3"; }
path() { api "$1" GET /api/trusted | q "[p.get('path') for p in d if p['hash_id']=='$2'][0]"; }
link_addr() { api "$1" GET /api/status | q "[l['address'] for l in d['links'] if l['hash_id']=='$2'][0]"; }
pair_on() {  # pair_on NS1 NS2 IP2 : meet on a shared link, trust both ways
  api "$1" POST "/api/announce?target=$3" >/dev/null
  local h1 h2; h1=$(hash_of "$1"); h2=$(hash_of "$2")
  wait_for 15 sh -c "true" ; sleep 2
  api "$1" POST /api/trusted "{\"hash_id\":\"$h2\"}" >/dev/null || true
  api "$2" POST /api/trusted "{\"hash_id\":\"$h1\"}" >/dev/null || true
}
ready() { api "$1" GET /api/trusted | q "[bool(p.get('onion')) for p in d if p['hash_id']=='$2'][0]" | grep -q True; }

# ---------------- Part 1: two homes behind NAT ----------------
for ns in inet ra rb ha hb; do ip netns add $ns; ip -n $ns link set lo up; done
ip link add ia netns inet type veth peer name wan netns ra
ip link add ib netns inet type veth peer name wan netns rb
ip link add lan netns ra type veth peer name e0 netns ha
ip link add lan netns rb type veth peer name e0 netns hb
ip -n inet addr add 198.51.100.1/24 dev ia; ip -n inet link set ia up
ip -n inet addr add 198.51.101.1/24 dev ib; ip -n inet link set ib up
ip -n ra addr add 198.51.100.2/24 dev wan; ip -n ra link set wan up
ip -n rb addr add 198.51.101.2/24 dev wan; ip -n rb link set wan up
ip -n ra addr add 192.168.1.1/24 dev lan; ip -n ra link set lan up
ip -n rb addr add 192.168.2.1/24 dev lan; ip -n rb link set lan up
ip -n ha addr add 192.168.1.2/24 dev e0; ip -n ha link set e0 up; ip -n ha route add default via 192.168.1.1
ip -n hb addr add 192.168.2.2/24 dev e0; ip -n hb link set e0 up; ip -n hb route add default via 192.168.2.1
ip -n ra route add default via 198.51.100.1; ip -n rb route add default via 198.51.101.1
for ns in inet ra rb; do ip netns exec $ns sysctl -q -w net.ipv4.ip_forward=1; done
for r in ra rb; do  # home router: NAT + stateful firewall, nothing forwarded
  ip netns exec $r iptables -t nat -A POSTROUTING -o wan -j MASQUERADE
  ip netns exec $r iptables -A FORWARD -i wan -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  ip netns exec $r iptables -A FORWARD -i wan -j DROP
  # Most home routers silently ignore unexpected connections ("stealth");
  # ROUTERS=reset makes them answer with a TCP reset instead (harder case).
  if [ "${ROUTERS:-stealth}" != reset ]; then
    ip netns exec $r iptables -A INPUT -i wan -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    ip netns exec $r iptables -A INPUT -i wan -p tcp -j DROP
  fi
done
# temporary "same Wi-Fi" link for meeting in person
ip link add meet netns ha type veth peer name meet netns hb
ip -n ha addr add 10.9.9.1/24 dev meet; ip -n ha link set meet up
ip -n hb addr add 10.9.9.2/24 dev meet; ip -n hb link set meet up

(cd "$ROOT" && ip netns exec inet "$PY" scripts/stun_server.py --host 198.51.100.1 > "$W/stun.log" 2>&1 < /dev/null &)
(cd "$ROOT" && ip netns exec inet "$PY" scripts/stun_server.py --host 198.51.100.1 --port 3479 > "$W/stun2.log" 2>&1 < /dev/null &)
ADDR=198.51.100.1 ip netns exec inet bash "$ROOT/scripts/tor_testnet.sh" start "$W/tornet" > "$W/tornet.log" 2>&1
sleep 45  # (the private Tor network needs its first consensus)

start_node ha PhoneA; start_node hb PhoneB
pair_on ha hb 10.9.9.2
HA=$(hash_of ha); HB=$(hash_of hb)
api ha POST /api/messages "{\"recipient_hash\":\"$HB\",\"text\":\"met in person\"}" >/dev/null
wait_for 20 got hb "$HA" "met in person" || fail "pairing in person"
wait_for 180 ready ha "$HB" && wait_for 180 ready hb "$HA" || fail "onion addresses exchanged"
pass "paired in person (shared link)"

# go home: the shared link disappears
ip -n ha link del meet
start=$(date +%s)
api hb POST /api/messages "{\"recipient_hash\":\"$HA\",\"text\":\"home now, no port forwarding\"}" >/dev/null
wait_for 240 got ha "$HB" "home now, no port forwarding" || fail "message after going home"
direct() { link_addr ha "$HB" | grep -q '^udp:' && link_addr hb "$HA" | grep -q '^udp:'; }
wait_for 30 direct || fail "never punched a direct path through the two NATs"
pass "found over Tor, direct through both home routers, message delivered after $(( $(date +%s) - start ))s — A sees B at $(link_addr ha "$HB"), B sees A at $(link_addr hb "$HA")"
api ha POST /api/messages "{\"recipient_hash\":\"$HB\",\"text\":\"direct across networks\"}" >/dev/null
wait_for 20 got hb "$HA" "direct across networks" || fail "message over the punched path"
pass "messages flow over the punched direct path"

# strict NAT (random ports, like many carrier NATs) on one side, port-dependent
# filtering on the other: only a birthday punch gets through (many sockets on
# the strict side, many guessed ports from the other)
ip netns exec rb iptables -t nat -F POSTROUTING
ip netns exec rb iptables -t nat -A POSTROUTING -o wan -j MASQUERADE --random-fully
ip netns exec rb conntrack -F 2>/dev/null || true
for ns in ha hb; do ip netns pids $ns | xargs -r kill; done; sleep 2
start_node ha PhoneA; start_node hb PhoneB
start=$(date +%s)
api hb POST /api/messages "{\"recipient_hash\":\"$HA\",\"text\":\"strict nat\"}" >/dev/null
wait_for 240 got ha "$HB" "strict nat" || fail "strict NAT on one side: birthday punch"
wait_for 30 direct || fail "strict NAT: delivered but not over a direct path"
pass "strict NAT on one side: birthday punch, delivered directly after $(( $(date +%s) - start ))s — A sees B at $(link_addr ha "$HB")"
api ha POST /api/messages "{\"recipient_hash\":\"$HB\",\"text\":\"reply through strict\"}" >/dev/null
wait_for 20 got hb "$HA" "reply through strict" || fail "reply over the birthday-punched path"
pass "messages flow both ways over the birthday-punched path"

# strict NAT on both sides: nothing direct is possible -> the message waits
ip netns exec ra iptables -t nat -F POSTROUTING
ip netns exec ra iptables -t nat -A POSTROUTING -o wan -j MASQUERADE --random-fully
for r in ra rb; do ip netns exec $r conntrack -F 2>/dev/null || true; done
for ns in ha hb; do ip netns pids $ns | xargs -r kill; done; sleep 2
start_node ha PhoneA; start_node hb PhoneB
api hb POST /api/messages "{\"recipient_hash\":\"$HA\",\"text\":\"both strict\"}" >/dev/null
sleep 90
got ha "$HB" "both strict" && fail "both strict: delivered — over what? (test setup?)"
pass "strict NAT on both sides: the message waits (nothing goes over Tor)"
ip netns exec ra iptables -t nat -F POSTROUTING
ip netns exec ra iptables -t nat -A POSTROUTING -o wan -j MASQUERADE
ip netns exec rb iptables -t nat -F POSTROUTING  # back to a normal router
ip netns exec rb iptables -t nat -A POSTROUTING -o wan -j MASQUERADE
ip netns exec rb conntrack -F 2>/dev/null || true
start=$(date +%s)
wait_for 240 got ha "$HB" "both strict" || fail "waiting message after the NAT became normal"
pass "normal NAT again: the waiting message went out directly after $(( $(date +%s) - start ))s"
for ns in ha hb inet; do ip netns pids $ns | xargs -r kill; done; sleep 1

# ---------------- Part 2: one phone on the VPN, the other on the home Wi-Fi ----------------
for ns in home hl hv; do ip netns add $ns; ip -n $ns link set lo up; done
ip link add wan netns home type veth peer name e0 netns hv    # "internet" between home and the away phone
ip link add lan netns home type veth peer name e0 netns hl
ip -n home addr add 203.0.113.1/24 dev wan; ip -n home link set wan up
ip -n hv addr add 203.0.113.50/24 dev e0; ip -n hv link set e0 up
ip -n home addr add 192.168.7.1/24 dev lan; ip -n home link set lan up
ip -n hl addr add 192.168.7.20/24 dev e0; ip -n hl link set e0 up; ip -n hl route add default via 192.168.7.1
ip netns exec home sysctl -q -w net.ipv4.ip_forward=1
for n in home hv; do wg genkey > "$W/$n.key"; wg pubkey < "$W/$n.key" > "$W/$n.pub"; done
mkwg() { if ip -n "$1" link add "$2" type wireguard 2>/dev/null; then return; fi; ip netns exec "$1" wireguard-go "$2" 2>/dev/null; }
mkwg home wghome
ip -n home addr add 10.0.100.1/24 dev wghome
ip netns exec home wg set wghome listen-port 51820 private-key "$W/home.key" peer "$(cat "$W/hv.pub")" allowed-ips 10.0.100.3/32
ip -n home link set wghome up
# typical home WireGuard: VPN clients reach the LAN through NAT on the server
ip netns exec home iptables -t nat -A POSTROUTING -s 10.0.100.0/24 -o lan -j MASQUERADE
start_node hl PhoneLan; start_node hv PhoneVpn
# meet at home first (phone V on the home Wi-Fi)
ip link add meet2 netns hl type veth peer name meet2 netns hv
ip -n hl addr add 192.168.8.20/24 dev meet2; ip -n hl link set meet2 up
ip -n hv addr add 192.168.8.30/24 dev meet2; ip -n hv link set meet2 up
pair_on hv hl 192.168.8.20
HL=$(hash_of hl); HV=$(hash_of hv)
api hv POST /api/messages "{\"recipient_hash\":\"$HL\",\"text\":\"at home\"}" >/dev/null
wait_for 20 got hl "$HV" "at home" || fail "pairing at home"
pass "phone V and phone L paired at home"
# V leaves home and connects back with WireGuard (VPN covers the VPN subnet and the home LAN)
ip -n hv link del meet2
mkwg hv wg0
ip -n hv addr add 10.0.100.3/24 dev wg0
ip netns exec hv wg set wg0 private-key "$W/hv.key" peer "$(cat "$W/home.pub")" \
  endpoint 203.0.113.1:51820 allowed-ips 10.0.100.0/24,192.168.7.0/24 persistent-keepalive 5
ip -n hv link set wg0 up; ip -n hv route add 192.168.7.0/24 dev wg0 2>/dev/null || true
start=$(date +%s)
api hv POST /api/messages "{\"recipient_hash\":\"$HL\",\"text\":\"from the vpn\"}" >/dev/null
wait_for 60 got hl "$HV" "from the vpn" || fail "VPN phone -> LAN phone"
api hl POST /api/messages "{\"recipient_hash\":\"$HV\",\"text\":\"from the wifi\"}" >/dev/null
wait_for 60 got hv "$HL" "from the wifi" || fail "LAN phone -> VPN phone"
pass "one on the VPN, one on the Wi-Fi: both directions in $(( $(date +%s) - start ))s, direct (V reaches L at $(link_addr hv "$HL"))"
# Common home setup: WireGuard runs on a separate box, so devices on the LAN
# can't open connections to VPN addresses (only the VPN phone can dial in).
ip netns exec home iptables -A FORWARD -o wghome -m conntrack --ctstate NEW -j DROP
for ns in hl hv; do  # restart the apps only (the VPN keeps running)
  for pid in $(ip netns pids $ns); do grep -q srltcp /proc/$pid/cmdline 2>/dev/null && kill $pid; done
done; sleep 2
start_node hl PhoneLan; start_node hv PhoneVpn
start=$(date +%s)
api hl POST /api/messages "{\"recipient_hash\":\"$HV\",\"text\":\"lan first after restart\"}" >/dev/null
wait_for 90 got hv "$HL" "lan first after restart" || fail "LAN phone -> VPN phone when only the VPN side can dial"
api hv POST /api/messages "{\"recipient_hash\":\"$HL\",\"text\":\"vpn reply\"}" >/dev/null
wait_for 30 got hl "$HV" "vpn reply" || fail "VPN phone -> LAN phone (one-way routing)"
pass "LAN can't route to the VPN: both apps restarted, LAN phone's message still arrived in $(( $(date +%s) - start ))s (VPN phone dialed in)"
# The VPN connects after the apps are already running (toggling it on a phone).
for pid in $(ip netns pids hv); do grep -q srltcp /proc/$pid/cmdline 2>/dev/null || kill $pid; done
sleep 1; ip -n hv link del wg0 2>/dev/null || true
api hl POST /api/messages "{\"recipient_hash\":\"$HV\",\"text\":\"while your vpn was off\"}" >/dev/null
sleep 20
mkwg hv wg0
ip -n hv addr add 10.0.100.3/24 dev wg0
ip netns exec hv wg set wg0 private-key "$W/hv.key" peer "$(cat "$W/home.pub")" \
  endpoint 203.0.113.1:51820 allowed-ips 10.0.100.0/24,192.168.7.0/24 persistent-keepalive 5
ip -n hv link set wg0 up; ip -n hv route add 192.168.7.0/24 dev wg0 2>/dev/null || true
start=$(date +%s)
wait_for 60 got hv "$HL" "while your vpn was off" || fail "message waiting on the LAN phone after the VPN came back"
pass "VPN switched on later: the LAN phone's waiting message arrived $(( $(date +%s) - start ))s after the VPN connected"
echo "All different-network checks passed."
