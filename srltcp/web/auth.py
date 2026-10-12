"""Per-install access token for the local web UI and API.

The UI listens on 127.0.0.1, but on a phone every other installed app (and on
a desktop every local process) can reach loopback. Every request therefore has
to present the token, either as the ``srltcp_session`` cookie (set when the
browser first opens ``/?token=...``) or as ``Authorization: Bearer <token>``.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from aiohttp import web

from srltcp.utils.platform import data_dir

COOKIE_NAME = "srltcp_session"
# Static assets carry no data; everything else requires the token.
PUBLIC_PATHS = frozenset({"/favicon.ico"})
PUBLIC_PREFIXES = ("/static/",)


def token_path() -> Path:
    return data_dir() / "web_token"


def load_or_create_token(path: Path | None = None) -> str:
    path = path or token_path()
    try:
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token)
    return token


def _presented_token(request: web.Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get(COOKIE_NAME, "")


def _locked_page() -> web.Response:
    return web.Response(
        status=401,
        content_type="text/html",
        text=(
            "<!doctype html><meta charset=utf-8><title>SRLTCP</title>"
            "<body style='font-family:sans-serif;background:#0c0e14;color:#e6e6e6;"
            "padding:2rem'><h1>SRLTCP is locked</h1><p>Open the link printed in the "
            "terminal when SRLTCP started (it contains <code>?token=</code>), or run "
            "<code>srltcp url</code> to print it again.</p></body>"
        ),
    )


def auth_middleware(token: str):  # type: ignore[no-untyped-def]
    @web.middleware
    async def _auth(request: web.Request, handler):  # type: ignore[no-untyped-def]
        path = request.path
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return await handler(request)

        query_token = request.rel_url.query.get("token", "")
        if path == "/" and query_token:
            if not secrets.compare_digest(query_token, token):
                return _locked_page()
            # Exchange the URL token for a cookie and drop it from the address bar.
            resp = web.HTTPFound("/")
            resp.set_cookie(
                COOKIE_NAME,
                token,
                httponly=True,
                secure=True,
                samesite="Strict",
                path="/",
                max_age=10 * 365 * 86400,
            )
            raise resp

        presented = _presented_token(request)
        if not presented or not secrets.compare_digest(presented, token):
            if path == "/":
                return _locked_page()
            raise web.HTTPUnauthorized(text="missing or invalid SRLTCP access token")
        return await handler(request)

    return _auth
