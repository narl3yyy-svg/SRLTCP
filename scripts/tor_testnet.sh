#!/bin/bash
# A private Tor network on 127.0.0.1 for testing SRLTCP's Tor transport offline:
# 3 directory authorities and 5 relays (all HSDir/guard), using the locally
# installed tor and tor-gencert. Nothing talks to the real Tor network.
#
# Usage: bash scripts/tor_testnet.sh start DIR   -> prints the torrc lines a
#                                                  client needs (saved to DIR/client.torrc)
#        bash scripts/tor_testnet.sh stop DIR
set -euo pipefail
CMD="${1:-start}"
DIR="$(realpath -m "${2:-/tmp/srltcp-tornet}")"
AUTHS=3
RELAYS=5

stop() {
  for pid in "$DIR"/*/pid; do
    [ -f "$pid" ] && kill "$(cat "$pid")" 2>/dev/null || true
  done
}
if [ "$CMD" = stop ]; then stop; exit 0; fi

stop
rm -rf "$DIR"; mkdir -p "$DIR"; chmod 700 "$DIR"

common() {  # common NAME ORPORT
  cat <<EOF
TestingTorNetwork 1
DataDirectory $DIR/$1
PidFile $DIR/$1/pid
Log notice file $DIR/$1/notice.log
RunAsDaemon 1
SafeLogging 0
ShutdownWaitLength 0
Address 127.0.0.1
ORPort 127.0.0.1:$2
AssumeReachable 1
SocksPort 0
ExitRelay 0
ExitPolicy reject *:*
TestingDirAuthVoteGuard *
TestingDirAuthVoteHSDir *
TestingDirAuthVoteExit *
TestingV3AuthInitialVotingInterval 5
TestingV3AuthInitialVoteDelay 2
TestingV3AuthInitialDistDelay 2
V3AuthVotingInterval 20
V3AuthVoteDelay 4
V3AuthDistDelay 4
TestingMinExitFlagThreshold 0
EOF
}

# Authorities: identity + signing keys, then their relay fingerprint.
: > "$DIR/dirauth.lines"
for i in $(seq 0 $((AUTHS - 1))); do
  n="auth$i"; orp=$((15000 + i)); dirp=$((17000 + i))
  mkdir -p "$DIR/$n/keys"; chmod 700 "$DIR/$n"
  (cd "$DIR/$n/keys" && echo "" | tor-gencert --create-identity-key -m 12 \
     -a "127.0.0.1:$dirp" --passphrase-fd 0 >/dev/null 2>&1)
  v3=$(grep fingerprint "$DIR/$n/keys/authority_certificate" | awk '{print $2}')
  common "$n" "$orp" > "$DIR/$n/torrc"
  cat >> "$DIR/$n/torrc" <<EOF
DirPort 127.0.0.1:$dirp
AuthoritativeDirectory 1
V3AuthoritativeDirectory 1
ContactInfo auth$i@test
Nickname auth$i
EOF
  fp=$(tor --list-fingerprint -f "$DIR/$n/torrc" --DirAuthority "x 127.0.0.1:1 ffffffffffffffffffffffffffffffffffffffff" 2>/dev/null | tail -1 | awk '{print $2$3$4$5$6$7$8$9$10$11}')
  echo "DirAuthority auth$i orport=$orp no-v2 v3ident=$v3 127.0.0.1:$dirp $fp" >> "$DIR/dirauth.lines"
done
for i in $(seq 0 $((AUTHS - 1))); do cat "$DIR/dirauth.lines" >> "$DIR/auth$i/torrc"; done

for i in $(seq 0 $((RELAYS - 1))); do
  n="relay$i"; mkdir -p "$DIR/$n"; chmod 700 "$DIR/$n"
  common "$n" $((15100 + i)) > "$DIR/$n/torrc"
  echo "Nickname relay$i" >> "$DIR/$n/torrc"
  cat "$DIR/dirauth.lines" >> "$DIR/$n/torrc"
done

for d in "$DIR"/auth* "$DIR"/relay*; do tor -f "$d/torrc" >/dev/null; done

{ echo "TestingTorNetwork 1"; cat "$DIR/dirauth.lines"; } > "$DIR/client.torrc"
echo "Private Tor network starting in $DIR (consensus in ~30-60 s)."
echo "Client torrc lines: $DIR/client.torrc"
