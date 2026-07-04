# SRLTCP

[![Checks](https://github.com/narl3yyy-svg/SRLTCP/actions/workflows/checks.yml/badge.svg)](https://github.com/narl3yyy-svg/SRLTCP/actions/workflows/checks.yml)
[![Build Android APK](https://github.com/narl3yyy-svg/SRLTCP/actions/workflows/build-apk.yml/badge.svg)](https://github.com/narl3yyy-svg/SRLTCP/actions/workflows/build-apk.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

**SRLTCP** (Serial + Relay-Less TCP) is a fast, secure, peer-to-peer communication and file transfer system. It runs over **USB Serial** and **TCP/IP**, supports direct P2P mode, and optionally uses a lightweight **headless relay server** that routes traffic without decrypting end-to-end encrypted payloads.

**Current version:** 0.1.43

---

## Features

| Feature | Description |
|---------|-------------|
| **Dual transports** | TCP/IP networking + USB Serial (pyserial) |
| **P2P mode** | Direct encrypted links between peers on LAN or serial cable |
| **Relay mode** | Optional headless server forwards opaque E2EE envelopes |
| **Secure messaging** | Ed25519 identity + X25519 key exchange + AES-GCM |
| **Fast file transfer** | Chunked streaming (1 MiB TCP / 8 KiB serial), zstd on TCP, resume support |
| **Folder sharing** | E2EE peer shares + optional token-based HTTP API |
| **Drag-and-drop send** | Drop files onto a contact in the Web UI |
| **WAN / manual peers** | Host + port per trusted contact; encrypted TCP 7825 |
| **Web UI** | Localhost **HTTPS-only** chat UI (default port **9876**) |
| **Settings** | First-run wizard + persistent config (folders, retention, LAN IP) |
| **System stats** | CPU usage & temperature in the web UI status bar |
| **Trusted peers** | Trust-before-message security model |
| **Ping / RTT** | Latency in ms; serial link quality % (RTT-based estimate, not RF RSSI) |
| **Cross-platform** | Linux, macOS, Windows CLI + Android (python-for-android) |

---

## Architecture

SRLTCP uses a modular package layout inspired by [chatx5](https://github.com/narl3yyy-svg/chatx5):

```
srltcp/
  app.py                    # CLI entry point
  core/
    identity.py             # Per-transport Ed25519 identities
    discovery.py            # UDP/TCP peer discovery registry
    node.py                 # Top-level node (messaging + sharing)
    protocol/
      framing.py            # Length + CRC32 frames
      messages.py           # Binary message types
      crypto.py             # E2EE: Ed25519, X25519, AES-GCM
    messaging/              # Mixin-composed backend
      backend.py            # Orchestrator
      links.py              # Peer link map
      connect.py            # Handshake + session keys
      announce.py           # Discovery broadcasts
      queue.py              # Offline message queue
      transfer.py           # Chunked file transfer
      routing.py            # Relay routing table
      relay.py              # Opaque envelope forwarding
  transports/
    tcp.py                  # TCP listener + UDP discovery
    serial.py               # USB serial (pyserial)
  web/                      # Local UI (aiohttp + WebSocket)
  routes/                   # REST + share + WS routes
  utils/                    # Logging, files, platform helpers
android/                    # Buildozer + python-for-android (see android/README.md)
tests/                      # pytest suite
scripts/                    # Maintainer helpers (e.g. scripts/check.sh)
.github/workflows/          # CI: checks.yml, build-apk.yml
```

### Data flow diagram

```mermaid
flowchart TB
    subgraph Node["SRLTCP Node"]
        UI[Web UI HTTPS :9876]
        MB[MessagingBackend]
        ID[Identity Store]
        UI <-->|WebSocket| MB
        MB --> ID
    end

    subgraph Transports
        TCP[TCP :7825]
        SER[Serial USB]
        UDP[UDP Discovery :7826]
    end

    subgraph Crypto["E2EE Layer"]
        HS[Ed25519 + X25519 Handshake]
        AES[AES-GCM Payload Encryption]
        HS --> AES
    end

    MB <--> TCP
    MB <--> SER
    TCP --> UDP

    subgraph Relay["Optional Headless Relay :7827"]
        RT[Routing Table]
        FWD[Opaque Envelope Forward]
        RT --> FWD
    end

    MB -.->|RELAY_ENVELOPE| FWD
    FWD -.->|cannot decrypt| MB
```

### Wire protocol

Every transport uses the same framed binary protocol:

```
┌──────────┬──────────┬──────────┬─────────────────┐
│ SRL\x01  │ length   │ CRC32    │ payload         │
│ (magic)  │ (4 BE)   │ (4 BE)   │ (variable)      │
└──────────┴──────────┴──────────┴─────────────────┘
```

Payload structure:

```
┌──────────┬───────┬───────────┬─────┬──────────────┐
│ msg_type │ flags │ stream_id │ seq │ body         │
│ (1 byte) │ (1)   │ (4 BE)    │ (4) │ (JSON/binary)│
└──────────┴───────┴───────────┴─────┴──────────────┘
```

File chunks use a binary body: `transfer_id (16) + offset (8) + length (4) + data`.

---

## Security model

### Identity

Each transport (TCP, Serial) has its own **Ed25519 keypair**. The node hash ID is the first 32 hex chars of `SHA-256(public_key)` — similar to Reticulum-style addressing.

Identities are stored in `~/.srltcp/identities/` (or `%APPDATA%\SRLTCP` on Windows).

### End-to-end encryption

1. **Handshake** — Ephemeral X25519 keys, signed by Ed25519 identity keys
2. **Session keys** — HKDF-derived AES-256 keys (separate send/recv) via HKDF-SHA256 with labels `srltcp-v2-send` / `srltcp-v2-recv`
3. **Payloads** — AES-256-GCM with 12-byte nonces; all chat text, file offers, and metadata are encrypted (`Flags.ENCRYPTED | Flags.E2EE`)
4. **File chunks** — Each chunk is encrypted before it leaves your node; TCP may apply zstd compression **before** encryption (flag bit). Larger 1 MiB TCP chunks improve throughput only — they do **not** weaken encryption.

### How data is transferred

| Path | What travels on the wire | Encrypted? |
|------|--------------------------|------------|
| **TCP / WAN (port 7825)** | Framed binary protocol after handshake | Yes — payloads are opaque AES-GCM blobs |
| **USB Serial** | Same framed protocol over serial | Yes — identical E2EE session |
| **UDP discovery (7826)** | Peer announces (hash, name, endpoints) | No — discovery metadata is plaintext on LAN |
| **Web UI (9876)** | Browser ↔ local node over HTTPS | Localhost TLS only; chat/file APIs proxy local data |
| **Relay (optional 7827)** | `RELAY_ENVELOPE` route token + opaque blob | Relay sees routing metadata only, not content |

**File transfer flow:** upload to local staging → `FILE_OFFER` (encrypted JSON) → `FILE_ACCEPT` → encrypted `FILE_CHUNK` stream → `FILE_COMPLETE` with SHA-256 verify. The receiver writes to your configured incoming folder; the Web UI serves completed files from disk for preview/download.

**WAN use:** Forward **TCP 7825** to your node. Traffic after handshake is E2EE. An observer on the internet can still see connection timing, packet sizes, and your public IP — verify peer hash IDs out-of-band before trusting. Set WAN host/port per contact (Add contact or contact menu → WAN).

### Relay privacy

In relay mode, the server forwards `RELAY_ENVELOPE` packets containing only:

- A 16-byte route token (destination hash prefix)
- An opaque encrypted blob

The relay **never receives session keys** and cannot decrypt message or file content.

### What is not encrypted

- LAN/UDP discovery announces (names, IPs, ports)
- Connection metadata (who talks to whom, when, approximate sizes)
- Local settings, trusted-peer list, and chat history on disk
- Self-signed localhost HTTPS certificate (browser trust is manual)

See [SECURITY.md](SECURITY.md) for vulnerability reporting and operator hardening.

### Web UI hardening (v0.1.1+)

- **HTTPS only** on `127.0.0.1` — auto-generated 4096-bit localhost certificate
- **TLS 1.2+** with modern cipher suites; no cleartext HTTP
- **Localhost-only binding** — refuses non-loopback Host headers
- **Security headers** — CSP, HSTS, `X-Frame-Options: DENY`, `no-referrer`
- **Origin validation** on POST requests and WebSocket connections
- **Path traversal protection** on file/share APIs
- **Constant-time** token comparison for share sessions

### First-run setup

On first launch, the web UI shows a setup wizard. Settings persist in `~/.srltcp/settings.json`:

| Setting | Description |
|---------|-------------|
| Display name | Shown to peers on the network |
| Web port | HTTPS port (default **9876**); restart to apply |
| Message retention | Hours to keep local chat history |
| Incoming files folder | Where received files are saved |
| Shared folder | Default folder for browse/share |
| LAN IP | Pinned interface for discovery & announce |
| Auto-announce | Broadcast presence every 5 seconds (LAN only) |
| WAN port-forward | Acknowledge you will forward TCP **7825** for internet peers |
| Timezone | Region for the status clock (time shown at top of sidebar) |
| Show clock | Toggle live clock in the UI |

Change port from CLI: `./run.sh web --port 9999`

---

## Installation

### Linux / macOS

```bash
git clone https://github.com/narl3yyy-svg/SRLTCP.git
cd SRLTCP
./run.sh web
# Verbose backend logs:
./run.sh web --debug
```

Open **https://127.0.0.1:9876** in your browser (self-signed cert — accept once for localhost).

Press **Ctrl+C** in the terminal to shut down cleanly.

For USB serial on Linux, add your user to the `dialout` group:

```bash
sudo usermod -aG dialout $USER
# log out and back in
./run.sh web --serial
```

### Windows

```cmd
git clone https://github.com/narl3yyy-svg/SRLTCP.git
cd SRLTCP
run.bat web
```

Requires [Python 3.12+](https://www.python.org/downloads/) with **Add to PATH** checked.

### pip install

```bash
pip install -e .
srltcp web
```

### Android (python-for-android)

See [android/README.md](android/README.md). The APK is built with **Buildozer** + **python-for-android** (Chaquopy was removed in v0.1.20). Targets **Android 15** (API 35), **arm64-v8a**.

**Local build:**

```bash
cd android
buildozer android debug
adb install -r bin/*debug*.apk
```

**CI / releases:** Push to `main` or tag `v*` (or run **Build Android APK** workflow) — APK is attached to [GitHub Releases](https://github.com/narl3yyy-svg/SRLTCP/releases) on tags. CI builds **arm64-v8a only** (avoids armeabi-v7a `grpmodule` failures on GitHub runners).

**Troubleshooting:**

| Symptom | What to do |
|---------|------------|
| CI build fails at `grpmodule.o` | Ensure workflow uses `--arch arm64-v8a` and a clean `android/.buildozer` |
| App closes on launch | `adb logcat -s SRLTCP python:D PythonService:D` |
| White screen | Wait 10–30s for Python service to bind HTTPS; app tries ports 9876–9878 |
| No serial on Android | Expected — serial transport is disabled on Android |
| No peers after reinstall | Uninstall clears app data (identities in app files dir) |
| No discovered peers | **Announce TCP** on both devices, or **Add Contact** with hash ID |
| Phantom trusted peers (`peer`, `deadbeef…`) | Leftover pytest fixtures — restart SRLTCP (v0.1.41+ auto-removes them) |

```bash
adb logcat -s SRLTCP python:D PythonService:D
adb shell am start -n org.srltcp.srltcp/org.srltcp.app.MainActivity
```

---

## Usage

### P2P mode (default)

Start the web UI on two machines on the same LAN:

```bash
# Machine A
./run.sh web --name "alice"

# Machine B
./run.sh web --name "bob"
```

1. Click **Announce** on both machines
2. Select a discovered peer in the sidebar
3. Send encrypted messages in the chat panel

### Headless relay server

Run a 24/7 relay on a VPS or always-on LAN host:

```bash
./run.sh relay --bind 0.0.0.0 --port 7827
```

Clients connect through the relay with E2EE — the relay only routes opaque envelopes:

```bash
./run.sh web --relay --name "client-a"
```

### USB Serial P2P

Connect two machines via USB-serial cable (or USB-OTG):

```bash
# Machine A
./run.sh web --serial --serial-port /dev/ttyUSB0 --no-tcp

# Machine B
./run.sh web --serial --serial-port /dev/ttyACM0 --no-tcp
```

### CLI messaging

```bash
srltcp send --recipient <hash_id> --text "Hello" --host 10.0.0.5
```

### File transfer (API)

```bash
# Send a file to a connected peer
curl -k -X POST https://127.0.0.1:9876/api/transfer \
  -H 'Content-Type: application/json' \
  -d '{"recipient_hash":"<hash>","path":"/path/to/large.iso"}'

# List transfers
curl -k https://127.0.0.1:9876/api/transfers
```

Transfers are **resumable** — if interrupted, the receiver's partial file offset is used on resume via `FILE_RESUME`.

In the web UI, images and videos preview in chat during transfer. Click to enlarge in a lightbox; use **Download** to save the file. The transfer dock (progress bar above the composer) hides automatically when no transfers are active. **Drag files** from your file manager onto a contact in the sidebar to send. Both peers must run **v0.1.17+** for shared-folder limits, revoke, and WAN features.

### E2EE shared folder (recommended)

Share a folder with a **trusted, connected** peer over the encrypted link — no plaintext folder listing on the network.

**Owner (machine A):**

1. Open **Settings → Folders** and set **Default shared folder** (or use the default `~/.srltcp/shared`).
2. Trust and connect to the peer on the LAN (or WAN — see below).
3. Open the chat with that peer → click the **folder icon** in the header → set **time limit** and **download limit** → **Offer shared folder**.
4. The peer receives an encrypted grant bound to their hash ID (enforced server-side).

**Remove a share:** In the share modal, under **Your active offers**, click **Remove** next to any grant.

**Download limits:** Choose 1, 2, 5, 10, 25, or unlimited downloads per grant. Each file or ZIP counts as one download.

**Time limits:** 1 minute, 5 minutes, 1 hour, 1 day, 1 week, or forever.

**Folder download:** Click **Download as ZIP** next to any folder in the browse view — the sender compresses it before transfer.

**Recipient (machine B):**

1. When connected, open **Share folder** for that contact.
2. Select the offered grant → browse files (listing arrives over E2EE).
3. Click a file to start a **secure file transfer** (same encrypted pipeline as chat attachments).

**API (peer share):**

```bash
# Offer folder to trusted peer (must be connected)
curl -k -X POST https://127.0.0.1:9876/api/share/peer/offer \
  -H 'Content-Type: application/json' \
  -d '{"recipient_hash":"<peer_hash>","path":"/home/user/shared"}'

# List remote folder (async — results via WebSocket share_listing)
curl -k -X POST https://127.0.0.1:9876/api/share/peer/list \
  -H 'Content-Type: application/json' \
  -d '{"owner_hash":"<owner_hash>","grant_id":"<grant_id>"}'

# Request file download
curl -k -X POST https://127.0.0.1:9876/api/share/peer/fetch \
  -H 'Content-Type: application/json' \
  -d '{"owner_hash":"<owner_hash>","grant_id":"<grant_id>","path":"docs/readme.txt"}'
```

### Legacy HTTP share sessions (localhost only)

For local tooling, token-based HTTP browse remains available on the **localhost HTTPS** UI only:

```bash
curl -k -X POST https://127.0.0.1:9876/api/share/create \
  -H 'Content-Type: application/json' \
  -d '{"path":"/home/user/shared"}'

curl -k "https://127.0.0.1:9876/api/share/<session_id>/list?token=<token>"
```

### WAN / manual peer connections (internet)

SRLTCP does **not** broadcast your node to the public internet. WAN connectivity is **opt-in and manual** per trusted contact.

#### Step 1 — Expose the encrypted messaging port (owner side)

SRLTCP listens on **TCP 7825** by default for encrypted P2P messaging (handshake + E2EE payloads). This is **not** a VPN; it is an application-level encrypted channel similar in spirit to a WireGuard tunnel endpoint, without routing all system traffic.

1. On the machine that will **receive** inbound WAN connections, open **Settings → Network**.
2. Enable **I will port-forward TCP 7825 for WAN peers** (documents your intent).
3. On your router/firewall, forward **TCP 7825** → that machine's LAN IP.
4. Note your **public IP** or a **DNS name** pointing to it (e.g. `home.example.com`).

**Safety:** Only forward **7825**, not the Web UI port (9876). The web UI stays on **localhost HTTPS** only.

#### Step 2 — Configure the remote peer (dialer side)

1. Trust the peer whose hash ID you verified **out-of-band** (in person, phone, etc.).
2. Right-click the trusted contact → **WAN / manual endpoint**.
3. Enter **Host or domain** (public IP or FQDN) and **TCP port** (default 7825).
4. Enable **WAN endpoint** and choose connection mode:
   - **Auto** — try LAN discovery first, then WAN if enabled
   - **LAN only** — never use the WAN endpoint
   - **WAN only** — dial only the manual endpoint (useful when off-LAN)
5. Save and open the chat — SRLTCP dials the endpoint and completes the same E2EE handshake as on LAN.

#### Security precautions

| Risk | Mitigation |
|------|------------|
| Connecting to the wrong host | Verify peer **hash ID** before trusting; WAN host is stored per contact |
| Private IP as WAN endpoint | Rejected — use LAN mode for `10.x` / `192.168.x` addresses |
| Localhost / loopback WAN | Rejected |
| DNS rebinding to private IP | Resolved address must be public |
| WAN connection storms | Outbound WAN dials are rate-limited (1/s per endpoint) |
| Web UI exposed to internet | **Do not** port-forward 9876; UI is localhost-only by design |
| Untrusted inbound traffic | Only trusted peers complete handshake; others are ignored after crypto verify |

**Both peers should run v0.1.17+** for WAN endpoint fields and share-folder messages.

---

## Performance notes

| Setting | Value | Rationale |
|---------|-------|-----------|
| Chunk size | 1 MiB TCP / 8 KiB serial | Higher LAN throughput; E2EE unchanged |
| Compression | zstd level 3, ≥ 64 KiB | Fast ratio for text/logs; skips already-compressed data |
| Frame CRC | CRC32 | Cheap integrity check per frame |
| Async I/O | aiofiles + asyncio | Non-blocking disk and network |
| Memory | Streaming only | Never loads full file into RAM |

**Tips for maximum speed:**

- Use wired Ethernet or USB 3 serial adapters at 115200+ baud
- Prefer direct P2P over relay (one less hop)
- Disable compression for pre-compressed archives (future: per-file flag)
- Run relay on a low-latency host close to all peers

---

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install  # optional
# Tests use SRLTCP_DATA_DIR automatically — do not run pytest against ~/.srltcp

./run.sh web                    # dev server
bash scripts/check.sh           # ruff + pytest
pytest tests/ -v                # unit tests only
```

### Project commands

| Command | Description |
|---------|-------------|
| `srltcp web` | Web UI + P2P node |
| `srltcp relay` | Headless relay server |
| `srltcp send` | One-shot CLI message |
| `srltcp identity` | Show local hash IDs |

---

## Changelog

See [srltcp/RELEASE_NOTES.md](srltcp/RELEASE_NOTES.md). Click the version badge in the status bar for release notes.

### v0.1.43

- **Android CI** — `android.archs` moved to `[app]` in buildozer.spec (fixes dual-arch + `buildozer android clean` crash)

### v0.1.42

- **Image preview** — screenshots/images show in chat only after transfer completes (sender and receiver); fixes broken serial/TCP previews
- **Serial link %** — smoothed RTT + EMA display (reduces 62↔82% flicker; still RTT-based, not RF RSSI)
- **Transfer UI** — removed bottom transfer dock; in-message progress bar retained
- **Add contact** — WAN host, port, enable, and connection mode fields
- **Settings UI** — compact folder browser; readable Restart button
- **Android CI** — `buildozer android clean` + spec-only `arm64-v8a` (no dual-arch `--arch` flag)
- **Docs** — expanded README security and data-transfer tables

### v0.1.41

- **Video receive fix** — MP4 preview only after transfer completes (partial files cannot play in browsers)
- **Transfer UI** — throttled progress updates; no full chat re-render every chunk
- **TCP throughput** — 1 MiB chunks, no artificial send delay (encryption unchanged)
- **Serial link %** — RTT-based estimate (554 ms ≈ 40–60% link, not misleading 100%)
- **Trusted list** — auto-removes pytest fixture peers (`deadbeef…`, `peer`, etc.)
- **Android CI** — arm64-v8a only build; clean `.buildozer` cache each run

### v0.1.35

- Manual **Announce TCP** / **Announce Serial** validate transport and show errors when discovery cannot send
- Android CI restored — APK build fails loudly if no `.apk` is produced

### v0.1.31

- Receiver **Download file** link when transfers complete; folder zips named `temp.zip`, `temp1.zip`, …
- Reconnect/connect timeout handling; Android CI SDK symlink fix

### v0.1.30

- Scrollable trusted-contact right-click menu on small screens
- **Delete** key removes the selected trusted peer
- Android 15 (API 35) APK; CI build fixed (arm64-v8a only)

### v0.1.17

- Share lifecycle limits, revoke, folder ZIP download; trusted peer list fix
- Receiver Save file on complete; media pan; Android crash fixes; 512 KiB chunks

### v0.1.16

- Drag-and-drop file send; E2EE shared folders; manual WAN peer endpoints (TCP 7825)
- Receiver transfer bar clears on complete; share/WAN WebSocket events; security hardening

### v0.1.15

- No UI flicker during transfers; transfer dock hides on receiver; media lightbox zoom
- Post-transfer connection cooldown; unread badges on trusted peers; improved notifications
- Trusted contact list readability; Android lifecycle fixes

### v0.1.14

- Stable large file transfers (no per-chunk fsync, async chunk handling, send lock)
- Media lightbox, copy/delete icons, transfer dock auto-hide, handshake polling
- HKDF explicit info strings; Android foreground service fixes

### v0.1.10

- Fixed DELETE contact (405), file transfer disconnects, reconnect backoff, serial handshake wait
- Tabbed settings window, transfer progress dock with cancel, `--debug` flag, NTP clock source

### v0.1.9

- Fixed connection storms and unstable reconnect loops; serial outbound connect
- Setup folder browse works above the wizard overlay; trusted peers hidden from Discovered
- Network map shows links; contact ⋮ menu (clear chat, rename, block, delete)
- Clock at top of sidebar (time only); timezone in settings; Android APK stability fixes

### v0.1.4

- Ctrl+C now exits promptly (WebSocket + transport teardown)
- APK build fixed for Chaquopy 15 Gradle DSL

### v0.1.3

- Handshake UI and reconnect fixes; RTT shown when connected
- Serial port and baud rate dropdowns (USB devices)
- Self-node removed from discovered peers; APK build fixed

### v0.1.2

- Fixed discovery spam; auto-announce off by default with passive discovery
- Trusted peers required for messaging and file transfer
- Ping/RTT (ms) and serial RF link quality (%)
- File upload via browser with progress bar and MB/s speed
- Settings: serial transport, folder pickers, retention presets, restart
- Identity create/regenerate/delete for TCP and serial

## Roadmap

- [ ] Multi-hop relay with source routing and loop prevention
- [ ] Android USB serial Chaquopy shim (full OTG support)
- [ ] Folder sync (bidirectional, incremental)
- [ ] Contact book with pinned peer hashes
- [ ] Noise protocol framework option for handshake
- [ ] QUIC transport backend
- [ ] Bandwidth limiting and QoS per transfer
- [ ] Signed release APK on GitHub Releases
- [ ] Desktop system tray wrapper (Tauri/Electron)

---

## License

GPL v3 — see [LICENSE](LICENSE).

## Acknowledgments

Architecture patterns adapted from [chatx5](https://github.com/narl3yyy-svg/chatx5). Identity hashing inspired by [Reticulum](https://reticulum.network/).