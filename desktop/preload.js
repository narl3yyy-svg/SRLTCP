// The only thing the desktop app adds to SRLTCP's page: where on disk a file
// you picked or dropped is, so SRLTCP sends it from there instead of first
// copying it (a 4 GB file starts at once). Nothing else is exposed; the page
// stays sandboxed, without Node.js.
"use strict";

const { contextBridge, webUtils } = require("electron");

contextBridge.exposeInMainWorld("srltcpDesktop", {
  pathForFile(file) {
    try {
      return webUtils.getPathForFile(file) || "";
    } catch (_) {
      return ""; // (not a file from this computer: the page copies it as before)
    }
  },
});
