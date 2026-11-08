# SRLTCP desktop app (Windows, macOS)

A small tray / menu-bar app around SRLTCP: it runs the SRLTCP node (the same
program as `run.bat web` / `./run.sh web`) with its own Python and Tor, and
shows the node's page in a window. Builds:

| Platform | Output | Notes |
|---|---|---|
| Windows 10/11, x64 | `dist/SRLTCP-<version>-windows-x64-setup.exe` | NSIS installer, per user (no admin rights), Start menu and desktop shortcuts, uninstaller |
| macOS 12+, Apple silicon | `dist/SRLTCP-<version>-macos-arm64.zip` | `SRLTCP.app`, ad-hoc signed (not notarized) |

Installing and using it: see *Desktop app* in the [main README](../README.md#desktop-app-windows-11-x64-and-macos-apple-silicon).

## How it works

- `main.js` (Electron) starts `python -I -B -X utf8 -m srltcp web --desktop`
  from the app's own `resources/backend` (a standalone CPython 3.12 with
  SRLTCP and its packages) and passes the bundled Tor in `SRLTCP_TOR_BINARY`.
- With `--desktop` the node prints one line, `SRLTCP-DESKTOP {"port", "token",
  "cert_sha256", "version"}`, and stops cleanly when the app closes its stdin
  (quitting, or the app crashing). A restart the node asks for (a restored
  backup, a setting that needs it) is exit code 75: the app starts it again.
- The window loads `https://127.0.0.1:<port>/?token=…`. The certificate is
  trusted only if its SHA-256 is the one the node reported (127.0.0.1 only;
  everything else gets Chromium's normal checks). Navigation outside the
  page opens in the browser; camera, microphone, notifications and clipboard
  are allowed for the page only; the page runs sandboxed, without Node.js.
- `preload.js` gives the page one function, `window.srltcpDesktop.pathForFile(file)`
  (Electron's `webUtils.getPathForFile`): a picked or dropped file is sent from
  where it is instead of being copied into SRLTCP first. Nothing else is exposed.
- Closing the window hides it (tray / menu bar). *Quit SRLTCP* stops the node
  (up to 12 s for a clean stop, then it's ended).
- Electron's own files go to `%APPDATA%\SRLTCP Desktop` / `~/Library/Application
  Support/SRLTCP Desktop` (log: `logs/srltcp.log`, without the access token);
  the node's data stays where `run.bat` / `run.sh` keep it.

## Building

```bash
bash scripts/build_desktop.sh          # both; or: win / mac
```

Needs Node.js 20+ and Python 3.12. It runs on Linux, macOS or Windows:

- `scripts/prepare_backend.py <target>` downloads CPython (python-build-standalone,
  pinned SHA-256), installs SRLTCP's packages as wheels built for the target
  (`pip --platform`, so it works from any OS), compiles the bytecode, drops
  what SRLTCP never uses (tkinter, tests, pip), and adds the Tor Expert Bundle
  (checked against the Tor Project's SHA-256; without pluggable transports).
- electron-builder (pinned in `package.json` / `package-lock.json`) packages
  the app. The Windows installer needs Wine on Linux/macOS (`wine64` and
  `wine32`).
- The Mac app is signed ad-hoc: `codesign` on a Mac, or
  [rcodesign](https://github.com/indygreg/apple-platform-rs) elsewhere (Apple
  silicon runs only signed code). It isn't notarized, so macOS asks once
  (*Open Anyway*). With an Apple Developer ID, sign and notarize instead.

## Testing

```bash
cd desktop && npm ci
python3.12 scripts/prepare_backend.py linux-x64      # (a Linux build of the same backend)
xvfb-run -a node tests/smoke.js                       # the app from source
npx electron-builder --linux --x64 && xvfb-run -a node tests/smoke.js dist/linux-unpacked/srltcp-desktop
```

The smoke test starts the app with an empty data folder and checks that the
node starts and its page loads over the pinned certificate, that another
certificate on 127.0.0.1 is refused, live updates (WebSocket), the tray
behaviour (closing hides the window, the node keeps running), a node restart
(exit code 75), that quitting frees the ports, and that the page logs no
errors. `tests/test_desktop_mode.py` covers the node's side.

Icons come from the logo in `srltcp/web/static/index.html`:
`node scripts/make_icons.js`.
