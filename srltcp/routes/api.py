"""REST API routes."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from srltcp.core.node import SRLTCPNode


def register_api_routes(app: web.Application, node: SRLTCPNode) -> None:
    async def status(_request: web.Request) -> web.Response:
        return web.json_response(node.status())

    async def identities(_request: web.Request) -> web.Response:
        return web.json_response(node.backend.get_identities())

    async def peers(_request: web.Request) -> web.Response:
        return web.json_response(node.backend.get_discovered_peers())

    async def messages(request: web.Request) -> web.Response:
        limit = int(request.rel_url.query.get("limit", "200"))
        return web.json_response(node.backend.get_messages(limit=limit))

    async def send_message(request: web.Request) -> web.Response:
        data = await request.json()
        recipient = data.get("recipient_hash", "")
        text = data.get("text", "")
        transport = data.get("transport", "tcp")
        if not recipient or not text:
            return web.json_response({"error": "recipient_hash and text required"}, status=400)
        msg = await node.backend.send_message(recipient, text, transport=transport)
        if msg:
            return web.json_response(msg.to_dict())
        return web.json_response({"error": "send failed"}, status=500)

    async def connect(request: web.Request) -> web.Response:
        data = await request.json()
        hash_id = data.get("hash_id", "")
        host = data.get("host")
        port = data.get("port")
        transport = data.get("transport", "tcp")
        ok = await node.backend.connect_to_peer(hash_id, host=host, port=port, transport=transport)
        return web.json_response({"connected": ok})

    async def announce(_request: web.Request) -> web.Response:
        transport = _request.rel_url.query.get("transport")
        await node.backend.announce(transport)
        return web.json_response({"announced": True})

    async def send_file(request: web.Request) -> web.Response:
        data = await request.json()
        recipient = data.get("recipient_hash", "")
        path_str = data.get("path", "")
        transport = data.get("transport", "tcp")
        if not recipient or not path_str:
            return web.json_response({"error": "recipient_hash and path required"}, status=400)
        path = Path(path_str)
        if not path.exists():
            return web.json_response({"error": "file not found"}, status=404)
        result = await node.backend.send_file(recipient, path, transport=transport)
        if result:
            return web.json_response(result)
        return web.json_response({"error": "transfer failed"}, status=500)

    async def transfers(_request: web.Request) -> web.Response:
        return web.json_response(node.backend.list_transfers())

    async def create_share(request: web.Request) -> web.Response:
        data = await request.json()
        folder = Path(data.get("path", ""))
        owner = data.get("owner_hash", "")
        if not folder.is_dir():
            return web.json_response({"error": "invalid folder"}, status=400)
        identity = node.backend.identities.get("tcp")
        owner_hash = owner or (identity.hash_id if identity else "")
        session = node.create_share_session(folder, owner_hash)
        return web.json_response(
            {
                "session_id": session.id,
                "token": session.token,
                "expires": session.expires,
            }
        )

    async def config_get(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "name": node.config.name,
                "tcp_port": node.config.tcp_port,
                "relay_mode": node.config.relay_mode,
                "enable_tcp": node.config.enable_tcp,
                "enable_serial": node.config.enable_serial,
            }
        )

    app.router.add_get("/api/status", status)
    app.router.add_get("/api/identities", identities)
    app.router.add_get("/api/peers", peers)
    app.router.add_get("/api/messages", messages)
    app.router.add_post("/api/messages", send_message)
    app.router.add_post("/api/connect", connect)
    app.router.add_post("/api/announce", announce)
    app.router.add_post("/api/transfer", send_file)
    app.router.add_get("/api/transfers", transfers)
    app.router.add_post("/api/share/create", create_share)
    app.router.add_get("/api/config", config_get)