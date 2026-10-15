#!/usr/bin/env bash
# Direct UDP between two mobile networks (no port forwarding, no Tor speed).
#
#   phoneA - carrierA (NAT) - router - internet - router - carrierB (NAT) - phoneB
#                                         |  relay (the "doorbell"), STUN server
#
# Like real carriers: each phone is behind NAT, TCP between the two networks
# is blocked, the internet path has delay and some loss. Checks:
#   1. the phones find each other through the relay, swap UDP addresses
#      inside the encrypted session and move to a direct UDP connection;
#   2. a big file crosses it fast, messages flow while it does;
#   3. with "symmetric" carrier NAT on both sides (different port per
#      destination: punching can't work) they stay on the relay and work.
#
# Needs root, iproute2, iptables (and tc for the delay/loss).
# Usage: sudo bash scripts/simulate_udp.sh   (DELAY=25ms LOSS=0.5% RATE=100mbit)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
W="$(mktemp -d /tmp/srltcp-udp.XXXX)"
umask 077
NSS="inet xa xb ca cb pa pb"
cleanup() {
  for ns in $NSS; do
    ip netns pids "$ns" 2>/dev/null | xargs -r kill 2>/dev/null || true
    ip netns del "$ns" 2>/dev/null || true
  done
}
trap cleanup EXIT
cleanup

pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; for f in "$W"/*.log; do echo "== $f"; tail -n 30 "$f"; done; exit 1; }
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
  [ -f "$W/data-$1/settings.json" ] ||
    echo '{"setup_complete": true, "auto_announce": false, "display_name": "'"$2"'",
           "tor_enabled": false, "stun_servers": "198.51.100.1:3478"}' > "$W/data-$1/settings.json"
  (cd "$ROOT" && SRLTCP_DATA_DIR="$W/data-$1" ip netns exec "$1" "$PY" -m srltcp web --name "$2" \
     >> "$W/$1.log" 2>&1 < /dev/null &)
  wait_for 30 test -s "$W/data-$1/web_token" && wait_for 30 api "$1" GET /api/version || fail "$2 did not start"
}
hash_of() { api "$1" GET /api/identities | q "d['tcp']['hash_id']"; }
got() { api "$1" GET "/api/messages?peer=$2" | q "[m['text'] for m in d]" | grep -q "$3"; }
link_addr() { api "$1" GET /api/status | q "[l['address'] for l in d['links'] if l['hash_id']=='$2'][0]"; }
on_udp() { link_addr "$1" "$2" | grep -q '^udp:'; }
ready() { api "$1" GET /api/trusted | q "[p['relay_ready'] for p in d if p['hash_id']=='$2'][0]" | grep -q True; }

setup() {  # setup cone|symmetric : fresh networks, phones paired in person
  cleanup; rm -rf "$W"/data-* "$W"/relay
  NATOPT=""; [ "$1" = symmetric ] && NATOPT="--random-fully"  # new port per destination
for ns in $NSS; do ip netns add $ns; ip -n $ns link set lo up; done
# (Routers between the carriers: like the real internet, the other NAT is
# several hops away, which hop-limited "priming" packets rely on.)
ip link add ia netns inet type veth peer name uplink netns xa
ip link add ib netns inet type veth peer name uplink netns xb
ip link add dnlink netns xa type veth peer name wan netns ca
ip link add dnlink netns xb type veth peer name wan netns cb
ip -n xa addr add 198.51.100.254/24 dev uplink; ip -n xa link set uplink up
ip -n xb addr add 198.51.101.254/24 dev uplink; ip -n xb link set uplink up
ip -n xa addr add 203.0.113.1/30 dev dnlink; ip -n xa link set dnlink up
ip -n xb addr add 203.0.113.5/30 dev dnlink; ip -n xb link set dnlink up
ip -n xa route add default via 198.51.100.1; ip -n xb route add default via 198.51.101.1
ip link add lan netns ca type veth peer name e0 netns pa
ip link add lan netns cb type veth peer name e0 netns pb
ip -n inet addr add 198.51.100.1/24 dev ia; ip -n inet link set ia up
ip -n inet addr add 198.51.101.1/24 dev ib; ip -n inet link set ib up
ip -n ca addr add 203.0.113.2/30 dev wan; ip -n ca link set wan up
ip -n cb addr add 203.0.113.6/30 dev wan; ip -n cb link set wan up
ip -n inet route add 203.0.113.0/30 via 198.51.100.254; ip -n inet route add 203.0.113.4/30 via 198.51.101.254
ip -n ca addr add 100.64.1.1/24 dev lan; ip -n ca link set lan up    # carrier-grade NAT space
ip -n cb addr add 100.64.2.1/24 dev lan; ip -n cb link set lan up
ip -n pa addr add 100.64.1.2/24 dev e0; ip -n pa link set e0 up; ip -n pa route add default via 100.64.1.1
ip -n pb addr add 100.64.2.2/24 dev e0; ip -n pb link set e0 up; ip -n pb route add default via 100.64.2.1
ip -n ca route add default via 203.0.113.1; ip -n cb route add default via 203.0.113.5
for ns in inet xa xb ca cb; do ip netns exec $ns sysctl -q -w net.ipv4.ip_forward=1; done
for c in ca cb; do  # carrier: NAT + stateful firewall, nothing gets in unasked
  ip netns exec $c iptables -t nat -A POSTROUTING -o wan -j MASQUERADE $NATOPT
  ip netns exec $c iptables -A FORWARD -i wan -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  ip netns exec $c iptables -A FORWARD -i wan -j DROP
done
# Carriers: no TCP between the two mobile networks (only to servers).
ip netns exec inet iptables -A FORWARD -p tcp -j DROP
# The mobile internet: delay, a little loss, limited bandwidth (netem); a
# kernel without netem gets the loss only (iptables).
NET="${DELAY:-25ms} each way, ${LOSS:-0.5%} loss, ${RATE:-100mbit}"
if ! { ip netns exec inet tc qdisc add dev ia root netem delay "${DELAY:-25ms}" loss "${LOSS:-0.5%}" \
         rate "${RATE:-100mbit}" limit 10000 2>/dev/null &&
       ip netns exec inet tc qdisc add dev ib root netem delay "${DELAY:-25ms}" loss "${LOSS:-0.5%}" \
         rate "${RATE:-100mbit}" limit 10000; }; then
  p=$("$PY" -c "print(float('${LOSS:-0.5%}'.rstrip('%')) / 100)")
  ip netns exec inet iptables -A FORWARD -p udp -m statistic --mode random --probability "$p" -j DROP
  NET="${LOSS:-0.5%} loss each way (no netem in this kernel: no added delay or rate limit)"
fi
# temporary shared link for meeting in person
ip link add meet netns pa type veth peer name meet netns pb
ip -n pa addr add 10.9.9.1/24 dev meet; ip -n pa link set meet up
ip -n pb addr add 10.9.9.2/24 dev meet; ip -n pb link set meet up

(cd "$ROOT" && ip netns exec inet "$PY" -m srltcp relay --port 7827 --public-host 198.51.100.1 \
   --data-dir "$W/relay" > "$W/relay.log" 2>&1 < /dev/null &)
(cd "$ROOT" && ip netns exec inet "$PY" scripts/stun_server.py --host 198.51.100.1 --port 3478 > "$W/stun.log" 2>&1 < /dev/null &)
wait_for 20 grep -q "srltcp-relay://" "$W/relay.log" || fail "relay did not start"
CODE=$(grep -o "srltcp-relay://[^ ]*" "$W/relay.log" | head -1)

start_node pa PhoneA; start_node pb PhoneB
api pa POST /api/settings "{\"relay_code\": \"$CODE\"}" >/dev/null
api pa POST "/api/announce?target=10.9.9.2" >/dev/null; sleep 2
HA=$(hash_of pa); HB=$(hash_of pb)
api pa POST /api/trusted "{\"hash_id\":\"$HB\"}" >/dev/null || true
api pb POST /api/trusted "{\"hash_id\":\"$HA\"}" >/dev/null || true
api pa POST /api/messages "{\"recipient_hash\":\"$HB\",\"text\":\"met in person\"}" >/dev/null
wait_for 20 got pb "$HA" "met in person" || fail "pairing in person"
wait_for 30 ready pa "$HB" && wait_for 30 ready pb "$HA" || fail "pair secret exchange"
}

setup cone
pass "paired in person"

ip -n pa link del meet  # each goes back to their own carrier
start=$(date +%s)
api pb POST /api/messages "{\"recipient_hash\":\"$HA\",\"text\":\"on mobile data now\"}" >/dev/null
wait_for 60 got pa "$HB" "on mobile data now" || fail "message through the relay"
pass "on different carriers: delivered through the relay in $(( $(date +%s) - start ))s"

start=$(date +%s)
wait_for 150 sh -c "$(declare -f api q link_addr on_udp); W=$W PY=$PY; on_udp pa $HB && on_udp pb $HA" \
  || fail "never moved to a direct UDP connection"
pass "direct UDP through both carrier NATs after $(( $(date +%s) - start ))s — A sees B at $(link_addr pa "$HB"), B sees A at $(link_addr pb "$HA")"

head -c 40000000 /dev/urandom > "$W/data-pa/video.bin"
start=$(date +%s.%N)
TID=$(api pa POST /api/transfer "{\"recipient_hash\":\"$HB\",\"path\":\"$W/data-pa/video.bin\"}" | q "d['id']")
api pb POST /api/messages "{\"recipient_hash\":\"$HA\",\"text\":\"chat during the file\"}" >/dev/null
wait_for 30 got pa "$HB" "chat during the file" || fail "chat while the file moves"
done_tx() { api pb GET "/api/transfers?all=1" | q "[t['state'] for t in d if t['id']=='$TID'][0]" | grep -q complete; }
wait_for 180 done_tx || fail "40 MB file over direct UDP"
took=$("$PY" -c "import time; print(f'{time.time() - $start:.1f}')")
rate=$("$PY" -c "print(f'{40 / $took:.1f}')")
cmp -s "$W/data-pa/video.bin" "$(find "$W/data-pb" -name 'video*.bin' | head -1)" || fail "file differs"
pass "40 MB file over direct UDP in ${took}s (${rate} MB/s; $NET); chat kept flowing"
on_udp pa "$HB" || fail "left the UDP path during the transfer"

# Symmetric carrier NAT on both sides (a new port for every destination):
# punching can't work, the relay keeps them talking. Fresh networks so no
# NAT remembers the earlier connection.
for ns in $NSS; do ip netns pids $ns 2>/dev/null | xargs -r kill 2>/dev/null || true; done; sleep 2
setup symmetric
ip -n pa link del meet
api pb POST /api/messages "{\"recipient_hash\":\"$HA\",\"text\":\"symmetric nat\"}" >/dev/null
wait_for 60 got pa "$HB" "symmetric nat" || fail "relay fallback with symmetric NAT"
sleep 40
on_udp pa "$HB" && fail "symmetric NAT should not punch (test setup?)"
api pa POST /api/messages "{\"recipient_hash\":\"$HB\",\"text\":\"still talking\"}" >/dev/null
wait_for 30 got pb "$HA" "still talking" || fail "messages after failed punching"
pass "symmetric NAT on both sides: no direct path possible, still delivered via $(link_addr pa "$HB")"
echo "All UDP checks passed."
