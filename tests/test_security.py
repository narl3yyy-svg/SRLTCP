"""Security middleware tests."""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from srltcp.web.security import security_middleware


@pytest.mark.asyncio
async def test_delete_and_patch_allowed() -> None:
    async def ok_handler(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[security_middleware])
    app.router.add_delete("/api/trusted/{hash_id}", ok_handler)
    app.router.add_patch("/api/trusted/{hash_id}", ok_handler)

    async with TestClient(TestServer(app)) as client:
        resp = await client.delete(
            "/api/trusted/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            headers={"Host": "127.0.0.1:9876"},
        )
        assert resp.status == 200
        resp2 = await client.patch(
            "/api/trusted/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            json={"name": "x"},
            headers={"Host": "127.0.0.1:9876", "Origin": "https://127.0.0.1:9876"},
        )
        assert resp2.status == 200