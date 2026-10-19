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

SRLTCP is designed for **peer-to-peer, end-to-end encrypted** communication over TCP and UDP (LAN, VPN, directly across the internet) and USB serial, with **no relays and no servers** carrying anything; Tor is used only to find contacts on other networks.

### What is protected

- **Peer authentication** — every handshake proves possession of the Ed25519 key whose SHA-256 prefix is the peer's hash ID **and** of the contact's ML-DSA-65 (post-quantum) key, pinned at the first handshake; pinned keys must match. Handshake signatures (v6) cover the ephemeral keys, the ML-KEM key/ciphertext, a timestamp, the sender **and the intended recipient**, and are checked against a replay cache — a handshake can't be replayed to another node.
- **Quantum-safe key exchange** — each session combines X25519 with ML-KEM-768 (FIPS 203); the session key needs both secrets, so recorded traffic stays safe even against a future quantum computer, and a flaw in either algorithm alone isn't enough.
- **Message and file payloads** are encrypted with AES-256-GCM under fresh per-session keys (forward secret).
- **Session keys** are derived via HKDF-SHA256 from both shared secrets, salted with both ephemeral keys and the ML-KEM transcript, with direction-specific labels.
- **Replay / reordering** — 64-bit counter nonces with a sliding replay window; the message type and flags are authenticated as AAD. Unencrypted application messages are dropped.
- **Trust gate** — only trusted contacts' messages, files and share requests are processed; others become contact requests.
- **Calls** — WebRTC DTLS-SRTP; SDP (with DTLS fingerprints) is exchanged inside the authenticated session, and the media packets are carried inside the session too (a per-device media forwarder bound to the device itself, with per-call credentials, accepting traffic only for the contact being called). No outside STUN/TURN servers.
- **Address updates** — a contact's stored address is only changed by a fresh (≤ 5 min old) announce signed by that contact's key and newer than any announce seen from it; replayed announces are ignored.
- **Group chats** — no group key or server: every group message is signed by its author (Ed25519) and sent over the pairwise sessions; the creator's phone passes messages on to members who aren't each other's contacts but cannot alter or forge them. Member lists are versioned and only accepted from the creator; invites only from trusted contacts; group traffic only from current members. The creator (and every member) sees all group messages and the member list, including members' public keys and names.
- **Pairing codes** — signed contact cards; a one-time token (15 min) lets the scanner be trusted back automatically.
- **Contacts** live in `trusted.json` (encrypted at rest, owner-only, written atomically).
- **Data at rest** — identity keys, contacts, chats, groups, settings, shares and transfer state are sealed with AES-256-GCM under a random data key (file name bound as associated data). Android: the data key is wrapped by a non-exportable Android Keystore key. Windows: DPAPI (your Windows account). Linux/macOS: an owner-only key file in the data folder (use full-disk encryption). If the data key can't be loaded, or your identity or contacts can't be decrypted, the app stops with an error instead of starting empty (it never creates a new identity over the old one); another damaged file (say, chat history) is moved aside, never overwritten.
- **Backups** — *Settings → Privacy → Backup*: one file sealed with your password (scrypt N=2¹⁶, r=8 → AES-256-GCM, header authenticated). It holds your identity, contacts, chats, groups and settings; keep it and its password safe, since together they let someone use your identity. Restoring stops the app, replaces those files and restarts.
- **Folder sync** — a sync exists only between you and one contact, set up by an invitation you accept; sync messages from anyone else are ignored. Every path a contact sends is checked (no `..`, absolute paths, drive letters or the internal `.srltcp-sync` folder), a file is only sent if it's in our index with exactly the requested hash, an incoming file is only accepted if we asked for that exact version, and it's verified by SHA-256 before it replaces anything. Deletes from the contact move files to a 30-day trash instead of erasing them.
- **Disappearing messages** — with a timer set, the messages and files you send carry it inside the session; both phones delete them when it runs out (received files too). A modified app or a screenshot can still keep them: this protects against someone getting the phone later, not against the person you write to.
- **Direct UDP connections** — keys and connection ids come from a punch exchange inside the authenticated E2EE session (a random 32-byte share from each side, HKDF-SHA256 with both identities into one AES-256-GCM key per direction). Every packet is encrypted and authenticated with its header (connection id, packet number) as AAD; packet numbers are the nonce and never repeat; replays and packets older than the window are dropped before decryption. Packets that don't decrypt get no answer at all, so the UDP port can't be used to inject data, close a connection, probe for the app or amplify traffic; the phone only sends probes to public addresses a contact gave it (no LAN, loopback or carrier-internal ranges). A birthday punch (one side behind a strict NAT) sends a few thousand probes to random ports of the contact's public address for at most 10 s; they carry the same encryption, and the extra sockets on the strict side are closed as soon as one gets through. The signed handshake still runs inside before the connection replaces a session, and call-media datagrams are accepted only on a connection that carries a contact's established session. A contact (who has the keys) can at most end its own connection: frames are bounds-checked, flow control caps what it can make us buffer, and acknowledgements for packets we never sent are ignored.
- **Handshake order** — a handshake's Ed25519 signature (addressed to us) and freshness are checked before any post-quantum work is done or any decision about existing sessions is taken; each connection may open at most 12 handshakes a minute, and a connection carries one handshake at a time. Installing the same session keys again never restarts the nonce counter.
- **Keep-alives** — pings and their answers travel inside the encrypted session (with a random nonce the answer must repeat), so nobody can fake that a connection is alive or what its latency is. Liveness decisions on the radio use only authenticated traffic.
- **Radio link layer** — acknowledgements must match the message's part count, and a packet counts as a resend of a delivered message only if its content matches, so forged packets with a visible message number can't make real messages count as delivered. The speed the receiver reports in each acknowledgement only sets how fast the sender paces; a forged one can at worst slow a transfer or make the sender overrun its radio, which jamming does anyway. (The radio link itself is not authenticated: anyone in range can still jam it.)
- **Unauthenticated connections** — TCP connections that don't complete a handshake within 30 s are closed, open connections are capped, and frame parsing is linear in the input, so junk traffic can't stall the node.
- **Discovery stays on your networks** — announces go only to the Wi-Fi/LAN you're on and your VPN (never broadcast on mobile data); announces, *Find by IP* and contact requests from any other network (a mobile carrier, the internet, IPv6 outside your LAN prefix) are ignored, so nobody outside your own networks can find this device or ask to be added. Contacts you already have reach you from anywhere (inside encrypted sessions); new people elsewhere use a pairing code or link.
- **LAN UDP announces** (port 7826) are signed with the sender's Ed25519 identity key. Receiving peers verify the signature and `hash_id` ↔ `public_key` binding before displaying or trusting a discovered peer. Unsigned or invalid announces are dropped.
- **One handshake protocol** — the identity handshake above. The experimental Noise XX option was removed in 0.6.1 (a device set to it couldn't connect to devices using the default); a saved `noise_xx` setting is ignored.
- **Web UI** is served over **HTTPS on localhost only** (self-signed cert generated locally) and every request needs the per-install access token (`~/.srltcp/web_token`, mode 600) — so other apps/users on the device cannot use the API. Requests that change something from a browser must come from exactly this page (https, localhost, this port; a page on another local port is refused), the token never appears in the access log, and received files are served with a sandboxing CSP (an SVG or HTML file can't run script).
- **Deleting received files** (long-press → Delete, *Delete chat* with its option) only ever removes a file the app itself finished receiving and placed, never a name a cancelled or failed transfer would have used; deletes are limited to the chat they're made in.
- **Share sessions** use constant-time token comparison; path traversal is blocked on file APIs.

### What is not protected

| Data | Visibility |
|------|------------|
| LAN UDP discovery (7826) | Plaintext names, IPs, ports on the local network — **signed** (Ed25519) since v0.1.57; spoofing without the private key is rejected |
| Direct internet connections | Your contacts learn your public IP address (shared inside the encrypted session, only with trusted contacts, only while the setting is on), and the STUN server (Google/Cloudflare by default, or yours) learns that your IP uses it — nothing about contacts or content. Network observers see a direct UDP connection between two addresses. Turn it off to connect only on the same Wi-Fi/VPN |
| Direct internet traffic | Observers see connection endpoints, timing, and volume — not decrypted payloads |
| Local disk | Encrypted at rest (see above). Not encrypted: received files in your incoming folder (other apps must open them), the Tor onion key (Tor reads it), the localhost certificate and web token, logs |
| Android WebView | Pins the app's own localhost certificate (SHA-256); user-installed CAs are not trusted for it; the JavaScript bridge only answers the app's own page |
| NFC pairing | Contact cards (public data) are exchanged in the clear over a few centimetres |
| VPN discovery | With discovery on (or after pressing Announce), your signed announce (name, hash ID, public key, addresses) is sent to every address in your VPN subnet, and you answer announces from inside that subnet |

### Connection modes

| Mode | When it's used | Port-forward? |
|------|----------------|---------------|
| **LAN / VPN direct** | Whenever it works (always tried first) | No |
| **Direct across networks (hole punching)** | Two networks whose NATs allow it (most home routers and many carriers), with *Try direct internet connections* on: UDP (encrypted per packet) or TCP; addresses exchanged only inside the E2EE session (over Tor), so contacts (never strangers) learn your public IP; connection authenticated by the signed handshake | No (sends out only) |
| **Tor onion service (doorbell)** | Only to find a contact on another network and set up the direct connection: handshake, keep-alives, address swap and hole punching; messages, files, calls, groups, shares and sync are refused over it (they wait for a direct path). Between mutually trusted contacts only (onion address shared inside the E2EE session; Tor connections from non-contacts refused) | No |
| **Direct WAN** | Advanced: you forward TCP 7825 | Yes, on the reachable peer |

### Verified contacts

Each contact has a **safety number**: 60 digits computed from both phones' identity keys (Ed25519 and the post-quantum ML-DSA-65 key), the same on both phones. If the numbers match, nobody swapped keys between you, not even when you first connected over Tor or a network someone else controls.

- **Automatic when you pair in person.** A pairing QR code (or NFC card) carries a fingerprint of both keys. The phone that scans it marks the contact verified, and the first session must then present exactly that post-quantum key or it is refused. The phone that *showed* the code marks the scanner verified too, because only someone who saw its screen (or touched it, for NFC) had the one-time secret; if you pressed *Copy link* or *Share…* for that code, it doesn't, since the link could have gone anywhere.
- **Otherwise:** tap the shield next to the contact's name. Scan the code on their phone (they open *Verify* in their chat with you), or compare the numbers in person or on a call and tap *The numbers match*. A code meant for another contact, or one that doesn't match, is refused with a warning.
- A verification stays valid only while both sides' keys stay the same. Keys can't change silently: a different post-quantum key from a known contact is refused outright.

### Threat assumptions

- Peers on the same LAN, and anyone in range of a serial radio, may observe traffic patterns (when and how much is sent), never the content: everything on the radio is sealed with the session keys, including call audio.
- A contact is only as trustworthy as the way you added them. Pairing **in person** (scanning their QR code, or an NFC tap) verifies their keys at once; for anyone added another way (pasted link, *Discovered*), compare the **safety number** (see *Verified contacts* below) before relying on the contact.
- LAN announce signatures authenticate discovery metadata but do **not** encrypt it — anyone on the LAN can still observe that you exist and read names/endpoints.
- On a serial radio, beacons identify nobody: their tags only the named contact can compute (HMAC with a static X25519 key derived from both identity keys), padded with random filler. Someone in radio range can tell that *a* device is transmitting, not who. **But the handshake that follows when two contacts connect is not hidden:** like any first connection, it carries both names, hash IDs and public keys in the clear, so someone listening at that moment learns who is talking (never what). Only contacts may open a session on the radio; a handshake from anyone else is ignored and never replaces a contact's session. The beacon key is classical (not post-quantum); it only hides presence, while sessions use the quantum-safe handshake.
- On Wi-Fi the app asks your own router (UPnP / NAT-PMP, only routers on your LAN) to forward its direct-connection UDP port. That port drops every packet that isn't authenticated with keys from a contact it's connecting to. Turn it off under *Settings → Network → Open a port on my home router*.
- Blocked peers are rejected at the application layer but may still appear in discovery until the entry expires.

## Hardening Guidelines for Operators

### All platforms

- Verify your contacts: the shield next to a contact's name is filled once you did.
- Keep SRLTCP updated; crypto and handshake behavior may change between minor versions.
- Run the web UI on localhost; **do not** expose port **9876** to the internet.
- File chunks use the same session keys as chat — larger TCP chunks do not reduce encryption strength.
- Shared-folder grants are recipient-bound and time-limited; use E2EE peer share for sensitive folders.

### Direct WAN (advanced)

- Forward only **TCP 7825** on the machine that receives inbound connections.
- Verify peer hash IDs before trusting manual WAN endpoints.

### Android

- Build APKs locally from a trusted source checkout; verify `scripts/sync-android-python.sh` ran before Gradle build.
- Grant **USB permissions** only to the serial adapters you use: Android asks once per adapter, and SRLTCP only ever opens USB serial devices (the bridge's driver list never claims keyboards, storage or headsets).
- Grant **notification permission** on Android 13+ so the foreground service can keep the node alive.
- The embedded WebView trusts only `127.0.0.1` / `localhost` self-signed TLS.
- App data (identities, contacts, chats, settings) lives in the app files directory, encrypted with a key wrapped by the Android Keystore; uninstall clears it. Android cloud backup is off for the app (`allowBackup=false`): use *Settings → Privacy → Backup* instead.
- **App lock** (*Settings → Privacy*, off by default): the app asks for the fingerprint, face or screen lock when opened and after 30 s in the background, and the app switcher shows a blank card (Android 13+). It hides the app's screens, not its data from someone who unlocks the phone itself. It can only be turned on once the phone has a screen lock.
- **Private notifications** (*Settings → Privacy*, off by default): on a locked phone, message notifications read "SRLTCP — New message"; calls ring as a notification without the caller's name instead of opening the call screen over the lock screen.

## Builds & releases

- **No GitHub Actions CI** — run `bash scripts/check.sh` locally before tagging (`ruff`, `mypy`, `pytest`).
- **APK builds are local** — Gradle + Chaquopy on your machine (see `android/README.md`). Release APKs are uploaded to GitHub Releases manually.
- Report supply-chain concerns through the vulnerability channel above.

## Dependency Security

Runtime dependencies are declared in `pyproject.toml` with minimum versions (`aiohttp`, `cryptography`, `pyserial`, `zstandard`). Dev tools: `ruff`, `mypy`, `pytest`. Android adds Chaquopy-managed pip packages at APK build time.

Report dependency or supply-chain issues through the same vulnerability channel.