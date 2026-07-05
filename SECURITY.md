# Security Policy

## Supported Versions

| Version | Supported |
| ------- | --------- |
| 0.1.49+ | :white_check_mark: |
| < 0.1.49 | :x: |

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

- **Message and file payloads** are encrypted with AES-256-GCM after an Ed25519-signed X25519 key exchange.
- **Session keys** are derived via HKDF-SHA256 with explicit salt and direction labels (`srltcp-v2-send` / `srltcp-v2-recv`).
- **Hub forwarding** wraps opaque E2EE blobs. Hub servers never receive session keys and cannot decrypt chat or file content.
- **Hub registration** is signed with the client's Ed25519 identity; the hub rejects unsigned or mismatched registrations.
- **Web UI** is served over **HTTPS on localhost only** (self-signed cert generated locally).
- **Share sessions** use constant-time token comparison; path traversal is blocked on file APIs.

### What is not protected

| Data | Visibility |
|------|------------|
| LAN UDP discovery (7826) | Plaintext names, IPs, ports on the local network |
| Hub presence | Hub operator sees who registered (hash ID prefix, display name) and when |
| Hub routing | Hub sees source/destination identity hash tokens and approximate packet sizes/timing |
| WAN / hub TCP | Observers see connection endpoints, timing, and volume — not decrypted payloads |
| Local disk | Settings, trusted-peer list, chat history, identities stored without extra encryption |
| Android WebView | Accepts the localhost self-signed certificate by design |

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
- Hub presence signatures reduce spoofing but do not replace out-of-band identity verification before trusting.
- Blocked peers are rejected at the application layer but may still appear in discovery or hub presence until TTL expires.

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

## GitHub / CI

- **Checks workflow** (`.github/workflows/checks.yml`) runs `ruff`, `mypy`, and `pytest` on every push/PR.
- **No APK is built in CI** — Android builds are local via Gradle + Chaquopy (see `android/README.md`). This avoids shipping reproducibility and NDK issues from cloud builders.
- Report supply-chain concerns through the vulnerability channel above.

## Dependency Security

Runtime dependencies are declared in `pyproject.toml` with minimum versions (`aiohttp`, `aiofiles`, `cryptography`, `pyserial`, `zstandard`). Dev tools: `ruff`, `mypy`, `pytest`. Android adds Chaquopy-managed pip packages at APK build time.

Report dependency or supply-chain issues through the same vulnerability channel.