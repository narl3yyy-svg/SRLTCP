# SRLTCP Release Notes

## v0.1.20 (2026-07-03)

### Android — full rebuild (python-for-android)
- **Removed Chaquopy** — old `android/` Gradle/Chaquopy project deleted entirely
- **New P4A + Buildozer stack** — foreground `PythonService` runs `srltcp web`; `MainActivity` WebView loads localhost HTTPS UI
- **CI** — GitHub Actions builds APK on `main` push and release tags via Buildozer

### Fixes
- **Transfer dock** — closes when the current transfer completes; no longer reopens for unrelated background transfers
- **Serial settings panel** — flex layout no longer clipped on the left
- **Contact list** — hash ID removed from preview; **Copy hash ID** added to right-click menu

### New features
- **Send folder to peer** — right-click trusted contact → Send folder… (zipped E2EE transfer)

## v0.1.19 (2026-07-03)

### Fixes
- **Shared folder listing** — list API now waits for E2EE response and returns entries directly (no more stuck “Loading folder listing…”); auto-connects before list/offer; denied requests return empty listing with error
- **Receiver image preview** — incoming screenshots/images re-render as inline previews when transfer completes (not just a file attachment bubble)
- **Transfer dock** — progress bar hides after complete; polling no longer resurrects finished transfers; cancel (✕) blocked once transfer is done
- **Chat scroll** — new text messages scroll to bottom after image/file transfers
- **Android startup** — removed nested server thread (server runs in MainActivity worker thread only)

## v0.1.18 (2026-07-03)

### Critical fixes
- **Discovered peers invisible** — UI filtered 64-char hashes but real IDs are 32 hex chars; peers now appear after announce
- **Trusted/manual contacts broken** — hash validation corrected to 32 chars (Reticulum-style identity hash)
- **Android startup** — server starts directly from MainActivity (no longer blocked by denied notification permission); foreground service is optional keep-alive only

### New features
- **Add Contact** — manually trust a peer by hash ID with optional LAN host/port (no discovery required)
- **Copy hash ID** — click your profile hash to copy for sharing with another peer

## v0.1.17 (2026-07-03)

### Fixes
- **Trusted peers list** — invalid/duplicate entries filtered; generic placeholder names resolved
- **Shared folder download** — fixed path field bug; files now arrive via secure transfer
- **Receiver transfer UI** — progress bar removed on complete; **Save file** link injected dynamically
- **Android crash** — Python startup fallback, serial disabled on Android, safer WebView lifecycle

### New features
- **Share lifecycle** — time limits (1m–forever) and download count limits (1–unlimited)
- **Revoke shares** — sender can remove active share offers
- **Folder ZIP download** — directories compressed on sender before E2EE transfer
- **Media lightbox pan** — drag to move around zoomed images

### Performance
- TCP chunk size increased to 512 KiB (from 256 KiB) for higher throughput (~30–50+ MB/s on fast LAN)

## v0.1.16 (2026-07-03)

### New features
- **Drag-and-drop file send** — drop files from your file manager onto a contact in the sidebar to send to that peer
- **E2EE shared folders** — offer a local folder to a trusted, connected peer; browse and download over the encrypted link (no plaintext HTTP exposure)
- **Manual WAN peers** — add host/domain + port per trusted contact; connection modes `auto`, `lan`, or `wan`; outbound dials validated and rate-limited

### Fixes
- **Receiver transfer bar** — progress track removed when transfer completes; no stuck “transferring” state
- **Share / WAN WebSocket events** — `share_offer` and `share_listing` broadcast correctly to the UI

### Security
- WAN hosts must be public IPs or valid domains; private/localhost addresses rejected
- Share grants are recipient-bound with TTL; list/fetch denied without valid grant + handshake
- Settings flag documents intent to port-forward TCP **7825** (encrypted messaging port)

### Android
- APK **0.1.16** (versionCode 14) built via CI on tag `v0.1.16`

## v0.1.15 (2026-07-03)

### Fixes
- **UI flicker** — incremental transfer bubble updates instead of full chat re-render; throttled progress patches preserve image/video elements
- **Transfer dock** — hides immediately on complete/cancel for sender and receiver
- **Connection stability** — 45s post-transfer cooldown suppresses spurious link_down/reconnect; TCP keepalive; client ignores transient disconnects during cooldown
- **Media viewer** — zoom in/out/reset and mouse-wheel zoom in lightbox

### UX
- **Unread counters** — badge on trusted peers; cleared when chat is opened
- **Trusted peer list** — improved default contrast and readability
- **Notifications** — typed toasts; browser notifications when tab is in background

### Android
- WebView pause/resume lifecycle; service thread guard; no WebView destroy on rotation

## v0.1.14 (2026-07-03)

### Fixes
- **File transfers** — removed per-chunk `fsync` (major cause of stalls/disconnects on large files); fsync on complete only; file chunks processed asynchronously so the read loop stays responsive; serialized TCP sends via connection lock
- **Handshake** — client polls link status after connect; 15s server-side handshake wait; skip post-handshake ping during active transfers
- **HKDF** — `derive_session_key()` now requires explicit `info` (no legacy `srltcp-v1` default)
- **Media** — `Accept-Ranges: bytes` on transfer file endpoint for video seeking during partial downloads

### UI
- **Message actions** — copy (clipboard icon) and delete (trash icon) on text bubbles
- **Transfer dock** — auto-hides when idle; polls `/api/transfers`
- **Media in chat** — image/video preview, lightbox, download links with correct MIME types

### Android
- Foreground service + notification permission flow; server-ready wait before WebView; graceful FGS fallback

## v0.1.13 (2026-07-03)

### Fixes
- **File transfers** — ping suppressed during active transfers; link_down deferred while transferring; 10s link-wait retry mid-transfer; no compression on serial; forced reconnect blocked during transfer
- **Transfer dock** — auto-hides when no active transfers; polls `/api/transfers` for accurate state
- **Media in chat** — images and videos preview during transfer; click opens lightbox; Download link on all file types
- **Message actions** — copy (clipboard icon) and delete (trash icon) on text bubbles
- **Android APK** — `POST_NOTIFICATIONS` requested before foreground service; graceful fallback if FGS fails; service not stopped on background

## v0.1.12 (2026-07-03)

### Fixes
- **File transfers** — compressed chunk flag honored on receive; serial uses 8 KiB chunks; unique incoming paths
- **Transfer dock** — slim progress bar + MB/s only; auto-hides on complete/cancel/fail
- **Images in chat** — preview on sender and receiver during/after transfer; **Save as…** download link
- **Chat header** — TCP / SERIAL badge on active peer connection
- **Copy/Delete** — actions anchored on the message bubble (not opposite side)
- **Trusted contacts** — right-click opens contact menu (⋮ button removed)
- **Android APK** — foreground `SRLTCPService`, server-ready signal before WebView load

## v0.1.11 (2026-07-03)

### Fixes
- **Serial connect 500** — stale incomplete TCP links are torn down before serial dial; transport mismatch no longer sends handshake over dead TCP peer (`KeyError: unknown peer`)
- **File transfer transport** — offers and chunks use the active link's transport (serial vs TCP)
- **Transfer cancel** — `FILE_REJECT` notifies remote peer; both sides show **cancelled** in chat and transfer dock
- **CPU / temperature** — more accurate first CPU sample; temperature uses hottest CPU zone (not average)
- **HKDF session keys** — proper salt (`srltcp-session-salt-v2`) and directional info strings (`srltcp-v2-send` / `srltcp-v2-recv`)

### New / UI
- **Message actions** — Copy and Delete on text bubbles
- **Transport badges** — readable TCP / SERIAL pills on trusted and discovered contacts
- **Transfer dock** — inline above composer (no longer blocks chat); cancelled state with toast
- **Network map** — animated graph, legend, glow on active links
- **Android** — `set_android_data_dir()` before server start for correct app files path

### Project
- `SECURITY.md`, `CONTRIBUTING.md`, issue/PR templates, `.pre-commit-config.yaml`
- CI: ruff, mypy, pytest with coverage, advisory pip-audit

**Note:** Peers must both run v0.1.11+ for handshake compatibility after the HKDF change.

## v0.1.10 (2026-07-03)

### Fixes
- **Delete contact 405** — security middleware now allows `DELETE` and `PATCH` (trusted contact removal works)
- **File transfer stability** — chunk size reduced to 256 KiB with flow control; fewer disconnects on large files/images
- **Reconnect storm** — exponential backoff, skip during active transfers, cancel on successful handshake
- **Serial handshake** — retry handshake on incomplete links; connect API waits up to 12s for handshake
- **Stale send after reconnect** — messages/files auto-reconnect before send when link is down
- **Android APK** — loading screen, crash handler, 2048-bit TLS on device, longer server wait; `SRLTCP-0.1.10.apk`

### New
- **`./run.sh web --debug`** — verbose backend logging on Arch/Ubuntu/Windows
- **Settings window** — full-screen tabbed settings (General, Network, Serial, Folders, Clock, Advanced)
- **Transfer dock** — bottom progress bar with cancel on sender and receiver
- **Clock source** — sync from this machine or NTP server (configurable in settings)
- **`POST /api/transfers/{id}/cancel`** — cancel active file transfer

## v0.1.9 (2026-07-03)

### Fixes
- **Connection storm** — per-peer connect lock, duplicate TCP handshakes rejected, `force=false` by default; UI no longer auto-reconnects on every `link_down`
- **Link teardown** — only the active transport peer removes a link; stale disconnect events ignored
- **Setup folder browse** — browse modal z-index above setup overlay; delegated click handlers for setup wizard buttons
- **Trusted list** — deleting a contact updates UI immediately; trusted peers hidden from Discovered tab
- **Network map** — shows active links and dashed lines to discovered peers; includes linked nodes missing from discovery
- **Serial connect** — outbound dial path for serial transport in `connect_to_peer`
- **Status clock** — “Show clock” setting hides clock; time shown at top of sidebar (time only, timezone in settings)
- **Contact menu** — ⋮ submenu: clear chat, rename, block/unblock, delete
- **Peer notifications** — toast when remote peer connects or disconnects
- **Android APK** — `extractNativeLibs`, `buildConfig`, server-ready wait before WebView load; output as `SRLTCP-0.1.9.apk`

### API
- `PATCH /api/trusted/{hash_id}` — rename or block/unblock
- `POST /api/trusted/{hash_id}/clear-chat` — remove chat history for a contact
- `POST /api/connect` — `force` defaults to `false`

## v0.1.8 (2026-07-03)

### Fixes
- **LAN discovery** — UDP announce broadcasts to all interface subnet addresses (not only 255.255.255.255); 3-packet burst on manual announce
- **Serial announce** — uses `MessageType.ANNOUNCE` framing so peers on the RF link can decode discovery
- **Discovered peers** — TCP and serial identities listed separately with endpoint (IP:port or serial device)
- **Log spam** — quiet access logger suppresses `/api/system`, `/api/status`, `/api/peers` polling noise
- **UI polling** — reduced background fetch frequency; settings form no longer reloads interfaces on every status tick
- **Default baud** — 57600 (was 115200)
- **Android APK** — crash guards, WebView compat client, non-daemon server thread, app icon, output as `SRLTCP-0.1.8.apk`

### New
- **Timezone & clock** — timezone picker in settings; live clock in status bar
- **Setup browse** — folder browse buttons on first-run wizard (same as settings)
- **Network map** — settings → Network map visualizes self, peers, and active links

## v0.1.7 (2026-07-03)

### Fixes
- **Serial transport** — enabling serial in settings now starts the transport and shows the serial identity (no restart required)
- **Serial from settings** — `enable_serial` / port / baud loaded at startup from saved settings
- **Separate announces** — "Announce TCP" and "Announce Serial" buttons (independent discovery per transport)
- **Connection stability** — TCP disconnects detected correctly; auto-reconnect for trusted peers; `link_down` WebSocket events
- **Chat UI state** — link status syncs every 5s; no page refresh needed to send messages when encrypted/online
- **File transfer** — progress bar and MB/s in chat bubbles; files and images appear in the message window
- **Large file send** — requires active encrypted link; clearer errors when peer not connected
- **Delete contacts** — remove trusted peers from the Trusted tab (× button)
- **Android APK** — `SRLTCPApplication` for Chaquopy init; self-signed localhost TLS accepted; port fallback 9876–9878

### New
- **Uninstall scripts** — `uninstall.sh` / `uninstall.bat` remove config and app data
- **File download API** — `GET /api/transfers/{id}/file` for completed transfers in chat

## v0.1.6 (2026-07-03)

### Fixes
- **APK build** — stage `srltcp/` into `src/main/python` (avoids Gradle task validation failure from scanning `android/build`)

## v0.1.5 (2026-07-03)

### Fixes
- **APK build** — Chaquopy 15 requires one `pip.install()` call per package

## v0.1.4 (2026-07-03)

### Fixes
- **Ctrl+C shutdown** — process now exits promptly; WebSockets closed before aiohttp teardown; background loops awaited
- **APK build** — Chaquopy 15 `chaquopy` DSL (replaces deprecated `python` block); plugin versions in `settings.gradle.kts`
- **TCP transport stop** — close peer connections before server shutdown to avoid hang

## v0.1.3 (2026-07-03)

### Fixes
- **Handshake UI** — no longer stuck on "Handshaking…" after connect (race condition fixed)
- **Reconnect** — tearing down stale links before reconnect; force reconnect on each connect
- **Self in discovered** — own node filtered from peer list and purged from registry
- **Latency display** — RTT (ms) shown in chat header and peer list when connected
- **APK build** — Gradle wrapper, SDK licenses, NDK, Chaquopy buildPython fix

### New
- **Serial port picker** — dropdown lists USB devices plugged into the system
- **Baud rate picker** — selectable standard rates (9600–921600)
- **Disconnect API** — `POST /api/disconnect` for clean peer teardown

## v0.1.2 (2026-07-03)

### Fixes
- **Discovery spam removed** — "Discovered" toasts no longer fire every announce cycle; peers update silently in the list
- **Auto-announce respects settings** — when off, the announce loop stops; passive discovery still populates peers when others announce
- **Self-discovery filtered** — your own UDP broadcasts are no longer listed as discovered peers
- **File send fixed** — browser uploads files via multipart `/api/upload` before transfer

### New features
- **Ping / RTT** — measure network peer latency in milliseconds
- **Serial RF metrics** — link quality percentage and RTT for serial peers
- **Trusted peers** — must trust a discovered peer before messaging or sending files
- **Transfer progress** — progress bar with MB/s transfer speed
- **Settings panel** — TCP and serial settings, folder pickers, message retention presets
- **Identity management** — create, regenerate, or delete TCP and serial identities
- **Message retention** — 1 day, 1 week, 1 month, 1 year, forever, or until restart
- **Restart button** — restart the node from settings
- **Release notes** — click version badge (top-left) to view changelog

## v0.1.1

- HTTPS-only local web UI on 127.0.0.1
- Setup wizard and settings persistence
- Security middleware (CSP, HSTS)
- Graceful Ctrl+C shutdown
- CPU stats and port fallback

## v0.1.0

- Initial release: TCP + Serial P2P, E2EE messaging, file transfer, relay mode, web UI