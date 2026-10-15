# Internet relay

Two phones on mobile data can't connect to each other: carriers put them behind
carrier-grade NAT, so neither has an address the other can reach. Once you're off
the shared Wi-Fi or VPN, something reachable has to sit in the middle. In SRLTCP
that is a **relay you run yourself** — a tiny program on any server with a public
address. Your WireGuard server is ideal.

The relay is designed to be **untrusted**: it forwards sealed packets and learns as
little as possible.

## Setting it up

**One command** (Ubuntu/Debian; also in the app: *Settings → Network → Set up your
own relay*):

```bash
curl -fsSLO https://raw.githubusercontent.com/narl3yyy-svg/SRLTCP/main/scripts/install-relay.sh
less install-relay.sh      # read it first
sudo bash install-relay.sh # options: --port 443 (default)  --public-host relay.example.com
```

It installs the relay as a sandboxed service (its own user, only the right to use
port 443), opens the port in `ufw` if that's active, starts it after every reboot and
shows the relay code and a QR code. On **one** phone tap *Settings → Network → Scan
relay code*. Run it again to update. Your cloud provider's own firewall (security
group) must allow the port too.

**By hand:**

```bash
git clone https://github.com/narl3yyy-svg/SRLTCP.git && cd SRLTCP
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/srltcp relay --public-host YOUR.SERVER.IP
```

It prints a code like `srltcp-relay://203.0.113.1:7827#…`:

1. Open **TCP 7827** in the server's firewall (`ufw allow 7827/tcp`, or your cloud
   provider's security group). With `--port 443` it looks like ordinary HTTPS to
   networks that block unusual ports.
2. On **one** phone: *Settings → Network → Internet relay*, paste the code, save.
   Contacts learn it automatically inside the encrypted connection; they don't need
   to paste anything.
3. Done. *Settings → Network* shows whether each relay is connected.

To keep it running, use the systemd unit in `scripts/srltcp-relay.service` (it
includes sandboxing). `journalctl -u srltcp-relay` shows the code again.

**Keep the code private** — it contains the relay's access key. If it leaks, delete
`access.key` in the relay's key directory and restart: you get a new code. Paste it
on your phone; contacts that are online get the new one automatically (a contact who
was offline the whole time needs one direct connection — same Wi-Fi or VPN — to pick
it up).

## How the phones use it

- **Pair locally first** (Wi-Fi, WireGuard, QR, NFC). While paired and connected, the
  two phones exchange — inside the end-to-end encrypted session — a random 32-byte
  contribution each (together: the *pair secret*) and the relay code.
- **Direct first.** A phone always tries its contact's LAN/VPN addresses first. If
  none answers within 3 s it uses the relay. Messages, files, voice and video calls
  all work over the relay.
- **Fast failover.** When an interface disappears (VPN switched off, Wi-Fi lost) the
  affected connections move to the relay within seconds (3 s in our test), instead of
  waiting for timeouts.
- **Back to direct.** While on the relay, one of the two phones tries the direct path
  every ~45 s (not during calls or file transfers) and moves the session back when it
  works (18 s after the VPN came back in our test). It dials every LAN/VPN address the
  contact has told it about at once, so one phone on the VPN and the other on the home
  Wi-Fi connect directly.
- **Through home routers (hole punching).** The phones connect to the relay from port
  7828 (peer port + 3); the relay tells each one its public address, and they swap
  those inside the encrypted session (only if *Try direct internet connections* is on).
  Then both dial each other at the same moment: each router lets the other phone's
  packets in as replies, and the connection must pass the normal signed handshake
  before it replaces the relay. Strict carrier NAT defeats it; the relay keeps working.
  Details: [NETWORKING.md](NETWORKING.md).
- **Relay first on restricted networks** (*Settings → Network*, off by default). On a
  Wi-Fi that keeps devices apart (planes, hotels, guest Wi-Fi) or when both phones are
  behind the same public address (Starlink, a carrier), no direct path can work. With
  this on, the app notices (the contact's address on this Wi-Fi didn't answer, or the
  contact reports the same public address) and goes straight to the relay instead of
  first waiting for a direct dial, moves a session that came in over Tor to the relay
  (nothing waits for a direct path that won't come), and stops direct tries until the
  network changes. It needs the network to allow ordinary internet traffic; the app
  doesn't try to get around a network's sign-in page or rules.
- **Which relay:** if both contacts have a relay, the one of the contact with the lower
  identity hash is used, so both sides agree. Only one of you needs one.

## What the relay can and can't see

| | Relay operator / someone who hacks the server | Network between phone and relay |
|---|---|---|
| Message text, files, calls | ✗ (end-to-end encrypted, and sealed again with the pair key) | ✗ |
| Names, identity hashes, public keys, group ids | ✗ | ✗ |
| Which contact you talk to | ✗ — only rotating mailbox tokens; it can guess from timing | ✗ (TLS) |
| Message types (text / file / call) | ✗ — packets are padded to 256-byte or 4 KiB buckets | ✗ |
| Inject, alter or replay packets | ✗ — rejected by the pair-key layer and the session | ✗ |
| Impersonate the relay | — | ✗ (certificate pinned by the code) |
| Your IP address, when you're online, rough volume | ✓ | ✓ (that you talk to the relay) |
| Block or delay your traffic | ✓ (then direct paths still work) | ✓ |

Layers of a relayed packet, outside in:

1. **TLS 1.3**, relay certificate pinned (SHA-256 in the relay code); access key in the
   first message, compared in constant time.
2. **Mailbox token** (16 bytes): `SHA-256("srltcp-relay-token-v1" ‖ preimage)`, where the
   preimage is `HKDF(pair secret, "mailbox|" ‖ recipient ‖ day)`. Subscribing needs the
   preimage, which only the two contacts can compute; tokens change every day.
3. **Pair box**: ChaCha20-Poly1305 with one key per direction derived from the pair
   secret (a packet can't be reflected back), a nonce made of a millisecond timestamp
   and random bytes, a replay window (packets more than 30 s older than the newest one
   are dropped, and duplicates always are), and padding.
4. **SRLTCP session**: the v4 handshake (Ed25519-signed ephemeral X25519, the signature
   names the recipient) and AES-256-GCM with counter nonces and a replay window.

A relay mailbox only accepts the contact it belongs to: a handshake proving any other
identity on it is dropped.

## Limits (abuse protection)

Per relay: 2048 connections, 64 per IP address (carrier NAT shares addresses), TLS and
access key within 15 s, 2-minute idle timeout (phones ping every 25 s), 256 KiB maximum
frame, 1024 mailboxes per connection, 30 subscription updates per minute, 16 MiB/s per
connection, and a receiver that falls 8 MiB behind loses packets instead of making the
relay buffer them. It stores nothing and logs only counters.

## Testing

`sudo bash scripts/simulate_internet.sh` builds two "phones" and a server in network
namespaces (WireGuard between the phones, relay on the server's public address) and
checks: pairing over the VPN, relay details exchanged automatically, both directions
over the relay within seconds of switching the VPN off, a video call over the relay,
moving back to direct when the VPN returns, and reconnecting over the relay after an
app restart with no VPN. `sudo bash scripts/simulate_nat.sh` puts two phones behind two
NAT routers with no port forwarding (`ROUTERS=reset` for routers that answer with
resets) and checks the relay, the punched direct path, fallback with a strict NAT, and
one phone on the VPN with the other on the home LAN. `scripts/pentest/pentest_relay.py` attacks the relay and the
clients (see [SECURITY_AUDIT.md](SECURITY_AUDIT.md)).
