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


class QuietAccessLogger(AccessLogger):
    """Skip logging for polling/WebSocket endpoints."""

    def log(self, request, response, time):  # type: ignore[no-untyped-def]
        if request.path in QUIET_PATHS:
            return
        super().log(request, response, time)