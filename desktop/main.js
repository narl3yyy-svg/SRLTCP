// SRLTCP desktop app (Windows, macOS): runs the SRLTCP node (bundled Python
// and Tor) in the background, shows its page in a window, and keeps it
// running in the system tray / menu bar when the window is closed.
//
// The node is the same program as `run.bat web` / `./run.sh web` (same data
// folder, same identity and contacts). It is started with `--desktop`: it
// prints one line with its port, access token and the SHA-256 of its
// localhost certificate, and stops cleanly when this app closes its stdin.
"use strict";

const { app, BrowserWindow, Menu, Notification, Tray, dialog, nativeImage, session, shell,
  systemPreferences } = require("electron");
const { spawn } = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const APP_ID = "com.srltcp.desktop";
const READY_PREFIX = "SRLTCP-DESKTOP ";
const START_TIMEOUT_MS = 90_000; // (first start: keys, certificate, Tor)
const STOP_TIMEOUT_MS = 12_000;
const LOG_KEEP = 400; // lines kept for the "couldn't start" dialog
const RESTART_CODE = 75; // (srltcp/routes/api.py: DESKTOP_RESTART_CODE)

const STARTING_PAGE = `<!doctype html><html><head><meta charset="utf-8"><title>SRLTCP</title>
<style>html,body{height:100%;margin:0;background:#06080C;color:#9fb3c8;font:15px system-ui,sans-serif}
body{display:flex;align-items:center;justify-content:center}</style></head>
<body><div>Starting SRLTCP…</div></body></html>`;

let win = null;
let tray = null;
let backend = null;
let info = null; // { port, token, cert_sha256, version }
let quitting = false;
let stopped = false;
let restarts = [];
const logLines = [];
let logStream = null;
const startHidden = process.argv.includes("--hidden");

// Electron's own files (caches, page storage, logs) in their own folder. By
// default they'd go to %APPDATA%\SRLTCP on Windows: the node's data folder,
// with your identity and chats, which run.bat uses too.
app.setPath("userData", path.join(app.getPath("appData"), "SRLTCP Desktop"));
app.setAppLogsPath(path.join(app.getPath("userData"), "logs"));

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => showWindow());
}
if (process.platform === "win32") app.setAppUserModelId(APP_ID);

// ── The node ──

function backendRoot() {
  if (process.env.SRLTCP_DESKTOP_BACKEND) return process.env.SRLTCP_DESKTOP_BACKEND;
  if (app.isPackaged) return path.join(process.resourcesPath, "backend");
  const arch = process.arch === "arm64" ? "arm64" : "x64";
  const os = { win32: "win", darwin: "mac" }[process.platform] || "linux";
  return path.join(__dirname, "backend", `${os}-${arch}`);
}

function backendPaths() {
  const root = backendRoot();
  const exe = process.platform === "win32" ? ".exe" : "";
  const python = process.platform === "win32"
    ? path.join(root, "python", "python.exe")
    : path.join(root, "python", "bin", "python3");
  const tor = path.join(root, "tor", "tor", `tor${exe}`);
  return { root, python, tor };
}

function log(line) {
  const text = String(line).replace(/\?token=[A-Za-z0-9_-]+/g, "?token=…");
  logLines.push(text);
  if (logLines.length > LOG_KEEP) logLines.splice(0, logLines.length - LOG_KEEP);
  try {
    logStream?.write(`${new Date().toISOString()} ${text}\n`);
  } catch (_) { /* (log file gone) */ }
  if (process.env.SRLTCP_DESKTOP_DEBUG) console.log(text);
}

function openLog() {
  const dir = app.getPath("logs");
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, "srltcp.log");
  try {
    if (fs.existsSync(file) && fs.statSync(file).size > 5 * 1024 * 1024) {
      fs.renameSync(file, `${file}.1`);
    }
  } catch (_) { /* keep appending */ }
  logStream = fs.createWriteStream(file, { flags: "a" });
  return file;
}

function startBackend() {
  const { python, tor, root } = backendPaths();
  if (!fs.existsSync(python)) {
    fatal(`SRLTCP's Python wasn't found at\n${python}\n\nReinstall SRLTCP.`);
    return;
  }
  info = null;
  stopped = false;
  const env = { ...process.env, SRLTCP_DESKTOP: "1" };
  if (fs.existsSync(tor)) env.SRLTCP_TOR_BINARY = tor;
  // -I: ignore PYTHON* variables and user packages; -B: write nothing into
  // the app (it's signed on macOS; the bytecode is compiled at build time).
  // -X utf8: UTF-8 output on Windows too (-I ignores PYTHONUTF8).
  const args = ["-I", "-B", "-X", "utf8", "-m", "srltcp", "web", "--desktop"];
  log(`Starting the SRLTCP node (${app.getVersion()}) from ${root}`);
  backend = spawn(python, args, {
    cwd: root, env, windowsHide: true, stdio: ["pipe", "pipe", "pipe"],
  });
  const timer = setTimeout(() => {
    if (!info) fatal("The SRLTCP node didn't start in time.", true);
  }, START_TIMEOUT_MS);
  let buffer = "";
  const onData = (chunk) => {
    buffer += chunk.toString("utf8");
    let nl;
    while ((nl = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, nl).replace(/\r$/, "");
      buffer = buffer.slice(nl + 1);
      if (line.startsWith(READY_PREFIX)) {
        try {
          info = JSON.parse(line.slice(READY_PREFIX.length));
        } catch (e) {
          log(`Bad ready line: ${e}`);
          continue;
        }
        clearTimeout(timer);
        log(`Node ready on port ${info.port}`);
        onReady();
      } else if (line.trim()) {
        log(line);
      }
    }
  };
  backend.stdout.on("data", onData);
  backend.stderr.on("data", (c) => c.toString("utf8").split(/\r?\n/).forEach((l) => l && log(l)));
  backend.stdin.on("error", () => {}); // (it may already be gone)
  backend.on("error", (err) => {
    clearTimeout(timer);
    fatal(`Couldn't start the SRLTCP node: ${err.message}`, true);
  });
  backend.on("exit", (code, signal) => {
    clearTimeout(timer);
    log(`Node stopped (code ${code}${signal ? `, ${signal}` : ""})`);
    backend = null;
    stopped = true;
    if (quitting) return;
    const wasReady = !!info;
    info = null;
    if (code === RESTART_CODE) {
      // It asked to be started again (a restored backup, a setting that needs it).
      setTimeout(startBackend, 300);
      return;
    }
    if (!wasReady) {
      fatal("The SRLTCP node stopped while starting.", true);
      return;
    }
    // It ran and stopped by itself (e.g. after restoring a backup, or a
    // crash): start it again, but not in a loop.
    const now = Date.now();
    restarts = restarts.filter((t) => now - t < 60_000);
    if (restarts.length < 3) {
      restarts.push(now);
      setTimeout(startBackend, 1000);
    } else {
      fatal("The SRLTCP node keeps stopping.", true);
    }
  });
}

function stopBackend() {
  return new Promise((resolve) => {
    if (!backend || stopped) return resolve();
    const proc = backend;
    const done = () => resolve();
    proc.once("exit", done);
    try {
      proc.stdin.end(); // the node stops cleanly (closes its ports, saves)
    } catch (_) { /* gone */ }
    setTimeout(() => {
      if (!stopped) {
        log("Node didn't stop in time: ending it");
        try { proc.kill(); } catch (_) { /* gone */ }
      }
      setTimeout(done, 1500);
    }, STOP_TIMEOUT_MS);
  });
}

function fatal(message, withLog = false) {
  log(message);
  if (quitting) return;
  const tail = logLines.slice(-14).join("\n");
  const already = /another srltcp instance|could not bind|address already in use|only one usage of each socket address|errno 98|errno 48|10048/i.test(tail);
  const detail = already
    ? "Another copy of SRLTCP seems to be running (run.bat / run.sh, or the app for another user). Close it and start SRLTCP again."
    : (withLog ? `Last lines of the log (${app.getPath("logs")}):\n\n${tail}` : "");
  dialog.showMessageBoxSync({
    type: "error", title: "SRLTCP", message, detail, buttons: ["Quit"],
  });
  quitting = true;
  stopBackend().then(() => app.exit(1));
}

// ── The window ──

function origin() {
  return info ? `https://127.0.0.1:${info.port}` : "";
}

function sameOrigin(url) {
  try {
    return !!info && new URL(url).origin === origin();
  } catch (_) {
    return false;
  }
}

function certMatches(cert) {
  // Electron gives "sha256/<base64 of the DER's SHA-256>"; the node gives hex.
  if (!info || !cert) return false;
  try {
    const der = cert.data
      ? Buffer.from(cert.data.replace(/-----[^-]+-----/g, "").replace(/\s+/g, ""), "base64")
      : null;
    const hex = der ? crypto.createHash("sha256").update(der).digest("hex") : "";
    const fromFp = cert.fingerprint?.startsWith("sha256/")
      ? Buffer.from(cert.fingerprint.slice(7), "base64").toString("hex") : "";
    const want = String(info.cert_sha256 || "").toLowerCase();
    return !!want && (hex === want || fromFp === want);
  } catch (_) {
    return false;
  }
}

function setUpSession() {
  const ses = session.defaultSession;
  ses.setUserAgent(`${ses.getUserAgent()} SRLTCPDesktop/${app.getVersion()}`);
  // The page's certificate is self-made by the node: trust exactly that one
  // (its SHA-256 came from the node itself), on 127.0.0.1 only.
  ses.setCertificateVerifyProc((req, callback) => {
    if (req.hostname === "127.0.0.1" || req.hostname === "localhost") {
      callback(certMatches(req.certificate) ? 0 : -2);
    } else {
      callback(-3); // (anything else: Chromium's normal checks)
    }
  });
  const allowed = new Set([
    "media", "notifications", "clipboard-sanitized-write", "clipboard-read", "fullscreen",
    "speaker-selection", "fileSystem",
  ]);
  ses.setPermissionRequestHandler((wc, permission, callback, details) => {
    const ok = allowed.has(permission) && sameOrigin(details.requestingUrl || wc.getURL());
    if (ok && permission === "media" && process.platform === "darwin") {
      // macOS asks once per app for the camera and microphone.
      const kinds = details.mediaTypes?.length ? details.mediaTypes : ["audio"];
      Promise.all(kinds.map((k) => systemPreferences.askForMediaAccess(
        k === "video" ? "camera" : "microphone")))
        .then((answers) => callback(answers.every(Boolean)))
        .catch(() => callback(false));
      return;
    }
    callback(ok);
  });
  ses.setPermissionCheckHandler((_wc, permission, requestingOrigin) =>
    allowed.has(permission) && sameOrigin(requestingOrigin));
  ses.setDevicePermissionHandler(() => false);
}

function createWindow() {
  win = new BrowserWindow({
    width: 1200,
    height: 820,
    minWidth: 380,
    minHeight: 520,
    show: false,
    title: "SRLTCP",
    backgroundColor: "#06080C",
    icon: path.join(__dirname, "assets", "icon-256.png"),
    autoHideMenuBar: true,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: true,
      preload: path.join(__dirname, "preload.js"), // (window.srltcpDesktop.pathForFile only)
    },
  });
  win.removeMenu?.();
  const wc = win.webContents;
  win.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(STARTING_PAGE)}`);
  // Only the node's own page in this window; links elsewhere open in the browser.
  wc.on("will-navigate", (event, url) => {
    if (!sameOrigin(url)) {
      event.preventDefault();
      openExternal(url);
    }
  });
  wc.setWindowOpenHandler(({ url }) => {
    if (sameOrigin(url)) {
      return {
        action: "allow",
        overrideBrowserWindowOptions: {
          autoHideMenuBar: true,
          webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
        },
      };
    }
    openExternal(url);
    return { action: "deny" };
  });
  wc.on("render-process-gone", (_e, details) => {
    log(`Page process gone: ${details.reason}`);
    if (!quitting && info) setTimeout(() => loadPage(), 1000);
  });
  win.on("page-title-updated", (_e, title) => showUnread(title));
  win.on("close", (event) => {
    if (quitting) return;
    event.preventDefault(); // keep running in the tray / menu bar
    win.hide();
    remindTray();
  });
  win.once("ready-to-show", () => {
    if (!startHidden) win.show();
  });
}

function loadPage() {
  if (!win || !info) return;
  win.loadURL(`${origin()}/?token=${encodeURIComponent(info.token)}`).catch((e) => {
    log(`Page didn't load: ${e.message}`);
  });
}

function openExternal(url) {
  try {
    const u = new URL(url);
    if (["https:", "http:", "mailto:"].includes(u.protocol)) shell.openExternal(url);
  } catch (_) { /* not a URL */ }
}

function showWindow() {
  if (!win) return;
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
  if (process.platform === "darwin") app.dock?.show();
}

function onReady() {
  if (!win) createWindow();
  loadPage();
  updateTray();
}

// ── Tray / menu bar ──

let reminded = false;
function remindTray() {
  if (reminded || !Notification.isSupported()) return;
  reminded = true;
  new Notification({
    title: "SRLTCP is still running",
    body: process.platform === "darwin"
      ? "Messages and calls still come in. Quit from the menu bar icon."
      : "Messages and calls still come in. Quit from the tray icon (next to the clock).",
    silent: true,
  }).show();
}

function showUnread(title) {
  const m = /^\((\d+\+?)\)/.exec(title || "");
  const count = m ? m[1] : "";
  if (process.platform === "darwin") app.dock?.setBadge(count);
  else if (win && count && !win.isFocused()) win.flashFrame(true);
  tray?.setToolTip(count ? `SRLTCP — ${count} unread` : "SRLTCP");
}

function trayImage() {
  if (process.platform === "darwin") {
    const img = nativeImage.createFromPath(path.join(__dirname, "assets", "trayTemplate.png"));
    img.setTemplateImage(true);
    return img;
  }
  return nativeImage.createFromPath(path.join(__dirname, "assets", "tray@2x.png"))
    .resize({ width: 16, height: 16 });
}

function loginItemOpen() {
  try {
    return app.getLoginItemSettings({ args: ["--hidden"] }).openAtLogin;
  } catch (_) {
    return false;
  }
}

function updateTray() {
  if (!tray) {
    tray = new Tray(trayImage());
    tray.setToolTip("SRLTCP");
    tray.on("click", () => showWindow()); // (Windows: a click opens the window)
  }
  const status = info ? `SRLTCP ${info.version} — running` : "SRLTCP — starting…";
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: status, enabled: false },
    { type: "separator" },
    { label: "Open SRLTCP", click: () => showWindow(), enabled: !!info },
    {
      label: process.platform === "darwin" ? "Open at Login" : "Start when I sign in",
      type: "checkbox",
      checked: loginItemOpen(),
      click: (item) => {
        app.setLoginItemSettings({ openAtLogin: item.checked, args: ["--hidden"] });
        updateTray();
      },
    },
    { label: "Show log file", click: () => shell.showItemInFolder(path.join(app.getPath("logs"), "srltcp.log")) },
    { type: "separator" },
    { label: "Quit SRLTCP", click: () => quit() },
  ]));
}

function quit() {
  if (quitting) return;
  quitting = true;
  log("Quitting");
  stopBackend().then(() => app.exit(0));
}

// ── App lifecycle ──

app.on("before-quit", (event) => {
  // Cmd+Q, logging out, the installer closing the app: stop the node first.
  if (!stopped && backend) {
    event.preventDefault();
    quit();
  } else {
    quitting = true;
  }
});
app.on("window-all-closed", () => { /* stays in the tray */ });
app.on("activate", () => showWindow()); // (macOS: Dock icon clicked)

app.whenReady().then(() => {
  if (process.platform === "darwin") {
    Menu.setApplicationMenu(Menu.buildFromTemplate([
      { role: "appMenu" }, { role: "editMenu" }, { role: "viewMenu" }, { role: "windowMenu" },
    ]));
    if (startHidden) app.dock?.hide();
  } else {
    Menu.setApplicationMenu(null);
  }
  log(`Log: ${openLog()}`);
  setUpSession();
  createWindow();
  updateTray();
  startBackend();
});

