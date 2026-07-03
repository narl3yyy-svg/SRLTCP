# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.18  | :white_check_mark: |
| < 0.1.18 | :x:               |

## Reporting a Vulnerability

If you discover a security issue in SRLTCP, please report it responsibly.

1. **Do not** open a public GitHub issue for undisclosed vulnerabilities.
2. Email or open a private security advisory on GitHub (preferred) with:
   - A clear description of the issue
   - Steps to reproduce
   - Impact assessment (confidentiality, integrity, availability)
   - Suggested fix if you have one
3. Allow up to **72 hours** for an initial response.

We will acknowledge valid reports, work on a fix, and coordinate disclosure once a patched release is available.

## Security Model

SRLTCP is designed for **peer-to-peer, end-to-end encrypted** communication over TCP and serial links.

### What is protected

- **Message and file payloads** are encrypted with AES-256-GCM after an Ed25519-signed X25519 key exchange.
- **Session keys** are derived via HKDF-SHA256 with explicit salt and direction labels (`srltcp-v2-send` / `srltcp-v2-recv`).
- **Relay mode** wraps opaque E2EE blobs; relays do not hold session keys.
- **Web UI** is served over **HTTPS on localhost only** (self-signed cert generated locally).

### What is not protected

- **Metadata** (peer discovery announces, transport endpoints, timing) is visible on the LAN or serial link.
- **Localhost TLS** uses a locally generated certificate; browsers will warn until trusted manually.
- **Trusted peer list** is stored on disk without additional encryption.
- **Android WebView** accepts the localhost self-signed certificate by design.

### Threat assumptions

- Peers on the same LAN or serial bus are assumed to be able to observe traffic patterns.
- Users must **verify peer identity** (hash ID) out-of-band before trusting a contact.
- Blocked peers are rejected at the application layer but may still appear in discovery.

## Hardening Guidelines for Operators

- Only trust peers whose hash IDs you have verified independently.
- Keep SRLTCP updated; crypto and handshake behavior may change between minor versions.
- Run the web UI on localhost; do not expose port 9876+ to untrusted networks without a reverse proxy and proper TLS.
- For WAN use, forward only **TCP 7825** (encrypted messaging). Verify peer hash IDs before trusting manual endpoints.
- Shared-folder grants are recipient-bound and time-limited; only use E2EE peer share for sensitive folders.
- On Android, grant USB permissions only to known devices.

## Dependency Security

CI runs `ruff`, `pytest`, and dependency installation from pinned minimum versions in `pyproject.toml`. Report supply-chain concerns through the same vulnerability channel above.