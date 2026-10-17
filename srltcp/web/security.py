"""Security middleware for local HTTPS web UI."""

from __future__ import annotations

import json
from urllib.parse import urlparse

from aiohttp import web

ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
ALLOWED_ORIGIN_HOSTS = ALLOWED_HOSTS

SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # Camera/microphone are used for QR scanning and calls (same origin only).
    "Permissions-Policy": "camera=(self), microphone=(self), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        # Inline style *attributes* (avatar colours, progress bars) only —
        # no inline <style> blocks or scripts.
        "style-src-attr 'unsafe-inline'; "
        "font-src 'self'; "
        "connect-src 'self' https://127.0.0.1:* wss://127.0.0.1:* "
        "https://localhost:* wss://localhost:*; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob: mediastream:; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    ),
}


def _origin_host(value: str) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in ("https", "http"):
        return None
    host = (parsed.hostname or "").lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host or None


def _own_port(request: web.Request) -> int:
    """The port this request was made to ("[::1]:9876", "127.0.0.1:9876")."""
    host = request.host
    tail = host.rsplit("]", 1)[-1] if host.startswith("[") else host
    _name, _, port = tail.rpartition(":")
    return int(port) if port.isdigit() else 443


def _is_allowed_origin(value: str, request: web.Request | None = None) -> bool:
    """Exactly this web UI: https, a localhost name, and (given the request)
    the port it is served on. Another port on the same machine is another
    site (anything could listen there), and so is plain http."""
    parsed = urlparse(value or "")
    host = _origin_host(value)
    if not host or host not in {h.strip("[]") for h in ALLOWED_ORIGIN_HOSTS}:
        return False
    if parsed.scheme != "https":
        return False
    if request is None:
        return True
    try:
        port = parsed.port or 443
    except ValueError:
        return False
    return port == _own_port(request)


@web.middleware
async def security_middleware(request: web.Request, handler):  # type: ignore[no-untyped-def]
    host = request.host.split(":")[0].lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host not in ALLOWED_HOSTS:
        raise web.HTTPForbidden(text="SRLTCP web UI is localhost-only")

    if request.method not in ("GET", "HEAD", "OPTIONS", "POST", "DELETE", "PATCH"):
        raise web.HTTPMethodNotAllowed(
            request.method, ["GET", "HEAD", "OPTIONS", "POST", "DELETE", "PATCH"]
        )

    if request.method in ("POST", "DELETE", "PATCH"):
        origin = request.headers.get("Origin", "")
        referer = request.headers.get("Referer", "")
        if origin and not _is_allowed_origin(origin, request):
            raise web.HTTPForbidden(text="Invalid origin")
        if referer and not _is_allowed_origin(referer, request):
            raise web.HTTPForbidden(text="Invalid referer")
        bearer = request.headers.get("Authorization", "").startswith("Bearer ")
        if not origin and not bearer:
            # Browsers always say where a request comes from; a change made
            # with only the cookie and no origin is not from this page.
            raise web.HTTPForbidden(text="Missing origin")

    try:
        response = await handler(request)
    except json.JSONDecodeError:
        response = web.json_response({"error": "invalid JSON"}, status=400)
    for key, value in SECURITY_HEADERS.items():
        response.headers[key] = value
    if request.path.startswith("/api/transfers/") and request.path.endswith("/file"):
        # A received file (an SVG, an HTML page) opened on its own never runs
        # script with this page's rights.
        response.headers["Content-Security-Policy"] += "; sandbox"
    return response


@web.middleware
async def quiet_access_log(request: web.Request, handler):  # type: ignore[no-untyped-def]
    """Suppress noisy polling endpoints from access log."""
    response = await handler(request)
    path = request.path
    if path in ("/api/peers", "/api/system", "/ws") or path.startswith("/static/"):
        response.headers["X-SRLTCP-Quiet"] = "1"
    return response