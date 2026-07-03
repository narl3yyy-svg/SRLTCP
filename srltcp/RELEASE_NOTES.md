# SRLTCP Release Notes

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