// Desktop app smoke test: starts the app (Linux build of the same code, a
// fresh data folder), checks the page, the tray behaviour, a node restart
// and that quitting stops the node. Run under a display:
//   xvfb-run -a node tests/smoke.js [path/to/app/executable]
"use strict";
const { _electron: electron } = require("playwright");
const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");

const exe = process.argv[2] || require("electron");
const args = process.argv[2] ? ["--no-sandbox"] : [path.join(__dirname, ".."), "--no-sandbox"];
const data = fs.mkdtempSync(path.join(os.tmpdir(), "srltcp-desktop-"));
const fail = (m) => { console.error("FAIL:", m); process.exitCode = 1; };
const portFree = (port) => new Promise((res) => {
  const s = net.createServer().once("error", () => res(false))
    .once("listening", () => s.close(() => res(true))).listen(port, "127.0.0.1");
});

(async () => {
  const env = { ...process.env, SRLTCP_DATA_DIR: data };
  if (!process.argv[2]) env.SRLTCP_DESKTOP_BACKEND = path.join(__dirname, "..", "backend", "linux-x64");
  const app = await electron.launch({ executablePath: exe, args, env, timeout: 60000 });
  const page = await app.firstWindow();
  const consoleErrors = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", (e) => consoleErrors.push(String(e)));
  const ok = async (what, fn, ms = 60000) => {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) {
      try { if (await fn()) { console.log("ok:", what, `(${((Date.now() - t0) / 1000).toFixed(1)} s)`); return true; } } catch (_) { /* retry */ }
      await new Promise((r) => setTimeout(r, 300));
    }
    fail(what);
    return false;
  };
  await ok("the node's page loads over the pinned certificate", async () =>
    page.url().startsWith("https://127.0.0.1:") && (await page.locator("#me-name").count()) > 0);
  await ok("the page knows it's the desktop app", () =>
    page.evaluate(() => document.documentElement.classList.contains("desktop-app")));
  await ok("API answers (the access token works)", () =>
    page.evaluate(async () => (await fetch("/api/version")).ok));
  const port = Number(new URL(page.url()).port);
  await ok("live updates (WebSocket) connect through the pinned certificate", () =>
    page.evaluate(() => new Promise((res) => {
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(`${proto}//${location.host}/ws`);
      ws.onopen = () => { ws.close(); res(true); };
      ws.onerror = () => res(false);
      setTimeout(() => res(false), 5000);
    })));
  await new Promise((r) => setTimeout(r, 2500));
  if (process.env.SMOKE_SCREENSHOT) await page.screenshot({ path: process.env.SMOKE_SCREENSHOT });
  // Only the node's own certificate is trusted: another self-signed one on
  // 127.0.0.1 (another program pretending to be the node) is refused.
  const { execFileSync } = require("child_process");
  const https = require("https");
  execFileSync("openssl", ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
    "-subj", "/CN=127.0.0.1", "-keyout", path.join(data, "k.pem"), "-out", path.join(data, "c.pem")],
  { stdio: "ignore" });
  const impostor = https.createServer({ key: fs.readFileSync(path.join(data, "k.pem")),
    cert: fs.readFileSync(path.join(data, "c.pem")) }, (_q, r) => r.end("impostor"));
  await new Promise((r) => impostor.listen(0, "127.0.0.1", r));
  const result = await app.evaluate(async ({ net }, p) => {
    try { return await (await net.fetch(`https://127.0.0.1:${p}/`)).text(); } catch (e) { return `refused: ${e.message || e}`; }
  }, impostor.address().port);
  impostor.close();
  if (result === "impostor") fail("another program's certificate on 127.0.0.1 was trusted");
  else console.log("ok: another certificate on 127.0.0.1 is refused —", result.slice(0, 50));
  const own = await app.evaluate(async ({ net }, p) => {
    try { return (await net.fetch(`https://127.0.0.1:${p}/api/version`)).status; } catch (e) { return String(e); }
  }, port);
  if (typeof own !== "number") fail(`the node's own certificate isn't trusted: ${own}`);

  // Close the window: the app keeps running (tray), the node too.
  const win = await app.browserWindow(page);
  await win.evaluate((w) => w.close());
  await ok("closing the window hides it, node still running", async () =>
    !(await win.evaluate((w) => w.isVisible())) && !(await portFree(port)));

  // Restart (a restored backup / a setting that needs it): exit code 75.
  await win.evaluate((w) => w.show());
  await page.evaluate(() => fetch("/api/restart", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }).catch(() => {}));
  await new Promise((r) => setTimeout(r, 3000));
  const logFile = path.join(os.homedir(), ".config", "SRLTCP Desktop", "logs", "srltcp.log");
  await ok("the node is started again after a restart", async () =>
    /Node stopped \(code 75\)/.test(fs.readFileSync(logFile, "utf8")) &&
    page.url().startsWith("https://127.0.0.1:") && (await page.evaluate(async () => (await fetch("/api/version")).ok)));

  // Quit: the node stops and frees its ports.
  const t0 = Date.now();
  await app.evaluate(({ app }) => app.quit()).catch(() => {});
  await app.waitForEvent("close", { timeout: 30000 }).catch(() => {});
  await ok("quitting stops the node (ports free)", async () => (await portFree(port)) && (await portFree(7825)), 20000);
  console.log(`   quit took ${((Date.now() - t0) / 1000).toFixed(1)} s`);
  const log = path.join(os.homedir(), ".config", "SRLTCP Desktop", "logs", "srltcp.log");
  if (fs.existsSync(log)) {
    const text = fs.readFileSync(log, "utf8");
    if (/token=[A-Za-z0-9_-]{10,}/.test(text)) fail("the access token ended up in the log file");
    const errs = text.split("\n").filter((l) => /Traceback|ERROR/.test(l));
    if (errs.length) console.log("   log errors:\n   " + errs.slice(-5).join("\n   "));
  }
  const real = consoleErrors.filter((e) => !/favicon|ERR_CONNECTION_REFUSED|WebSocket/i.test(e));
  if (real.length) fail(`page errors:\n   ${real.slice(0, 8).join("\n   ")}`);
  else console.log("ok: no errors in the page");
  fs.rmSync(data, { recursive: true, force: true });
  console.log(process.exitCode ? "SMOKE TEST FAILED" : "SMOKE TEST PASSED");
})().catch((e) => { console.error(e); process.exit(1); });
