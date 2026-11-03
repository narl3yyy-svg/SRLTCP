# Third-party notices

SRLTCP is GPL-3.0-or-later. It bundles or depends on the following
components under their own licenses:

| Component | Where | License |
|-----------|-------|---------|
| QR Code Generator for JavaScript 1.4.4 — © 2009 Kazuhiko Arase | `srltcp/web/static/qrcode.js` (vendored, unmodified) | MIT |
| ZXing Android Embedded 4.3.0 (and ZXing core) | Android app dependency (QR scanner) | Apache-2.0 |
| aiohttp, cryptography, pyserial, zstandard, kyber-py (MIT/Apache-2.0), dilithium-py (MIT) | Python dependencies | see each project |
| Chaquopy | Android Python runtime | MIT |
| Tor (The Tor Project; Tor Expert Bundle, unmodified) | Android app (tor-android); desktop apps; downloaded by `run.bat` / `run.sh` | BSD-3-Clause (and bundled libevent BSD-3-Clause, OpenSSL Apache-2.0) |
| Electron (Chromium, Node.js) | Desktop apps (`desktop/`) | MIT (Chromium: BSD-3-Clause and others — `LICENSES.chromium.html` in the app) |
| Python (python-build-standalone build of CPython 3.12) | Desktop apps | PSF-2.0 |
| electron-builder | Desktop app packaging (build only) | MIT |

"QR Code" is a registered trademark of DENSO WAVE INCORPORATED.
