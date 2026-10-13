# Security audit and penetration test (v0.7.2)

Scope: the whole app — local web UI/API, peer protocol, file transfers and shared
folders, discovery, pairing, group chats, the local call relay, the Android
wrapper, the internet relay (server, clients, rendezvous, privacy) — and, new in
0.6.0, direct connections across networks (address exchange, hole punching).

How: automated, repeatable attack harnesses that start their own nodes and
relays and actually perform each attack, plus code review by separate
reviewers for Python, the web UI and Android, plus end-to-end simulations
on real WireGuard in network namespaces.

```bash
.venv/bin/python scripts/pentest/pentest_local.py   # app surfaces
.venv/bin/python scripts/pentest/pentest_relay.py   # internet relay
sudo bash scripts/simulate_internet.sh              # VPN off/on, relay, calls
sudo bash scripts/simulate_nat.sh                   # two NATs, punching, VPN + LAN
bash scripts/tor_testnet.sh start /tmp/tornet        # private Tor network (offline)
SRLTCP_TOR_EXTRA_TORRC=/tmp/tornet/client.torrc .venv/bin/python scripts/simulate_tor.py
.venv/bin/python -m pytest -q                        # includes relay attack tests
```

Each harness prints PASS (defended) / FAIL (vulnerable) per check and exits
non-zero on any FAIL.

## Threat model

| Attacker | Can | Must not be able to |
|---|---|---|
| Anyone on the internet who reaches the relay | connect, send junk, flood | use the relay without the access key, exhaust it, learn anything about users |
| The relay operator (or whoever hacks the server) | see IPs, timing, volume; drop/delay | read content, names, identities, who talks to whom, message types; inject, alter or replay; impersonate a contact |
| A network attacker between phone and relay | observe, block, MITM attempts | impersonate the relay (pinned cert), read anything |
| Someone on the same LAN/VPN | send UDP/TCP to the phone | impersonate a contact, read sessions, crash or exhaust the app |
| A stranger with a valid identity | handshake, send any message type | have anything stored or acted on beyond one contact request |
| A trusted but malicious contact | send messages/files, misuse rendezvous and punch messages | escape the download folder, read other folders, forge group messages, hijack the pair's relay path, use our phone to knock on devices in our LAN or flood a third party |
| Another app on the phone / page in a browser | reach 127.0.0.1 | use the local API without the token, CSRF, DNS rebinding, the JS bridge |

## Results — app surfaces (`pentest_local.py`)

**66 PASS, 0 FAIL** after fixes (0.5.0 first run: 57 PASS, 1 FAIL; 0.6.1 added
7 attacks on password-protected shares; 0.7.2 added upload-name traversal).

| Area | Attacks | Result |
|---|---|---|
| Local web API | all 55 routes unauthenticated, wrong token, token in query on API, timing of token compare, DNS rebinding (Host), CSRF (Origin/Referer), WebSocket without token / cross-origin, CORS, oversized and malformed JSON, path traversal (browse, folder delete, transfer id, share download), CSP | defended |
| Peer TCP port | every session message type before a handshake (with and without a fake ENCRYPTED flag), malformed/short/unknown frames, hash/key mismatch, pinned-key mismatch, blocked peer, connection cap, handshake deadline | defended |
| Untrusted but valid identity | FILE_OFFER, SHARE_REQUEST, CALL_SIGNAL, GROUP_TEXT | no side effects; one contact request |
| Malicious trusted contact | filenames with `../`, absolute, `..\`, NUL; chunk replay onto the sender's own file; offset beyond size; SHA mismatch; share fetch traversal and symlink escape; forged group message | defended |
| Shares (0.6.1) | open a password share without / with a wrong password, fetch with a wrong password, password brute force, another contact using someone's grant (even claiming their id), 1100-offer flood with huge labels; password at rest, file permissions | defended: nothing listed or sent; locked after 5 tries for 10 min; grants bound to the authenticated identity; offers capped at 1000, labels 128 chars; only a salted scrypt hash in an owner-only `shares.json` |
| UDP discovery | bad/tampered signatures, non-JSON, address redirect via self-reported host, replayed announce, oversized datagrams, **announce flood** | flood was a FAIL — fixed |
| Pairing cards | tampered, malformed, oversized, own identity, token reuse/expiry | defended |
| Local call relay (TURN) | allocation without credentials, packets from non-local addresses, forwarding to anyone but the call's peer | defended |

### Findings and fixes

1. **Discovery registry grew without limit (medium, fixed).** Anyone on the LAN/VPN
   could make fresh identities and send validly self-signed announces; every one
   stayed in memory → memory exhaustion. Fixed: the registry holds at most 1024
   entries (expired first, then the stalest; contact requests last) and the
   announce-timestamp table is bounded too (`srltcp/core/discovery.py`,
   `srltcp/core/messaging/announce.py`).
2. **Unauthenticated connections could make the app buffer up to 64 MiB each
   (low, fixed).** Frames are now capped at 4 MiB (file chunks are 1 MiB), on top of
   the 30 s handshake deadline and the 512-connection cap.
3. **Malformed JSON returned HTTP 500 (cosmetic, fixed)** → now 400.

Earlier in this release cycle (found by code review, all fixed and covered by
tests): linear-time frame parsing (junk on the port froze the event loop),
hub registration replay, interrupted transfers never resuming, replayed
announces redirecting a contact's address, NFC answering while the app was in
the background, user-installed CAs bypassing the Android certificate pin, the
JS bridge answering any page, and handshake replay to a third device (now
handshake v4 names the recipient).

## Results — internet relay and direct connections (`pentest_relay.py`)

**44 PASS, 0 FAIL** (plus tests/test_relay.py, tests/test_punch.py and the namespace
simulations).

| Attacker | Attacks | Result |
|---|---|---|
| Internet stranger at the relay port | no/wrong access key, wrong protocol version (same answer as a wrong key: no oracle), commands before HELLO, slow TLS/HELLO (15 s deadline), TLS 1.2 downgrade, non-TLS junk, oversized frames, frame-length lies, 1000+ mailbox subscriptions, subscription storms, send floods, slow-reading receiver, total and per-IP connection caps | defended |
| Relay operator (has the certificate and access key) | read traffic, find names/identity hashes/public keys/message types/plaintext in relayed bytes, inject junk/huge/unknown-mailbox packets, replay genuine packets, reflect a client's own packets back, deliver one contact's packets on another's mailbox, impersonate a contact on its mailbox | defended — sees only padded opaque blobs (256 B buckets observed), rotating tokens |
| Network attacker between phone and relay | impersonate the relay | refused: certificate pin checked before the access key is sent (0 HELLO frames reached the fake relay) |
| Malicious trusted contact | bogus/oversized/host-injection relay codes in RENDEZVOUS, rapid contribution churn, pointing the pair at its own relay, a different identity answering a direct-upgrade attempt | defended / bounded (see below) |
| Untrusted peer | RENDEZVOUS | dropped (trust gate) |
| Another node | replay a handshake captured for someone else | refused (v4 recipient binding) |
| Local API | read pair secrets or relay keys via /api/trusted, /api/status, settings | not exposed (only your own relay code, token-protected) |
| Malicious trusted contact (0.6.0) | PUNCH offers aimed at LAN / loopback / cloud-metadata / carrier-NAT addresses or hostnames, offer floods, 200-candidate lists, hostnames and junk in RENDEZVOUS addresses | nothing dialed for private targets; one answered round per contact per 30 s; at most 6 targets; IP literals only |
| Untrusted peer (0.6.0) | PUNCH | ignored (trust gate; session message only) |
| Malicious relay (0.6.0) | report a LAN address as our "public" address | never advertised to contacts |
| Anyone (0.6.0) | take over a punched connection | must pass the signed v4 handshake with the contact's pinned key before it replaces the relay session |

Hardening applied from the INFO notes:

- A contact's relay code may no longer point at link-local, multicast, reserved or
  unspecified addresses (LAN and loopback stay allowed — self-hosted relays live
  there). Even before, the certificate pin stopped any data from reaching a wrong
  endpoint; this removes the "make the phone probe an internal address" angle.
- A contact can change its relay details at most once a minute, so a malicious
  contact can't turn RENDEZVOUS floods into constant contact-file rewrites.

### 0.7.0: Tor onion transport

New attack surface: an onion service per device, Tor SOCKS dialing, and the
onion address in RENDEZVOUS. Design and checks (private Tor network on
127.0.0.1, `scripts/simulate_tor.py`, plus `tests/test_tor.py`):

| Attacker | Attempt | Result |
|---|---|---|
| Stranger who learns the onion address | connect over Tor, handshake with a valid identity | refused before a session or contact request exists (onion listener is separate and loopback-only; Tor connections require a trusted identity) |
| Malicious trusted contact | send a junk / non-v3 / bad-checksum / injection string as `"o"` | not stored (v3 address with version byte and checksum verified) |
| Anyone on the Tor path | read or alter traffic | sees only the SRLTCP E2EE session inside Tor's own encryption; impersonation needs the contact's pinned identity key |
| Network observer / ISP | learn who talks to whom | sees Tor use only; contacts don't learn each other's IP; one circuit per contact (SOCKS isolation) |
| Local apps on the device | use our onion listener / control port | onion listener on 127.0.0.1 accepts only trusted identities; Tor control port is cookie-authenticated with an owner-only cookie; onion key stored 0600 |

**0.7.1 fix (Android, medium):** tor-android's TorService forces
`--CookieAuthentication 0` on its command line. 0.7.0 asked it for an extra TCP
control port, which would have had no authentication, so any app on the phone
could have controlled that Tor. 0.7.1 uses TorService's control socket instead.
That's a Unix socket inside the app's private storage, so only SRLTCP can reach
it, and there's no TCP control port at all. 0.7.0 also started the service via
`Class.forName`, which can fail when called from Python; now it uses an explicit
component name. `simulate_tor.py --android-style` runs one node exactly the way
TorService runs Tor (control socket, cookie auth off) against the private Tor
network.

Other notes: turning Tor off tells contacts first (they drop the address);
the onion address persists across restarts; a Tor session upgrades to a
direct path when one appears and the Tor connection is closed. Android bundles
tor-android 0.4.9.5. That's the newest build this toolchain can package; newer
ones need compileSdk 37. Update it when the Android toolchain moves.

### 0.7.2: findings and fixes

- **Stuck file offer disabled dead-link detection (availability, medium).**
  An outgoing offer that was never answered (lost when the link dropped)
  stayed "offered" forever. Offered transfers count as active, and active
  transfers suspend pings, the dead-link reaper and reconnects for that
  contact, so the session stayed half-open: later messages and files were
  written into a dead socket. A peer could also cause this on purpose by
  never answering offers. Fixed: an unanswered offer is resent once after
  30 s, then the transfer is queued (paused) and stops counting as active.
  When the link goes down, pending outgoing offers are queued as well.
- **Upload file names (low).** Upload names are now stripped of leading dots,
  so `.`, `..` and hidden names can't be created in `uploads/`. The transfer
  path check rejected any path containing `..` as a substring, which broke
  names like `report..v2.pdf`; it now rejects only real `..` path parts,
  which still blocks traversal. A new pentest check uploads `..`, `.`,
  `../../evil.txt`, `..\..\evil.txt`, `.hidden` and `/etc/x` and confirms
  each lands inside `uploads/` as a visible file.
- Queued files are offered only over an authenticated session with the
  trusted recipient, the same as before. Nothing is sent before the
  handshake.

### 0.6.1: findings and fixes

1. **Handshake race garbled sessions (availability, fixed).** A second connect
   request while a handshake was in flight (an automatic reconnect plus sending a
   message) sent a second handshake on the same connection; the two sides derived
   different keys and every message failed to decrypt until the link timed out.
   No confidentiality impact (messages failed closed), but contacts looked
   disconnected. Now the first handshake finishes; regression tests cover it.
2. **Experimental Noise XX setting removed.** A device set to it couldn't talk to
   any default device. Everyone uses the audited identity handshake (v4
   recipient binding); a saved `noise_xx` is ignored.
3. **QR pairing tokens.** The scanned device keeps the one-time tokens it showed
   (15-minute validity, single use) as SHA-256 hashes in an owner-only file, so
   pairing completes both ways after a restart; the scanner keeps the token to
   send back in the owner-only contacts file until delivered. An expired or
   reused token still only produces a contact request the user must accept.
4. **Shares.** New password option (salted scrypt, sent only inside the E2EE
   session, lockout), offers to offline contacts, persistence — attacked as in
   the table above.
5. **Deleted contacts.** Their one-to-one history is purged 24 hours after
   deletion unless they are added back.

### Direct connections (0.6.0): findings and fixes

Review of the new code (address exchange, hole punching, IPv6) found and fixed:

1. **A contact could make the phone dial arbitrary addresses (medium, fixed).** Punch
   targets came from the contact; private ranges were allowed, so a malicious contact
   could make the phone knock on LAN devices (router admin page, NAS) from port 7828.
   Now punch targets must be public: 10/8, 172.16/12, 192.168/16, 100.64/10,
   loopback, link-local, multicast, ULA and IPv4-mapped addresses are refused.
2. **Unbounded punch rounds (low, fixed).** The answering side started a dialing round
   for every offer. Now at most one round per contact per 30 s, at most 6 targets.
3. **IPv6 addresses could reveal the device's MAC address (low, fixed).** EUI-64
   addresses (…ff:fe…) and deprecated ones are never shared; temporary (privacy)
   addresses are preferred.
4. **Contacts deleted on restart (data loss, fixed; found by the NAT simulation).** A
   startup filter for leaked test fixtures removed real contacts whose name was still
   the generic "peer" and had no saved address. Contacts with a pinned key or pair
   secrets are never treated as fixtures (regression test added).

Privacy of the new feature: your public IP address goes only to trusted contacts,
inside the end-to-end encrypted session, and only while *Try direct internet
connections* is on (default on; off = relay only). The relay already sees it. No
port is opened for listening: hole punching only dials out.

Remaining INFO (by design): the relay answers "nobody listening" for a mailbox
(only computable by the two contacts, so it reveals presence only to the contact),
and it sees IPs, timing and volume.

## What remains (by design)

- The relay operator sees IP addresses, when phones are online and how much
  they send; it can drop or delay traffic. Use a server you control.
- Anyone with the relay code can use the relay's bandwidth (not read or join
  conversations). Keep it private; rotate by deleting `access.key`.
- Data on the phone (contacts, chat history, keys) is protected by file
  permissions, not encrypted at rest; a rooted/compromised phone exposes it.
- On the LAN, discovery announces (name, addresses) are visible to others on
  that network (signed, not encrypted).
- With *Try direct internet connections* on, contacts learn your public IP address
  (that's how a direct connection works). Turn it off to use the relay only.
