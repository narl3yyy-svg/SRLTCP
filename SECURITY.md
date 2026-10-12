# Security Policy

## Supported Versions

| Version | Supported |
| ------- | --------- |
| 0.3.x | :white_check_mark: |
| 0.2.x | :white_check_mark: (upgrade recommended) |
| < 0.2.0 | :x: (impersonation and Noise-mode key derivation flaws; upgrade) |

Security fixes are released on `main` and tagged as `v*`. Use the latest release when possible.

## Reporting a Vulnerability

If you discover a security issue in SRLTCP, please report it responsibly.

1. **Do not** open a public GitHub issue for undisclosed vulnerabilities.
2. Open a **private security advisory** on GitHub (preferred) or contact the maintainer with:
   - A clear description of the issue
   - Steps to reproduce
   - Impact assessment (confidentiality, integrity, availability)
   - Suggested fix if you have one
3. Allow up to **72 hours** for an initial response.

We will acknowledge valid reports, work on a fix, and coordinate disclosure once a patched release is available.

## Security Model

SRLTCP is designed for **peer-to-peer, end-to-end encrypted** communication over TCP (LAN, VPN, WAN), USB serial, and an optional self-hosted **internet relay**.

### What is protected

- **Peer authentication** — every handshake proves possession of the Ed25519 key whose SHA-256 prefix is the peer's hash ID; pinned contact keys must match. Handshake signatures (v4) cover the ephemeral key, a timestamp, the sender **and the intended recipient**, and are checked against a replay cache — a handshake can't be replayed to another node.
- **Message and file payloads** are encrypted with AES-256-GCM after an Ed25519-signed ephemeral X25519 key exchange (forward secret per session).
- **Session keys** are derived via HKDF-SHA256 from the DH secret, salted with both ephemeral keys, with direction-specific labels.
- **Replay / reordering** — 64-bit counter nonces with a sliding replay window; the message type and flags are authenticated as AAD. Unencrypted application messages are dropped.
- **Trust gate** — only trusted contacts' messages, files and share requests are processed; others become contact requests.
- **Calls** — WebRTC DTLS-SRTP; SDP (with DTLS fingerprints) is exchanged inside the authenticated session, and the media packets are carried inside the session too (a per-device TURN relay bound to the device itself, with per-call credentials, accepting traffic only for the contact being called). No outside STUN/TURN unless you configure one.
- **Address updates** — a contact's stored address is only changed by a fresh (≤ 5 min old) announce signed by that contact's key and newer than any announce seen from it; replayed announces are ignored.
- **Group chats** — no group key or server: every group message is signed by its author (Ed25519) and sent over the pairwise sessions; the creator relays to members who aren't each other's contacts but cannot alter or forge messages. Member lists are versioned and only accepted from the creator; invites only from trusted contacts; group traffic only from current members. The creator (and every member) sees all group messages and the member list, including members' public keys and names.
- **Pairing codes** — signed contact cards; a one-time token (15 min) lets the scanner be trusted back automatically.
- **Internet relay** (self-hosted, untrusted): TLS 1.3 with the certificate pinned by the relay code and an access key; phones subscribe to daily-rotating mailbox tokens derived from a per-contact secret (exchanged inside the E2EE session), so the relay never learns names, identity hashes or keys; every relayed packet is sealed again with the pair key (ChaCha20-Poly1305, per-direction keys, replay window) and padded to 256 B / 4 KiB buckets. A relay mailbox accepts only the contact it belongs to. The relay stores nothing. Details: docs/INTERNET_RELAY.md.
- **Pair secrets** live in `trusted.json`, written atomically with owner-only permissions, and are never returned by the local API.
- **Unauthenticated connections** — TCP connections that don't complete a handshake within 30 s are closed, open connections are capped, and frame parsing is linear in the input, so junk traffic can't stall the node.
- **LAN UDP announces** (port 7826) are signed with the sender's Ed25519 identity key. Receiving peers verify the signature and `hash_id` ↔ `public_key` binding before displaying or trusting a discovered peer. Unsigned or invalid announces are dropped.
- **Optional Noise XX handshake** — Settings → Network → Handshake protocol. Uses `Noise_XX_25519_ChaChaPoly_SHA256`; session keys are HKDF-derived from the Noise cipher-state keys, and each side signs the handshake hash with its Ed25519 identity (versions before 0.2.0 derived keys from the public handshake hash — do not use them). Both peers must select the same protocol.
- **Web UI** is served over **HTTPS on localhost only** (self-signed cert generated locally) and every request needs the per-install access token (`~/.srltcp/web_token`, mode 600) — so other apps/users on the device cannot use the API.
- **Share sessions** use constant-time token comparison; path traversal is blocked on file APIs.

### What is not protected

| Data | Visibility |
|------|------------|
| LAN UDP discovery (7826) | Plaintext names, IPs, ports on the local network — **signed** (Ed25519) since v0.1.57; spoofing without the private key is rejected |
| Direct internet connections | Your contacts learn your public IP address (shared inside the encrypted session, only with trusted contacts, only while the setting is on). Turn it off for relay-only |
| Internet relay | The relay operator sees your IP address, when you're connected, and roughly how much you send and when; it can delay or drop traffic. It can't see names, identities, who you talk to (beyond guessing from timing), content or message types |
| WAN / relay TCP | Observers see connection endpoints, timing, and volume — not decrypted payloads |
| Relay code | Contains the access key: anyone with it can use your relay's bandwidth (but can't read or join anyone's conversations). Rotate by deleting `access.key` |
| Local disk | Settings, trusted-peer list, chat history, identities stored without extra encryption |
| Android WebView | Pins the app's own localhost certificate (SHA-256); user-installed CAs are not trusted for it; the JavaScript bridge only answers the app's own page |
| NFC pairing | Contact cards (public data) are exchanged in the clear over a few centimetres |
| VPN discovery | With discovery on (or after pressing Announce), your signed announce (name, hash ID, public key, addresses) is sent to every address in your VPN subnet, and you answer announces from inside that subnet |
| External call relay (TURN/STUN) | Only if you configure one (not needed since 0.3.1): it sees both devices' IP addresses and traffic volume, never call content |

### Connection modes

| Mode | When it's used | Port-forward? |
|------|----------------|---------------|
| **LAN / VPN direct** | Whenever it works (always tried first) | No |
| **Direct across networks (hole punching)** | Two networks whose routers allow it, with *Try direct internet connections* on; addresses exchanged only inside the E2EE session, connection authenticated by the signed handshake | No (dials out only) |
| **Internet relay** | Automatically when there's no direct path | Only on the relay server (TCP 7827) |
| **Direct WAN** | Advanced: you forward TCP 7825 | Yes, on the reachable peer |

### Threat assumptions

- Peers on the same LAN or serial bus may observe traffic patterns.
- Users must **verify peer identity** (32-char hash ID) out-of-band before trusting a contact.
- LAN announce signatures authenticate discovery metadata but do **not** encrypt it — anyone on the LAN can still observe that you exist and read names/endpoints.
- Pair locally (or compare the hash ID out of band); the relay is only used between contacts that are already paired.
- Blocked peers are rejected at the application layer but may still appear in discovery until the entry expires.
- Noise XX is experimental; mismatched handshake protocols between peers cause connection failure by design.

## Hardening Guidelines for Operators

### All platforms

- Only trust peers whose hash IDs you have verified independently.
- Keep SRLTCP updated; crypto and handshake behavior may change between minor versions.
- Run the web UI on localhost; **do not** expose port **9876** to the internet.
- File chunks use the same session keys as chat — larger TCP chunks do not reduce encryption strength.
- Shared-folder grants are recipient-bound and time-limited; use E2EE peer share for sensitive folders.

### Internet relay server

- Open only the relay port (TCP 7827, or 443) on the server; never port 9876.
- Run it as an unprivileged user with the sandboxed systemd unit in `scripts/srltcp-relay.service`; keep the OS and Python patched.
- Keep the relay code private (it has the access key); rotate it by deleting `access.key` and restarting.
- Don't add logging of tokens or packet sizes — the relay only logs counters by design.

### Direct WAN (advanced)

- Forward only **TCP 7825** on the machine that receives inbound connections.
- Verify peer hash IDs before trusting manual WAN endpoints.

### Android

- Build APKs locally from a trusted source checkout; verify `scripts/sync-android-python.sh` ran before Gradle build.
- Grant **USB permissions** only to known devices (serial is disabled in the app, but permissions remain declared).
- Grant **notification permission** on Android 13+ so the foreground service can keep the node alive.
- The embedded WebView trusts only `127.0.0.1` / `localhost` self-signed TLS.
- App data (identities, settings) lives in the app files directory; uninstall clears it.

## Builds & releases

- **No GitHub Actions CI** — run `bash scripts/check.sh` locally before tagging (`ruff`, `mypy`, `pytest`).
- **APK builds are local** — Gradle + Chaquopy on your machine (see `android/README.md`). Release APKs are uploaded to GitHub Releases manually.
- Report supply-chain concerns through the vulnerability channel above.

## Dependency Security

Runtime dependencies are declared in `pyproject.toml` with minimum versions (`aiohttp`, `aiofiles`, `cryptography`, `pyserial`, `zstandard`, `noiseprotocol`). Dev tools: `ruff`, `mypy`, `pytest`. Android adds Chaquopy-managed pip packages at APK build time.

Report dependency or supply-chain issues through the same vulnerability channel.