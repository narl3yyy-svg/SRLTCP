"""Shared folder browse/download routes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from srltcp.core.node import SRLTCPNode


def register_share_routes(app: web.Application, node: SRLTCPNode) -> None:
    async def list_files(request: web.Request) -> web.Response:
        session_id = request.match_info["session_id"]
        token = request.rel_url.query.get("token", "")
        entries = node.list_share(session_id, token)
        if entries is None:
            return web.json_response({"error": "invalid session"}, status=403)
        return web.json_response({"entries": entries})

    async def download(request: web.Request) -> web.Response:
        session_id = request.match_info["session_id"]
        token = request.rel_url.query.get("token", "")
        rel_path = request.rel_url.query.get("path", "")
        target = node.resolve_share_path(session_id, token, rel_path)
        if not target or not target.is_file():
            return web.json_response({"error": "not found"}, status=404)
        return web.FileResponse(target)

    app.router.add_get("/api/share/{session_id}/list", list_files)
    app.router.add_get("/api/share/{session_id}/download", download)