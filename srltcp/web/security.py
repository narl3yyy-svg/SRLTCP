"""Security middleware for local HTTPS web UI."""

from __future__ import annotations

from aiohttp import web

ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})

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
        "connect-src 'self' https://127.0.0.1:* wss://127.0.0.1:*; "
        "img-src 'self' data:; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    ),
}


@web.middleware
async def security_middleware(request: web.Request, handler):  # type: ignore[no-untyped-def]
    host = request.host.split(":")[0].lower()
    if host not in ALLOWED_HOSTS:
        raise web.HTTPForbidden(text="SRLTCP web UI is localhost-only")

    if request.method not in ("GET", "HEAD", "OPTIONS", "POST"):
        raise web.HTTPMethodNotAllowed(request.method, ["GET", "HEAD", "OPTIONS", "POST"])

    # Origin check for state-changing requests
    if request.method == "POST":
        origin = request.headers.get("Origin", "")
        if origin:
            origin_host = origin.split("//")[-1].split(":")[0].lower()
            if origin_host not in ALLOWED_HOSTS:
                raise web.HTTPForbidden(text="Invalid origin")

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