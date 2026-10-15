# SRLTCP

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

**SRLTCP** (Serial + Relay-Less TCP) is a fast, secure, peer-to-peer messenger for chat, voice and video calls, files and folder sharing. It runs over **TCP/IP** and **USB Serial**, connects directly on the same Wi-Fi, LAN or VPN, and reaches contacts on other networks over **Tor** (no server needed), **direct IPv6**, or your own optional **relay**, switching automatically. Every session is end-to-end encrypted with quantum-safe (post-quantum hybrid) cryptography, and your data is encrypted on the device.

**Current version:** 0.10.1 — update every device: 0.9 and later use a quantum-safe handshake (v6) and don't connect to 0.8.x or older; folder sync needs 0.10 on both sides.

---

## Features

| Feature | Description |
|---------|-------------|
| **Dual transports** | TCP/IP networking + USB Serial (pyserial) |
| **P2P mode** | Direct encrypted links between peers on LAN or serial cable |
| **Internet relay** | Paired contacts keep talking with the VPN off or on different networks; automatic switching between direct and relay; the relay is blind (TLS + pinned cert, rotating mailboxes, second encryption layer, padding) |
| **Tor (no server needed)** | Reach trusted contacts from any network without a server or port forwarding: each device publishes a private onion service that only its contacts can use. Bundled on Android; uses Tor Browser's Tor on a PC |
| **Direct across networks** | No port forwarding: one phone on the VPN and one on the home Wi-Fi connect directly, and two home networks connect directly through both routers (hole punching; over IPv6 straight from a Tor session, no relay needed). Addresses are exchanged only inside the encrypted session; Tor or the relay steps in when a network doesn't allow it |
| **Quantum-safe encryption** | Every session: X25519 + ML-KEM-768 key exchange, Ed25519 + ML-DSA-65 signatures, AES-256-GCM with replay protection |
| **Verified contacts** | 60-digit safety numbers over both identity keys; automatic when you pair in person (QR or NFC) |
| **Encrypted on the device** | Keys, contacts, chats, groups and settings sealed at rest (Android Keystore / Windows DPAPI); encrypted backup and restore |
| **Privacy options** | Disappearing messages, app lock and private lock-screen notifications (all off by default) |
| **Fast file transfer** | Windowed streaming (1 MiB chunks direct, smaller on Tor/relay/serial), zstd, verified while streaming; survives dropped connections and app restarts; several files at once; optional photo shrinking |
| **Folder sharing** | E2EE peer shares + optional token-based HTTP API |
| **Folder sync** | Keep a folder the same on your device and a contact's: two-way, incremental, conflicts kept as copies, deletes go to a 30-day trash |
| **Drag-and-drop send** | Drop files onto a contact in the Web UI |
| **Optional port forwarding** | Forward TCP 7825 if you like; never required (Tor, IPv6 and the relay cover other networks) |
| **Web UI** | Localhost **HTTPS-only** chat UI (default port **9876**), protected by a private access token |
| **Settings** | First-run wizard + persistent config (folders, retention, LAN IP) |
| **System stats** | CPU usage & temperature in the web UI status bar |
| **Pairing** | QR code, shareable link, or **NFC tap** (Android) — one scan pairs both sides |
| **Group chats** | End-to-end encrypted groups of up to 32; members who aren't each other's contacts still talk (the creator passes messages on, signed by their author so they can't be faked) |
| **Voice & video calls** | WebRTC; signalling and media both travel inside the encrypted SRLTCP session (built-in relay), so calls work wherever chat works — no third-party servers |
| **Offline delivery** | Messages queue while a contact is offline and deliver on reconnect, with ✓/✓✓ receipts |
| **Works over WireGuard** | VPN-aware discovery (unicast sweep + replies), Find by IP, automatic address updates |
| **Signed LAN discovery** | Ed25519-signed UDP announces — spoofed peers rejected |
| **Contact requests** | Unknown peers can't message you; they appear as "wants to chat" until you accept |
| **Chat history** | Saved locally, encrypted, pruned by your retention setting |
| **Trusted peers** | Trust-before-message security model |
| **Ping / RTT** | Latency in ms; serial link quality % (RTT-based estimate, not RF RSSI) |
| **Cross-platform** | Linux, macOS, Windows CLI + Android (Chaquopy APK) |

---

## Architecture

SRLTCP uses a modular package layout inspired by [chatx5](https://github.com/narl3yyy-svg/chatx5):

```
srltcp/
  app.py                    # CLI entry point (web, relay, send, identity, url)
  core/
    identity.py             # Per-transport identities (Ed25519 + ML-DSA-65)
    trusted.py              # Contacts: pinned keys, verification, pair secrets
    verify.py               # Safety numbers
    backup.py               # Encrypted backup / restore
    settings.py             # Settings (encrypted at rest)
    discovery.py            # UDP/TCP peer discovery registry
    node.py                 # Top-level node (messaging + sharing + call ringing)
    relay_server.py         # `srltcp relay` server
    protocol/
      framing.py            # Length + CRC32 frames
      messages.py           # Binary message types
      crypto.py             # Hybrid handshake, session keys, AES-GCM, replay window
      pq.py                 # ML-KEM-768 / ML-DSA-65 (cryptography or pure Python)
      signed.py             # Signed payload helpers
    messaging/              # Mixin-composed backend
      backend.py            # Orchestrator
      links.py              # Peer link map
      connect.py            # Handshake + session keys
      announce.py, ping.py  # Discovery broadcasts, RTT / dead-link detection
      pairing.py            # Contact cards (QR / link / NFC), pairing, verification
      store.py              # On-disk chat history
      transfer.py           # Windowed, resumable file transfer
      share_peer.py         # E2EE folder/file shares
      sync.py               # Two-way folder sync with a contact
      groups.py             # Group chats
      relay.py, punch.py    # Relay client, path switching, hole punching (IPv4/IPv6)
      tor.py                # Onion service, Tor dialing, parallel Tor lanes
      turn.py               # Built-in call relay (media through the session)
      disappear.py          # Disappearing messages
  transports/
    tcp.py                  # TCP listener + UDP discovery
    serial.py               # USB serial (pyserial)
  web/                      # Local UI (aiohttp + WebSocket, access token, calls.js)
  routes/                   # REST + share + WS routes
  utils/                    # Vault (encryption at rest), files, network, platform helpers
android/                    # Gradle + Chaquopy Android app (see android/README.md)
tests/                      # pytest suite
scripts/                    # check.sh, build-android.sh, simulate*.py, tor_testnet.sh, pentest/
```

## Security model

### Identity

Each transport (TCP, Serial) has its own **Ed25519 keypair** plus a post-quantum **ML-DSA-65** key (from its own random seed). The node hash ID is the first 32 hex chars of `SHA-256(Ed25519 public key)` — similar to Reticulum-style addressing.

Identities are stored, encrypted, in `~/.srltcp/identities/` (or `%APPDATA%\SRLTCP\identities` on Windows).

### End-to-end encryption

1. **Handshake (quantum-safe)** — fresh X25519 *and* ML-KEM-768 keys every session, signed (with a timestamp) by both identity keys: Ed25519 and ML-DSA-65. Each side checks that the peer's hash ID is derived from the key it presents and that both keys match the ones pinned for that contact, so contacts cannot be impersonated; replayed handshakes are rejected. Breaking one algorithm (with a quantum computer or a new attack) is not enough: the session key needs both secrets.
2. **Session keys** — HKDF-SHA256 from the X25519 and ML-KEM shared secrets (salted with both ephemeral keys and the ML-KEM transcript), separate keys per direction; new keys every session (forward secrecy)
3. **Payloads** — AES-256-GCM with 64-bit counter nonces and a sliding replay window; the message type and flags are authenticated. All chat text, receipts, file offers, call signalling and metadata are encrypted, and unencrypted application messages are rejected
4. **Trust** — only trusted contacts' messages, files and share requests are accepted; anyone else becomes a contact request
5. **File chunks** — Each chunk is encrypted before it leaves your node; it may be zstd-compressed **before** encryption (flag bit). Chunk size (1 MiB direct, 64 KiB Tor, 128 KiB relay) affects throughput only — not encryption.

### How data is transferred

| Path | What travels on the wire | Encrypted? |
|------|--------------------------|------------|
| **TCP / WAN (port 7825)** | Framed binary protocol after handshake | Yes — payloads are opaque AES-GCM blobs |
| **USB Serial** | Same framed protocol over serial | Yes — identical E2EE session |
| **UDP discovery (7826)** | Signed peer announces (hash, name, endpoints, Ed25519 signature) | Metadata is plaintext on LAN; signatures prevent spoofing |
| **Web UI (9876)** | Browser ↔ local node over HTTPS | Localhost TLS + access token; other local apps can't use the API |
| **Calls** | WebRTC media (DTLS-SRTP) carried inside the SRLTCP session via a local relay | Yes — double-encrypted: DTLS-SRTP inside the E2EE session |
| **NFC pairing** | Contact cards (public key, name, LAN address) | Public data; requires phones a few cm apart |
| **Internet relay (7827, TLS)** | Rotating mailbox tokens + padded blobs sealed with the pair key, inside the E2EE session | Yes — three layers; the relay sees only IPs, timing and rough volume |

**File transfer flow:** upload to local staging → (queued while the contact is unreachable) → `FILE_OFFER` (encrypted JSON) → `FILE_ACCEPT` → encrypted `FILE_CHUNK` stream, confirmed by `FILE_ACK` (a sliding window: the sender keeps a limited amount unconfirmed and resends from the last confirmed byte after a lost piece, a path change or a re-key) → `FILE_COMPLETE` with the SHA-256, computed while streaming → the receiver, which hashed while writing, answers with a final `FILE_ACK` (verified or not). The receiver writes strictly in order to your configured incoming folder; a cancelled or damaged download is deleted. Either side can cancel (`FILE_REJECT`). Over Tor the chunks are spread over 8 extra circuits ("lanes") joined with a session-key proof; chat stays on the main circuit; and both devices look for a direct path the moment a file starts.

**Port forwarding:** if you forward **TCP 7825** to your node, traffic after the handshake is still end-to-end encrypted; an observer on the internet can see connection timing, packet sizes and your public IP.

### Internet relay privacy

Over the internet, paired phones meet at a relay you run (`srltcp relay`). It is treated as untrusted:

- **TLS 1.3 with a pinned certificate.** The relay code contains the certificate's SHA-256, so nobody can impersonate the relay, and the network only sees TLS. An access key in the code keeps strangers from using it.
- **No identities.** Phones subscribe to *mailboxes*: random-looking tokens derived from a secret shared only by the two contacts, rotating daily. The relay never learns names, identity hashes or keys, and can't tell which contacts talk to each other except through IP addresses and timing.
- **Second encryption layer.** Every relayed packet is sealed again with a key only the two contacts have (ChaCha20-Poly1305, one key per direction), padded to 256-byte / 4 KiB buckets, and checked against replays. The relay sees opaque, similar-sized blobs and can't read message types, inject packets or replay them.
- **Inside:** the normal SRLTCP session (signed handshake with forward secrecy, AES-GCM).
- Nothing is stored on the relay. See [docs/INTERNET_RELAY.md](docs/INTERNET_RELAY.md).

### What is not encrypted

- LAN/UDP discovery announces (names, IPs, ports — signed but not encrypted)
- Relay metadata: the relay operator sees your IP address, when you are connected and roughly how much you send
- Connection metadata (who talks to whom, when)
- Received files in your incoming folder (so other apps can open them). Your keys, contacts, chats, groups and settings **are** encrypted on the device: with a hardware-backed Android Keystore key on phones, DPAPI on Windows, an owner-only key file on Linux/macOS
- Self-signed localhost HTTPS certificate (browser trust is manual; the Android app pins it)

See [SECURITY.md](SECURITY.md) for vulnerability reporting, the threat model and hardening; the latest penetration test is in [docs/SECURITY_AUDIT.md](docs/SECURITY_AUDIT.md).

### Web UI hardening

- **Access token** — every page/API/WebSocket request needs the per-install token (`~/.srltcp/web_token`); open the link printed at startup or run `srltcp url`
- **HTTPS only** on `127.0.0.1` — auto-generated localhost certificate (RSA 4096-bit on a PC, 2048-bit on Android; the app pins it)
- **TLS 1.2+** with modern cipher suites; no cleartext HTTP
- **Localhost-only binding** — refuses non-loopback Host headers
- **Security headers** — strict CSP (no inline scripts, no third-party requests), HSTS, `X-Frame-Options: DENY`, `no-referrer`
- **Origin validation** on POST requests and WebSocket connections
- **Path traversal protection** on file/share APIs
- **Constant-time** token comparison for share sessions

### First-run setup

On first launch, the web UI shows a setup wizard (or *Restore a backup* when moving from another device). Settings persist, encrypted, in `~/.srltcp/settings.json`:

| Setting | Description |
|---------|-------------|
| Display name | Shown to peers on the network |
| Web port | HTTPS port (default **9876**); restart to apply |
| Keep messages | 1 day … forever, or until restart |
| Incoming files folder | Where received files are saved |
| Shared folder | Default folder for browse/share |
| LAN IP | Pinned interface for discovery & announce |
| Auto-announce | Broadcast presence every 5 seconds (LAN/VPN only) |
| Tor | *Reach contacts anywhere over Tor*, on by default |
| Direct internet connections | Hole punching (IPv4 via a relay, IPv6 from Tor), on by default |
| Internet relay | Optional. Paste the code from `srltcp relay` on one phone; contacts get it automatically |
| Privacy | Disappearing messages, app lock, private notifications, backup / restore |
| Port forwarding | Advanced: acknowledge you forward TCP **7825** (never required) |
| Timezone | Region for the status clock (time shown at top of sidebar) |
| Show clock | Toggle live clock in the UI |
| Advanced: external call relay | Optional STUN/TURN server; only used if the built-in relay is unavailable (leave empty) |

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

Open the link printed in the terminal — `https://127.0.0.1:9876/?token=…` (self-signed cert — accept once for localhost). The token keeps other programs on your machine out of your chats; run `srltcp url` to print the link again.

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

### Android (Gradle + Chaquopy)

See [android/README.md](android/README.md). The APK embeds the same `srltcp/` Python code via **Chaquopy** — built **locally** with Gradle (no cloud CI).

**Prerequisites**

| Tool | Notes |
|------|--------|
| **JDK 17+** | Any JDK 17 or newer (tested with 21); set `JAVA_HOME` if `java` is not on PATH |
| **Android SDK API 34** | Platform + build-tools 34.x |
| **Python 3.12** | Used by Chaquopy at build time. If `python3` is another version, set `SRLTCP_BUILD_PYTHON=/usr/bin/python3.12` |
| **adb** | USB debugging enabled on the phone |

```bash
export ANDROID_HOME="$HOME/Android/Sdk"
export PATH="$PATH:$ANDROID_HOME/platform-tools"
```

**Clean rebuild (recommended after Python or Android changes)**

```bash
cd /path/to/SRLTCP
bash scripts/sync-android-python.sh          # copy srltcp/ into the APK tree
cd android
rm -rf app/build .gradle build               # remove old build artifacts
./gradlew clean
./gradlew assembleDebug renameDebugApk
```

Output: `android/app/build/outputs/apk/debug/SRLTCP-0.10.1.apk` (one APK for phones, arm64; add `-PsrltcpAbis=arm64-v8a,x86_64` for an emulator or Chromebook)

**One-command build** (sync + Gradle): `bash scripts/build-android.sh`

**Install on a connected device**

```bash
adb uninstall com.srltcp.app                 # optional — fresh install
adb install -r android/app/build/outputs/apk/debug/SRLTCP-0.10.1.apk
adb shell am start -n com.srltcp.app/.MainActivity
```

**Live logs (device attached via USB)**

```bash
adb logcat -c                               # clear old logs
adb logcat -s SRLTCP:* python.stderr:*      # follow SRLTCP + Python output
```

**On the phone:** tap **☰** for contacts, **⚙** for Settings (full-screen). Default folders: `Downloads/SRLTCP/Incoming` and `Downloads/SRLTCP/Shared` (grant storage access when prompted). Serial/USB is disabled; TCP works over Wi‑Fi, mobile data and VPN; Tor (built in) or the internet relay covers the rest.

| Symptom | What to do |
|---------|------------|
| `SDK location not found` | Set `ANDROID_HOME` or `android/local.properties` with `sdk.dir=...` |
| Gradle / Java errors | Use JDK 17 or newer |
| White screen | Wait ~30s for Python; check logcat |
| File attach fails | Rebuild with v0.1.52+ (WebView file picker) |
| Can't delete shared folder | Use **Delete** in Settings → Folders (v0.1.52+) |

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

1. Click **Pair** on one machine and let the other scan the QR code (or paste the link) — both become trusted contacts
   - Or press **Announce** on one device and **Trust** the other under *Discovered*
2. Select the contact and chat, send files, or start a voice/video call

### Finding each other (LAN, WireGuard / VPN)

| Network | What to do |
|---------|------------|
| **Same Wi-Fi / LAN** | Press **Announce** (or leave *Let nearby devices find me* on). Devices appear under *Discovered*. |
| **WireGuard / any VPN** | Same — press **Announce** once on either phone. VPNs carry no broadcast, so SRLTCP also sends its signed announce directly to every address in the VPN subnet, and the other device answers. Both appear under *Discovered* within a few seconds. |
| **You know the IP** | *Add Contact → Find a device by IP*, e.g. `10.0.100.5`. |

After that, addresses look after themselves: when a contact's IP changes (new Wi-Fi, mobile data, VPN reconnect) its signed announce updates your contact entry and the app reconnects — measured at under 15 seconds over WireGuard.

**WireGuard checklist** (only if devices don't find each other):

- Each phone's peer config must route the VPN subnet: `AllowedIPs = 10.0.100.0/24` (not just the server's address).
- The server must forward traffic **between** peers: `net.ipv4.ip_forward=1` and allow `wg0 → wg0` in the firewall (e.g. `iptables -A FORWARD -i wg0 -o wg0 -j ACCEPT`).
- Allow UDP **7826** (discovery) and TCP **7825** (connections) between peers. Port 9876 is local-only and never needs opening.
- Test from phone A's browser: `https://10.0.100.5:9876` should show a certificate warning or "SRLTCP is locked" — that proves the phones can reach each other.

### Phones on different mobile carriers / without the VPN

Mobile carriers put phones behind carrier-grade NAT: a phone on mobile data usually has **no IPv4 address another phone can connect to**, so something reachable has to sit in the middle. By default that's **Tor** (built in, no setup; see below). With IPv6 on both sides the phones can also connect directly. If you'd rather have a faster middle you control, run your own relay:

1. **Pair locally** (same Wi-Fi, WireGuard, QR or NFC), or send a pairing link.
2. **Internet relay** — run `srltcp relay` on a server with a public address (your WireGuard server is perfect) and paste the code it prints into *Settings → Network → Internet relay* on **one** phone. The other phone learns it automatically inside the encrypted connection.
3. From then on the phones use a **direct** path whenever one works and the **relay** otherwise — VPN off, different carriers, Wi-Fi to mobile data. Moving to the relay takes a few seconds (3 s in our test); when a direct path comes back they move back within about a minute. Messages, files, voice and video calls all work over the relay. The chat header shows **DIRECT** or **RELAY**.

**Direct without port forwarding.** One phone on the VPN and the other on the home Wi-Fi connect directly (the phones tell each other their LAN and VPN addresses inside the encrypted session). Two phones on *different home networks* also connect directly through both routers: with *Settings → Network → Try direct internet connections* on (default), they use the connection they already have (Tor for IPv6, a relay for IPv4) to agree on a moment and dial each other at once (hole punching) — in our simulation the direct connection was up 10 s after they met on the relay, with routers that ignore or reset unexpected connections alike. This shares your public IP address with your contacts only, and on a direct connection your networks can see which address you talk to; turn it off to keep every internet connection on Tor or the relay. Carrier-grade NAT on IPv4 mobile data usually prevents it; Tor or the relay then simply carries on.

**No server, no port forwarding: Tor.** With *Settings → Network → Reach contacts anywhere over Tor* on (default) on both devices, they reach each other from any network through the Tor network: phone on mobile data, PC on a VPN, a friend's Wi-Fi. No relay and nothing to configure on any router. Each device publishes a private onion service; its address goes only to trusted contacts (inside the encrypted connection), and only trusted contacts can connect through it. Tor is built into the Android app; on a PC install [Tor Browser](https://www.torproject.org/download/) (SRLTCP starts its Tor by itself). Tor is used only when there's no faster path, and the app moves back to a direct path when one appears. **IPv6 straight from Tor:** when both devices have IPv6 internet, they swap their IPv6 addresses inside the Tor session and dial each other directly (no relay, no server); the chat badge changes from TOR to DIRECT and files run at the full speed of both connections. If a network blocks it, they simply stay on Tor. Tested on a private Tor network (`scripts/tor_testnet.sh` + `scripts/simulate_tor.py`).

Every setting explained, plus common setups and troubleshooting: **[docs/NETWORKING.md](docs/NETWORKING.md)**.

### Pairing

| Method | How |
|--------|-----|
| **QR code** | *Pair* → the other device taps *Scan their QR code* (Android camera, or a browser with BarcodeDetector) |
| **Link** | *Pair* → *Copy link* / *Share…*, send it over a channel you trust; the other side pastes it into *Pair* |
| **NFC (Android)** | Open *Pair* on **both** phones and hold them back to back (NFC on, screens unlocked). The phones take turns reading each other and exchange contact cards automatically; you feel a short vibration when it worked |

Pairing is always two-way. With QR, link and NFC both phones add each other: the device that showed the code adds whoever scanned it automatically (the scan carries a one-time secret from that code) and opens the new chat — even if the two only reach each other later, or an app restarts in between. If you add someone from *Discovered* (or with an old code), their phone pops up **Contact request — Add them back?**; tapping *Accept* makes you contacts on both sides.

A pairing code is a signed contact card (hash ID, public keys, name, and every address the device has — VPN first). It works once and expires after 15 minutes.

**Verified contacts.** The shield next to a contact's name shows whether you checked their keys: filled with a tick when verified. Pairing **in person** (QR scan or NFC tap) verifies both phones automatically. For a contact added another way, tap the shield: scan the code on their phone, or compare the 60-digit **safety number** in person or on a call and tap *The numbers match*. The number covers both identity keys, including the post-quantum one, and is the same on both phones. Details in [SECURITY.md](SECURITY.md#verified-contacts).

### Editing contacts

Tap the contact's name at the top of a chat, the **⋮** next to a contact, or long-press it → *Edit contact*. You can rename them (only you see the name), set their LAN/VPN address and port, verify them, block/unblock and delete.

**Check connection** (in *Edit contact*) tries every address the app knows for that contact (and Tor) and says in plain words what works and what to change — e.g. "the VPN must include the home network".

**Deleting a contact** keeps your chat with them for 24 hours: add them back within that time and the conversation carries on. After 24 hours it is deleted for good, and adding them again starts fresh. Group chats are not affected.

### Privacy: backup, restore, disappearing messages

*Settings → Privacy*:

- **App lock** (Android, off by default): SRLTCP asks for your fingerprint, face or screen lock when it's opened and after 30 seconds in the background. Messages and calls still arrive while it's locked; a call can be declined from its notification. Turning it on asks you to unlock once, to be sure you can.
- **Hide message content on the lock screen** (Android, off by default): a locked phone shows "SRLTCP — New message", and calls ring without the caller's name (the call screen opens when you unlock or answer); unlocked, notifications show everything.
- **Disappearing messages** (off by default): messages and files you send in one-to-one chats are deleted from both phones 1 hour, 1 day, 1 week or 4 weeks after sending; a small timer shows on those messages. Each person's setting applies to what they send.
- **Backup**: enter a password (8+ characters, a passphrase is better) and tap *Create backup*. Android asks where to save the file (Downloads, a USB stick, Drive…); a PC downloads it. The file holds your identity, contacts, chats, groups and settings, encrypted with that password. Received files, shared folders and paused transfers are not in it.
- **Restore**: on the new device tap *Restore from backup…* (also offered on the first-run screen), pick the file, enter the password. SRLTCP replaces its identity, contacts and chats with the backup's and restarts; your contacts see the same you. Use a restored identity on one device only.

### Calls

Voice and video calls use WebRTC. Offer/answer are sent inside the encrypted SRLTCP session, so DTLS fingerprints are authenticated by the contact's identity key. The media itself also goes through the session: each device runs a tiny TURN relay on localhost for its own browser/WebView, and the relay forwards the (already DTLS-SRTP encrypted) packets to the other phone as encrypted `CALL_MEDIA` messages. So **calls work wherever chat works** — same Wi-Fi, WireGuard/VPN, direct IPv6, Tor or the internet relay — with no STUN/TURN server. Over Tor they work but can be choppier.

**Earpiece / speaker (Android).** Calls start on the earpiece (voice) or the loudspeaker (video). A wired or Bluetooth headset takes over when connected. The speaker button switches between them. The app applies the route again when the other side's audio starts and when a headset is plugged in or removed, because the WebView's audio engine can reset it on some phones. On Android 12+ it uses `setCommunicationDevice` and falls back to the speakerphone switch if the phone refuses. Speaker volume during a call is the phone's *call* volume: press the volume keys while in the call.

### Group chats

*Groups → New group*, give it a name and tick the trusted contacts to add. Everyone you add gets the group automatically (only invites from your own contacts are accepted).

- Every message is signed by its author and sent over the normal one-to-one encrypted connections — no group server and no shared group key.
- You send to the creator and to the members you have as contacts; the creator passes messages on to the rest. So members who don't know each other still see every message, and the creator can't change or invent anyone's message (each one carries its author's signature).
- Members who are offline get missed messages and member-list changes when they come back.
- The creator renames the group and adds or removes members (tap the group name at the top of the chat). Anyone can *Leave group*; the creator leaving closes it for everyone.
- Text only for now — files and calls stay one-to-one.

### When the VPN is off

Nothing changes: the app moves to direct IPv6, Tor or your relay by itself and back to the VPN when it returns. If no path works at all (for example Tor is off on one side and there's no relay), a message waits on your phone (tap its tick: *Waiting to send*) and goes out by itself a few seconds after a path is back — nothing is lost.

### Running in the background (Android)

SRLTCP keeps running after you close it, like any messenger, so messages, calls and contact requests arrive as notifications. To stop it completely, **swipe the SRLTCP notification away** (Android 14 and newer) or tap **Quit** on it (any version — older Android keeps that notification pinned). Opening the app starts it again.

### Internet relay server

On a Linux server with a public address (the WireGuard server works well):

```bash
git clone https://github.com/narl3yyy-svg/SRLTCP.git && cd SRLTCP
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/srltcp relay --public-host YOUR.SERVER.IP     # default port 7827
```

It prints a relay code (`srltcp-relay://…`). Paste it into *Settings → Network → Internet relay* on one phone. Open **TCP 7827** in the server's firewall (or use `--port 443` if nothing else uses it — it then looks like ordinary HTTPS). Keep the code private: it contains the access key. Running it as a service, updating, and what the relay can and can't see: [docs/INTERNET_RELAY.md](docs/INTERNET_RELAY.md).

```
phone A ──TLS──▶ relay (sees: IPs, timing, sealed padded blobs) ◀──TLS── phone B
        └────────────── E2EE session inside, pair-key sealed ───────────────┘
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

### Local REST API

Every API call needs the access token. On Linux/macOS:

```bash
export SRLTCP_TOKEN=$(cat ~/.srltcp/web_token)   # Windows: %APPDATA%\SRLTCP\web_token
```

### File transfer (API)

```bash
# Send a file to a connected peer
curl -k -H "Authorization: Bearer $SRLTCP_TOKEN" -X POST https://127.0.0.1:9876/api/transfer \
  -H 'Content-Type: application/json' \
  -d '{"recipient_hash":"<hash>","path":"/path/to/large.iso"}'

# List transfers
curl -k -H "Authorization: Bearer $SRLTCP_TOKEN" https://127.0.0.1:9876/api/transfers
```

Transfers are **resumable**: after a dropped connection, a path change or a **restart of either app**, the file continues from what the receiver already has on disk (`FILE_RESUME`). Unfinished transfers are kept in `transfers.json` (data folder, owner-only) and come back *paused* until the contact is connected again; the receiver re-checks the part it already has, and a file that was edited or deleted in the meantime is not resumed (both sides show it as failed). With *Keep messages: Until restart* nothing is kept.

Files **wait like messages**: sending to a contact who isn't reachable right now returns a transfer in state `paused` (the chat shows *waiting for connection*). It's offered automatically as soon as a session with that contact comes up. An offer that's never answered (lost when the link dropped) is sent once more after 30 s, then waits for the next connection.

In the web UI, pick **several files at once** with the paperclip, or **drag files** from your file manager onto a contact in the sidebar. Progress, speed and a cancel button show in the file's bubble, in that contact's chat only. Images and videos preview in chat during transfer. Click to enlarge in a viewer that zooms with two fingers, a double-tap or the mouse wheel (the rest of the app doesn't zoom; it fits the screen); use **Download file** to save it — you are asked where to save (Android shows the system *Save as* screen; desktop Chrome/Edge show a save dialog, other browsers use their download folder).

**Shrink photos before sending** (*Settings → General*, off by default) sends photos as JPEG with the longest side at most 2560 px: a 12 MP phone photo goes from ~5 MB to ~1 MB, which matters over Tor and mobile data. The re-encoded photo carries none of the original's metadata (GPS location, camera, time). Screenshots and PNG graphics, GIFs and anything the device can't decode are always sent as they are.

### Sharing folders and files (E2EE)

Open a chat → **folder icon** in the header (or the contact's **⋮** → *Share folder*). The share window has three tabs:

**Share**
1. *What*: **a folder** (Choose folder; your default shared folder is pre-filled) or **files** (pick one or more; a private copy is shared, so you can move or edit the originals).
2. *With*: tick contacts, or pick a **group** (every member who is your contact gets it). Contacts who aren't connected now get the offer automatically the next time they are.
3. *Name* (optional), *Available for* (5 min … until you stop it), *Downloads per person* (1 … unlimited).
4. *Password* (optional): recipients must type it to open the share. Tell them another way. It's sent only inside the encrypted connection, you keep only a salted hash, and 5 wrong tries lock the share for 10 minutes.

**Shared with me**: tap a share → enter the password if it has one → tap a file to download it (or *Download as ZIP* for a folder). Downloads use the normal encrypted file transfer.

**My shares**: who has what, time left, downloads used, "waiting for them to connect"; **Stop sharing** ends access at once.

Each recipient gets their own grant bound to their identity: nobody else can use it, even with the password. Shares survive restarts; files copied for a share are deleted when it ends.

**API:**

```bash
# Files: upload into a private share folder (repeat -F for more files)
curl -k -H "Authorization: Bearer $SRLTCP_TOKEN" -F file=@photo.jpg https://127.0.0.1:9876/api/share/files
# Share a folder (or the uploaded files: "kind":"files","path":<returned path>)
curl -k -H "Authorization: Bearer $SRLTCP_TOKEN" -X POST https://127.0.0.1:9876/api/share/peer/offer \
  -H 'Content-Type: application/json' \
  -d '{"recipients":["<peer_hash>"],"group_id":"","path":"/home/user/shared","ttl_preset":"1d","download_limit_preset":"unlimited","password":""}'
# Open a share you received (add "password" if it has one)
curl -k -H "Authorization: Bearer $SRLTCP_TOKEN" -X POST https://127.0.0.1:9876/api/share/peer/list \
  -H 'Content-Type: application/json' -d '{"owner_hash":"<owner_hash>","grant_id":"<grant_id>"}'
# Download a file from it
curl -k -H "Authorization: Bearer $SRLTCP_TOKEN" -X POST https://127.0.0.1:9876/api/share/peer/fetch \
  -H 'Content-Type: application/json' \
  -d '{"owner_hash":"<owner_hash>","grant_id":"<grant_id>","path":"docs/readme.txt"}'
```

### Folder sync

Contact's **⋮** → *Sync a folder…* → choose a folder (and the name they see) → *Start syncing*. Your contact gets an invitation and picks where their copy goes (default `Downloads/SRLTCP/Sync/<name>` on Android). From then on, adding, changing or deleting a file on either side is copied to the other whenever you're both connected: on the same Wi-Fi, over IPv6, Tor or your relay. Each folder is scanned every 20 seconds and right after you reconnect; *Sync now* doesn't wait.

- **Two-way and incremental.** Only changed files travel, through the normal end-to-end encrypted, resumable file transfer, checked against their SHA-256.
- **Conflicts.** If you both change the same file before it syncs, both phones keep the same version under the original name and the other one next to it as `name (conflict <date> <device>).ext`. Nothing is lost.
- **Deletes** are copied too, but on the other side the file goes to the hidden `.srltcp-sync/trash` folder inside the synced folder for 30 days. A delete on one side and an edit on the other keeps the edit.
- **Pause, resume, stop** from the same window. Stopping (on either side) ends it for both; the files stay where they are.
- **Safety.** A contact can only touch the folder of a sync you started or accepted with *that* contact, every path they send is checked (nothing outside the folder, no hidden `.srltcp-sync` files), and a file is only sent if it's exactly the version asked for. Both devices need a version with folder sync.

### Legacy HTTP share sessions (localhost only)

For local tooling, token-based HTTP browse remains available on the **localhost HTTPS** UI only:

```bash
curl -k -H "Authorization: Bearer $SRLTCP_TOKEN" -X POST https://127.0.0.1:9876/api/share/create \
  -H 'Content-Type: application/json' \
  -d '{"path":"/home/user/shared"}'

curl -k -H "Authorization: Bearer $SRLTCP_TOKEN" "https://127.0.0.1:9876/api/share/<session_id>/list?token=<token>"
```

### Port forwarding (optional, advanced)

You don't need it: Tor, direct IPv6 and the relay connect contacts on different networks by themselves. If you forward **TCP 7825** on your router anyway (*Settings → Network → More network options → I port-forward TCP 7825*), contacts reach you there with the same end-to-end encrypted handshake as on the LAN. Internet addresses saved per contact by earlier versions are still used. Only forward 7825, never the web UI port (9876), which stays on localhost.

| Risk | Mitigation |
|------|------------|
| Connecting to the wrong host | The handshake proves the contact's keys; a wrong host can't complete it. Verify contacts (shield next to the name) |
| Private IP as an internet endpoint | Rejected — LAN addresses are reached on the LAN |
| Localhost / loopback endpoint | Rejected |
| DNS rebinding to a private IP | Resolved address must be public |
| Connection storms | Outbound dials are rate-limited (1/s per endpoint) |
| Web UI exposed to the internet | **Do not** forward 9876; the UI is localhost-only by design |
| Untrusted inbound traffic | Only trusted contacts complete a session; others become contact requests at most |

---

## Performance notes

| Setting | Value | Rationale |
|---------|-------|-----------|
| Chunk / window | 1 MiB / 24 MiB direct · 64 KiB / 4 MiB Tor · 128 KiB / 2 MiB relay · 8 KiB serial | Keeps each path full without flooding it |
| Tor | 8 parallel circuits for file data; a direct path is tried as soon as a file starts | One circuit tops out around 0.3 MB/s (Tor's per-stream window / onion round trip) |
| Compression | zstd (level 1 direct, 3 on slow paths) | Skipped for photos, videos, archives and data that doesn't shrink |
| Frame CRC | CRC32 | Cheap integrity check per frame |
| Hashing | SHA-256 while streaming, both sides | No extra pass over the file before or after |
| Off the event loop | read/hash/compress/encrypt and write/hash in worker threads | Chat, calls and UI stay responsive during big transfers |
| Memory | Streaming; bounded reorder buffer (48 MiB) | Never loads the full file into RAM |

**Tips for maximum speed:**

- Use wired Ethernet or USB 3 serial adapters at 115200+ baud
- Direct LAN/VPN/IPv6 paths are used automatically whenever they work (no hop in between)
- Over Tor, files use 4 circuits at once; a relay on a low-latency host close to you is faster than Tor if you want one

---

## Development

### Desktop / Python

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
# Tests use SRLTCP_DATA_DIR automatically — do not run pytest against ~/.srltcp

./run.sh web                    # HTTPS web UI + P2P node (open the ?token= link it prints)
./run.sh relay --public-host IP  # internet relay on TCP 7827
bash scripts/check.sh           # ruff + mypy + pytest (run before tagging)
pytest tests/ -v
```

| Command | Description |
|---------|-------------|
| `srltcp web` | Web UI + P2P node |
| `srltcp relay` | Internet relay server (`hub` still works as an alias) |
| `srltcp send` | One-shot CLI message |
| `srltcp identity` | Show local hash IDs |
| `srltcp url` | Print the private web UI link (with access token) |
| `python scripts/simulate.py` | Run three local nodes and drive their UIs end-to-end in Chromium (needs `pip install playwright`) |
| `bash scripts/tor_testnet.sh start DIR` + `python scripts/simulate_tor.py` | Two nodes talking only over a private, offline Tor network |
| `python scripts/pentest/pentest_local.py` / `pentest_relay.py` | Automated penetration tests (node and relay) |
| `sudo bash scripts/simulate_wireguard.sh` | Two "phones" + a server on a real WireGuard VPN in network namespaces: discovery, messaging, IP change |
| `bash scripts/sync-android-python.sh` | Copy `srltcp/` into Android project before Gradle |
| `bash scripts/build-android.sh` | Full local APK build |

**Releases:** bump `srltcp/__init__.py` and `android/app/build.gradle.kts`, update `srltcp/RELEASE_NOTES.md`, build the APK locally, tag `vX.Y.Z`, attach APK + source zip to [GitHub Releases](https://github.com/narl3yyy-svg/SRLTCP/releases).

---

## Changelog

Full history: [srltcp/RELEASE_NOTES.md](srltcp/RELEASE_NOTES.md). Click the version badge in the app status bar for in-app release notes.

## Roadmap

**Done (v0.10.0)**

- [x] Folder sync with a contact: two-way, incremental, conflict copies, 30-day trash for deletes

**Done (v0.9.0)**

- [x] Quantum-safe sessions: X25519 + ML-KEM-768 key exchange, Ed25519 + ML-DSA-65 signatures
- [x] Direct IPv6 connections set up from a Tor session (no relay or server)
- [x] Verified contacts: safety numbers, automatic when pairing in person
- [x] Encryption at rest (Android Keystore), encrypted backup / restore
- [x] Disappearing messages, app lock, private lock-screen notifications
- [x] Transfers survive restarts; several files at once; optional photo shrinking

**Done (v0.8.0–0.8.3)**

- [x] Transfer engine v2 (windowed, verified while streaming, cancel both ways), Tor lanes
- [x] Calls ring like phone calls (default ringtone, lock screen); ✓✓ for delivered files

**Done (v0.6.0–0.7.0)**

- [x] Direct connections across networks without port forwarding (hole punching)
- [x] Reach contacts over Tor: no server, no port forwarding (built into the Android app)
- [x] Built-in call relay (calls through the encrypted session, on every path)

**Done (v0.1.50–0.1.57)**

- [x] Headless hub server (E2EE tunneling, no port-forward for clients)
- [x] Android Gradle + Chaquopy rebuild (local `./gradlew` builds)
- [x] Android mobile UI (slide-out sidebar, full-screen settings) (v0.1.51)
- [x] Downloads folder defaults for incoming/shared files on Android
- [x] Local APK releases (no GitHub Actions CI)
- [x] Android file attach via WebView file picker (v0.1.52)
- [x] Settings folder delete on Android (v0.1.52)
- [x] Android chat layout above status bar (v0.1.51; padding tuned v0.1.53)
- [x] Android status bar shows temperature only (CPU hidden) (v0.1.52)
- [x] APK named `SRLTCP-X.Y.Z.apk` (no `-debug` suffix) (v0.1.53)
- [x] No connection-refused flash when swiping app closed (v0.1.53)
- [x] Android background notifications + session restore (v0.1.54)
- [x] Hub LAN address for same-network clients (v0.1.55)
- [x] Android stack navigation — sidebar home, chat overlay, back gesture (v0.1.56)
- [x] Signed LAN announces — Ed25519 signatures on UDP discovery; reject unsigned/spoofed peers (v0.1.57)
- [x] Noise XX handshake option (v0.1.57; removed again in 0.6.1)

**Done (v0.5.0)**

- [x] Internet relay v2: paired contacts stay in touch without the VPN, automatic direct/relay switching, blind relay (TLS pinned, rotating mailboxes, pair-key sealing, padding)
- [x] Handshake v4 (signature names the recipient), penetration test with automated harnesses (`scripts/pentest/`)

**Done (v0.4.1)**

- [x] Swipe the background notification away to stop SRLTCP completely

**Done (v0.4.0)**

- [x] Group chats (signed messages, creator relays to members who aren't contacts)
- [x] Call audio/video on Android, phone-style audio routing, speaker button
- [x] Security and reliability fixes from a full review (network flood, hub replay, transfer resume)

**Done (v0.3.1)**

- [x] Calls connect everywhere chat works (media through the encrypted session)
- [x] Two-way pairing prompt, Save-as for downloads, Android back gesture

**Done (v0.3.0)**

- [x] Discovery over WireGuard/VPN (unicast sweep, announce replies, Find by IP)
- [x] Edit contact; automatic contact address updates; dead-link detection
- [x] NFC pairing works by just holding phones together
- [x] Background operation on Android with notifications

**Done (v0.2.0)**

- [x] Pairing by QR code / link / NFC tap, contact requests
- [x] Voice & video calls (WebRTC, E2EE signalling)
- [x] Offline message queue with delivery receipts; chat history on disk
- [x] Authenticated handshake (hash ↔ key binding), replay protection, Noise key fix
- [x] Access token for the local web UI/API; Android certificate pinning

**Planned**

- [ ] Files and voice/video calls in group chats
- [ ] Calls over serial/RF
- [ ] Wi-Fi Direct / Bluetooth transport for phone-to-phone without a router
- [ ] Per-contact "always use Tor" (hide who talks to whom even when a direct path exists)
- [ ] Several relays per contact (failover) and multi-hop relaying
- [ ] Android USB serial Chaquopy shim (full OTG support)
- [ ] QUIC (UDP) as an extra direct path: UDP hole punching gets through more routers and carrier NATs than TCP, so fewer contacts fall back to Tor. Plan: keep TCP whenever a direct TCP path works (faster), use QUIC when only UDP gets through, Tor as the last resort; SRLTCP's quantum-safe handshake runs inside QUIC. Needs aioquic built for Android (Chaquopy has no wheel yet) or a Java QUIC library
- [ ] Bandwidth limiting and QoS per transfer
- [ ] Signed APK releases with a stable release key (updates install over the old app)
- [ ] Desktop system tray wrapper (Tauri/Electron)

---

## License

GPL v3 — see [LICENSE](LICENSE). Bundled third-party components: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Acknowledgments

Architecture patterns adapted from [chatx5](https://github.com/narl3yyy-svg/chatx5). Identity hashing inspired by [Reticulum](https://reticulum.network/).