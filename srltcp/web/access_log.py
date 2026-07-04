"""Quiet aiohttp access logger — suppresses high-frequency polling routes."""

from __future__ import annotations

from aiohttp.web_log import AccessLogger

QUIET_PATHS = frozenset(
    {
        "/api/system",
        "/api/status",
        "/api/peers",
        "/api/trusted",
        "/api/transfers",
        "/ws",
    }
)

_QUIET_PREFIXES = ("/api/transfers/",)


class QuietAccessLogger(AccessLogger):
    """Skip logging for polling/WebSocket endpoints."""

    def log(self, request, response, time):  # type: ignore[no-untyped-def]
        path = request.path
        if path in QUIET_PATHS or path.startswith(_QUIET_PREFIXES):
            return
        super().log(request, response, time)