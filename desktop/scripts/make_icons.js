// Renders the app icons from the logo in srltcp/web/static/index.html (the
// favicon). Run once after changing the logo: node scripts/make_icons.js
// (needs Playwright's Chromium; the PNGs are committed).
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const html = fs.readFileSync(path.join(__dirname, "../../srltcp/web/static/index.html"), "utf8");
const svg = decodeURIComponent(html.match(/rel="icon" href="data:image\/svg\+xml,([^"]+)"/)[1]);
const inner = svg.replace(/^<svg[^>]*>/, "").replace(/<\/svg>$/, "");
const out = (f) => path.join(__dirname, "..", f);

function page(size, { pad = 0, mono = false, bg = true }) {
  let body = inner;
  if (!bg) body = body.replace(/<rect[^>]*\/>/, "");
  if (mono) body = body.replaceAll("#3EF58E", "#000");
  const s = size - 2 * pad;
  return `<html><body style="margin:0;background:transparent">
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="18 18 72 72" width="${s}" height="${s}"
         style="position:absolute;left:${pad}px;top:${pad}px">${body}</svg></body></html>`;
}

(async () => {
  const browser = await chromium.launch();
  const shots = [
    ["build/icon.png", 1024, {}],
    // macOS icons sit inside a 824 px square on a 1024 px canvas.
    ["build/icon-mac.png", 1024, { pad: 100 }],
    ["assets/tray.png", 16, {}],
    ["assets/tray@2x.png", 32, {}],
    // macOS menu bar: black on transparent ("Template": macOS tints it).
    ["assets/trayTemplate.png", 16, { mono: true, bg: false }],
    ["assets/trayTemplate@2x.png", 32, { mono: true, bg: false }],
    ["assets/icon-256.png", 256, {}],
  ];
  for (const [file, size, opts] of shots) {
    const p = await browser.newPage({ viewport: { width: size, height: size } });
    await p.setContent(page(size, opts));
    await p.screenshot({ path: out(file), omitBackground: true });
    await p.close();
  }
  await browser.close();
  console.log("icons written");
})();
