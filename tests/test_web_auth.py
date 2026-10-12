"""Local web UI access-token tests."""

from __future__ import annotations

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from srltcp.web.auth import COOKIE_NAME, auth_middleware, load_or_create_token
from srltcp.web.security import security_middleware

HOST = {"Host": "127.0.0.1:9876"}


def _app(token: str) -> web.Application:
    async def ok(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def index(_request: web.Request) -> web.Response:
        return web.Response(text="ui", content_type="text/html")

    app = web.Application(middlewares=[security_middleware, auth_middleware(token)])
    app.router.add_get("/", index)
    app.router.add_get("/api/messages", ok)
    app.router.add_get("/static/app.js", ok)
    return app


async def test_api_requires_token() -> None:
    async with TestClient(TestServer(_app("t" * 43))) as client:
        resp = await client.get("/api/messages", headers=HOST)
        assert resp.status == 401
        resp = await client.get(
            "/api/messages", headers={**HOST, "Authorization": "Bearer wrong"}
        )
        assert resp.status == 401
        resp = await client.get(
            "/api/messages", headers={**HOST, "Authorization": "Bearer " + "t" * 43}
        )
        assert resp.status == 200


async def test_static_assets_public_and_index_locked() -> None:
    async with TestClient(TestServer(_app("t" * 43))) as client:
        assert (await client.get("/static/app.js", headers=HOST)).status == 200
        resp = await client.get("/", headers=HOST)
        assert resp.status == 401
        assert "locked" in await resp.text()


async def test_query_token_sets_cookie_and_redirects() -> None:
    token = "x" * 43
    async with TestClient(TestServer(_app(token))) as client:
        resp = await client.get(f"/?token={token}", headers=HOST, allow_redirects=False)
        assert resp.status == 302
        assert resp.headers["Location"] == "/"
        cookie = resp.cookies[COOKIE_NAME]
        assert cookie.value == token
        assert cookie["httponly"]
        assert cookie["samesite"] == "Strict"
        resp = await client.get(
            "/api/messages", headers={**HOST, "Cookie": f"{COOKIE_NAME}={token}"}
        )
        assert resp.status == 200
        bad = await client.get("/?token=nope", headers=HOST, allow_redirects=False)
        assert bad.status == 401


def test_token_persisted(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "web_token"
    t1 = load_or_create_token(path)
    assert len(t1) >= 32
    assert load_or_create_token(path) == t1
    assert (path.stat().st_mode & 0o777) == 0o600
