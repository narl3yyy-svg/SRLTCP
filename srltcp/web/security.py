"""Security middleware for local HTTPS web UI."""

from __future__ import annotations

from urllib.parse import urlparse

from aiohttp import web

ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
ALLOWED_ORIGIN_HOSTS = ALLOWED_HOSTS

SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "connect-src 'self' https://127.0.0.1:* wss://127.0.0.1:* "
        "https://localhost:* wss://localhost:*; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
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


def _is_allowed_origin(value: str) -> bool:
    host = _origin_host(value)
    return bool(host and host in ALLOWED_ORIGIN_HOSTS)


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
        if origin and not _is_allowed_origin(origin):
            raise web.HTTPForbidden(text="Invalid origin")
        if referer and not _is_allowed_origin(referer):
            raise web.HTTPForbidden(text="Invalid referer")

    response = await handler(request)
    for key, value in SECURITY_HEADERS.items():
        response.headers[key] = value
    return response


@web.middleware
async def quiet_access_log(request: web.Request, handler):  # type: ignore[no-untyped-def]
    """Suppress noisy polling endpoints from access log."""
    response = await handler(request)
    path = request.path
    if path in ("/api/peers", "/api/system", "/ws") or path.startswith("/static/"):
        response.headers["X-SRLTCP-Quiet"] = "1"
    return response