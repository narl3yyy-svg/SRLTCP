# Security audit and penetration test (v0.5.0)

Scope: the whole app — local web UI/API, peer protocol, file transfers and shared
folders, discovery, pairing, group chats, the local call relay, the Android
wrapper — and the new internet relay (server, clients, rendezvous, privacy).

How: automated, repeatable attack harnesses that start their own nodes and
relays and actually perform each attack, plus code review by separate
reviewers for Python, the web UI and Android, plus end-to-end simulations
on real WireGuard in network namespaces.

```bash
.venv/bin/python scripts/pentest/pentest_local.py   # app surfaces
.venv/bin/python scripts/pentest/pentest_relay.py   # internet relay
sudo bash scripts/simulate_internet.sh              # VPN off/on, relay, calls
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
| A trusted but malicious contact | send messages/files, misuse rendezvous | escape the download folder, read other folders, forge group messages, hijack the pair's relay path |
| Another app on the phone / page in a browser | reach 127.0.0.1 | use the local API without the token, CSRF, DNS rebinding, the JS bridge |

## Results — app surfaces (`pentest_local.py`)

**58 PASS, 0 FAIL** after fixes (first run: 57 PASS, 1 FAIL).

| Area | Attacks | Result |
|---|---|---|
| Local web API | all 55 routes unauthenticated, wrong token, token in query on API, timing of token compare, DNS rebinding (Host), CSRF (Origin/Referer), WebSocket without token / cross-origin, CORS, oversized and malformed JSON, path traversal (browse, folder delete, transfer id, share download), CSP | defended |
| Peer TCP port | every session message type before a handshake (with and without a fake ENCRYPTED flag), malformed/short/unknown frames, hash/key mismatch, pinned-key mismatch, blocked peer, connection cap, handshake deadline | defended |
| Untrusted but valid identity | FILE_OFFER, SHARE_REQUEST, CALL_SIGNAL, GROUP_TEXT | no side effects; one contact request |
| Malicious trusted contact | filenames with `../`, absolute, `..\`, NUL; chunk replay onto the sender's own file; offset beyond size; SHA mismatch; share fetch traversal and symlink escape; forged group message | defended |
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

## Results — internet relay (`pentest_relay.py`)

**36 PASS, 0 FAIL** (plus tests/test_relay.py and the namespace simulation).

| Attacker | Attacks | Result |
|---|---|---|
| Internet stranger at the relay port | no/wrong access key, wrong protocol version (same answer as a wrong key: no oracle), commands before HELLO, slow TLS/HELLO (15 s deadline), TLS 1.2 downgrade, non-TLS junk, oversized frames, frame-length lies, 1000+ mailbox subscriptions, subscription storms, send floods, slow-reading receiver, total and per-IP connection caps | defended |
| Relay operator (has the certificate and access key) | read traffic, find names/identity hashes/public keys/message types/plaintext in relayed bytes, inject junk/huge/unknown-mailbox packets, replay genuine packets, reflect a client's own packets back, deliver one contact's packets on another's mailbox, impersonate a contact on its mailbox | defended — sees only padded opaque blobs (256 B buckets observed), rotating tokens |
| Network attacker between phone and relay | impersonate the relay | refused: certificate pin checked before the access key is sent (0 HELLO frames reached the fake relay) |
| Malicious trusted contact | bogus/oversized/host-injection relay codes in RENDEZVOUS, rapid contribution churn, pointing the pair at its own relay, a different identity answering a direct-upgrade attempt | defended / bounded (see below) |
| Untrusted peer | RENDEZVOUS | dropped (trust gate) |
| Another node | replay a handshake captured for someone else | refused (v4 recipient binding) |
| Local API | read pair secrets or relay keys via /api/trusted, /api/status, settings | not exposed (only your own relay code, token-protected) |

Hardening applied from the INFO notes:

- A contact's relay code may no longer point at link-local, multicast, reserved or
  unspecified addresses (LAN and loopback stay allowed — self-hosted relays live
  there). Even before, the certificate pin stopped any data from reaching a wrong
  endpoint; this removes the "make the phone probe an internal address" angle.
- A contact can change its relay details at most once a minute, so a malicious
  contact can't turn RENDEZVOUS floods into constant contact-file rewrites.

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
