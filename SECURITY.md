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

SRLTCP is designed for **peer-to-peer, end-to-end encrypted** communication over TCP, USB serial, and optional **hub relay** paths.

### What is protected

- **Peer authentication** — every handshake proves possession of the Ed25519 key whose SHA-256 prefix is the peer's hash ID; pinned contact keys must match. Handshake signatures cover a timestamp and are checked against a replay cache.
- **Message and file payloads** are encrypted with AES-256-GCM after an Ed25519-signed ephemeral X25519 key exchange (forward secret per session).
- **Session keys** are derived via HKDF-SHA256 from the DH secret, salted with both ephemeral keys, with direction-specific labels.
- **Replay / reordering** — 64-bit counter nonces with a sliding replay window; the message type and flags are authenticated as AAD. Unencrypted application messages are dropped.
- **Trust gate** — only trusted contacts' messages, files and share requests are processed; others become contact requests.
- **Calls** — WebRTC DTLS-SRTP between the devices; SDP (with DTLS fingerprints) is exchanged inside the authenticated session. No STUN/TURN unless you configure one.
- **Address updates** — a contact's stored address is only changed by a fresh (≤ 5 min old) announce signed by that contact's key; replayed old announces are ignored.
- **Pairing codes** — signed contact cards; a one-time token (15 min) lets the scanner be trusted back automatically.
- **Hub forwarding** wraps opaque E2EE blobs. Hub servers never receive session keys and cannot decrypt chat or file content.
- **Hub registration** is signed with the client's Ed25519 identity; the hub rejects unsigned or mismatched registrations.
- **LAN UDP announces** (port 7826) are signed with the sender's Ed25519 identity key. Receiving peers verify the signature and `hash_id` ↔ `public_key` binding before displaying or trusting a discovered peer. Unsigned or invalid announces are dropped.
- **Optional Noise XX handshake** — Settings → Network → Handshake protocol. Uses `Noise_XX_25519_ChaChaPoly_SHA256`; session keys are HKDF-derived from the Noise cipher-state keys, and each side signs the handshake hash with its Ed25519 identity (versions before 0.2.0 derived keys from the public handshake hash — do not use them). Both peers must select the same protocol.
- **Web UI** is served over **HTTPS on localhost only** (self-signed cert generated locally) and every request needs the per-install access token (`~/.srltcp/web_token`, mode 600) — so other apps/users on the device cannot use the API.
- **Share sessions** use constant-time token comparison; path traversal is blocked on file APIs.

### What is not protected

| Data | Visibility |
|------|------------|
| LAN UDP discovery (7826) | Plaintext names, IPs, ports on the local network — **signed** (Ed25519) since v0.1.57; spoofing without the private key is rejected |
| Hub presence | Hub operator sees who registered (hash ID prefix, display name) and when |
| Hub routing | Hub sees source/destination identity hash tokens and approximate packet sizes/timing |
| WAN / hub TCP | Observers see connection endpoints, timing, and volume — not decrypted payloads |
| Local disk | Settings, trusted-peer list, chat history, identities stored without extra encryption |
| Android WebView | Pins the app's own localhost certificate (SHA-256) |
| NFC pairing | Contact cards (public data) are exchanged in the clear over a few centimetres |
| VPN discovery | With discovery on (or after pressing Announce), your signed announce (name, hash ID, public key, addresses) is sent to every address in your VPN subnet, and you answer announces from inside that subnet |
| Call relay (TURN/STUN) | Only if you configure one: it sees both devices' IP addresses and traffic volume, never call content |

### Connection modes

| Mode | When to use | Client port-forward? |
|------|-------------|----------------------|
| **LAN P2P** | Same network | No |
| **Hub** | Internet users without router access | No (clients dial out to hub) |
| **Direct WAN** | You control port-forward on TCP 7825 | Yes, on the reachable peer |

Hub mode is recommended for non-technical users. Direct WAN remains available for advanced setups.

### Hub operator trust

Running a hub means you can observe:

- Which identity hashes are online
- When peers connect and disconnect
- Encrypted traffic volume and timing between registered clients

You **cannot** read message or file content. Users should only use hubs they trust for availability and metadata privacy, the same way they trust any network relay.

### Threat assumptions

- Peers on the same LAN or serial bus may observe traffic patterns.
- Users must **verify peer identity** (32-char hash ID) out-of-band before trusting a contact.
- LAN announce signatures authenticate discovery metadata but do **not** encrypt it — anyone on the LAN can still observe that you exist and read names/endpoints.
- Hub presence signatures reduce spoofing but do not replace out-of-band identity verification before trusting.
- Blocked peers are rejected at the application layer but may still appear in discovery or hub presence until TTL expires.
- Noise XX is experimental; mismatched handshake protocols between peers cause connection failure by design.

## Hardening Guidelines for Operators

### All platforms

- Only trust peers whose hash IDs you have verified independently.
- Keep SRLTCP updated; crypto and handshake behavior may change between minor versions.
- Run the web UI on localhost; **do not** expose port **9876** to the internet.
- File chunks use the same session keys as chat — larger TCP chunks do not reduce encryption strength.
- Shared-folder grants are recipient-bound and time-limited; use E2EE peer share for sensitive folders.

### Hub server (Bob)

- Forward only **TCP 7825** (or your chosen hub port) to the hub machine.
- Run the hub on a host you control; keep the OS and Python runtime patched.
- Do not modify hub code to log `RELAY_ENVELOPE` bodies — they are opaque by design, but logging metadata still harms user privacy.
- Use a stable DNS name and TLS-terminated reverse proxy only if you understand the trade-offs; native SRLTCP hub traffic is already E2EE inside envelopes.

### Hub clients (Alice, John)

- Configure **Settings → Network → Connect via hub server** with Bob's public host and port.
- Click **Announce** after connecting to register presence on the hub.
- You only see other peers registered on the **same** hub.
- No router port-forward is required on client devices.

### Direct WAN (advanced)

- Forward only **TCP 7825** on the machine that receives inbound connections.
- Verify peer hash IDs before trusting manual WAN endpoints.
- WAN dials are rate-limited (1/s per endpoint) to reduce connection storms.

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