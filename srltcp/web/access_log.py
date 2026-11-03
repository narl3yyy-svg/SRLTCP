"""Quiet aiohttp access logger: the page polls the node every few seconds, so
successful requests are logged only with --debug; failures always are."""

from __future__ import annotations

import logging

from aiohttp.web_log import AccessLogger

QUIET_PATHS = frozenset(
    {
        "/api/system",
        "/api/status",
        "/api/peers",
        "/api/trusted",
        "/api/transfers",
        "/api/tor/status",
        "/ws",
    }
)

_QUIET_PREFIXES = ("/api/transfers/", "/static/")


class QuietAccessLogger(AccessLogger):
    """One short line per request: method, path (never the query string, which
    can hold the access token), status, size. Successful requests only at
    DEBUG; polling and WebSocket routes not at all."""

    def log(self, request, response, time):  # type: ignore[no-untyped-def]
        path = request.path
        if path in QUIET_PATHS or path.startswith(_QUIET_PREFIXES):
            return
        status = getattr(response, "status", 0)
        level = logging.INFO if status >= 400 else logging.DEBUG
        if not self.logger.isEnabledFor(level):
            return
        size = getattr(response, "body_length", 0)
        self.logger.log(level, "%s %s %s %s (%.0f ms)", request.method, path, status, size,
                        time * 1000)
