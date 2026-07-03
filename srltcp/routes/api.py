"""REST API routes."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from srltcp.core.settings import AppSettings, SettingsStore
from srltcp.utils.network import list_interfaces
from srltcp.utils.system_stats import system_stats

if TYPE_CHECKING:
    from srltcp.core.node import SRLTCPNode


def _validate_path(path_str: str, *, must_exist: bool = False) -> Path | None:
    if not path_str or ".." in path_str:
        return None
    try:
        p = Path(path_str).expanduser().resolve()
    except (OSError, ValueError):
        return None
    if must_exist and not p.exists():
        return None
    return p


def register_api_routes(app: web.Application, node: SRLTCPNode) -> None:
    store = SettingsStore()

    async def status(_request: web.Request) -> web.Response:
        return web.json_response(node.status())

    async def identities(_request: web.Request) -> web.Response:
        return web.json_response(node.backend.get_identities())

    async def peers(_request: web.Request) -> web.Response:
        return web.json_response(node.backend.get_discovered_peers())

    async def messages(request: web.Request) -> web.Response:
        limit = min(int(request.rel_url.query.get("limit", "200")), 1000)
        return web.json_response(node.backend.get_messages(limit=limit))

    async def send_message(request: web.Request) -> web.Response:
        data = await request.json()
        recipient = data.get("recipient_hash", "")
        text = str(data.get("text", ""))[:65536]
        transport = data.get("transport", "tcp")
        if not recipient or not text.strip():
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
        path = _validate_path(path_str, must_exist=True)
        if not path or not path.is_file():
            return web.json_response({"error": "file not found"}, status=404)
        result = await node.backend.send_file(recipient, path, transport=transport)
        if result:
            return web.json_response(result)
        return web.json_response({"error": "transfer failed"}, status=500)

    async def transfers(_request: web.Request) -> web.Response:
        return web.json_response(node.backend.list_transfers())

    async def create_share(request: web.Request) -> web.Response:
        data = await request.json()
        folder_str = data.get("path", "")
        owner = data.get("owner_hash", "")
        folder = _validate_path(folder_str, must_exist=True) if folder_str else None
        if folder_str and (not folder or not folder.is_dir()):
            folder = node.settings.resolved_shared_folder()
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

    async def settings_get(_request: web.Request) -> web.Response:
        return web.json_response(node.settings.to_dict())

    async def settings_post(request: web.Request) -> web.Response:
        data = await request.json()
        current = node.settings
        updated = AppSettings.from_dict({**current.to_dict(), **data})
        updated.message_retention_hours = max(1, min(updated.message_retention_hours, 8760))
        updated.web_port = max(1024, min(updated.web_port, 65535))

        for field_name in ("incoming_files_dir", "shared_folder"):
            val = getattr(updated, field_name)
            if val:
                p = _validate_path(val)
                if not p:
                    return web.json_response({"error": f"invalid {field_name}"}, status=400)

        updated.setup_complete = True
        store.save(updated)
        node.apply_settings(updated)
        return web.json_response(updated.to_dict())

    async def interfaces(_request: web.Request) -> web.Response:
        return web.json_response({"interfaces": list_interfaces()})

    async def system(_request: web.Request) -> web.Response:
        return web.json_response(system_stats())

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
    app.router.add_get("/api/settings", settings_get)
    app.router.add_post("/api/settings", settings_post)
    app.router.add_get("/api/interfaces", interfaces)
    app.router.add_get("/api/system", system)