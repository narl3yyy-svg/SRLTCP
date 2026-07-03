"""REST API routes."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from srltcp import __version__
from srltcp.core.trusted import TrustedPeer
from srltcp.utils.folders import list_directory

if TYPE_CHECKING:
    from srltcp.core.node import SRLTCPNode

from srltcp.utils.platform import data_dir

RELEASE_NOTES_PATH = Path(__file__).resolve().parents[1] / "RELEASE_NOTES.md"


def register_api_routes(app: web.Application, node: SRLTCPNode) -> None:
    upload_dir = data_dir() / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    async def status(_request: web.Request) -> web.Response:
        return web.json_response(node.status())

    async def identities(_request: web.Request) -> web.Response:
        return web.json_response(node.backend.get_identities())

    async def peers(_request: web.Request) -> web.Response:
        return web.json_response(node.backend.get_discovered_peers())

    async def trusted_list(_request: web.Request) -> web.Response:
        return web.json_response(node.backend.get_trusted_peers())

    async def trusted_add(request: web.Request) -> web.Response:
        data = await request.json()
        hash_id = data.get("hash_id", "")
        if not hash_id:
            return web.json_response({"error": "hash_id required"}, status=400)
        peer = node.add_trusted_from_discovered(hash_id)
        if not peer:
            name = data.get("name", "peer")
            transport = data.get("transport", "tcp")
            peer = node.backend.trusted.add(
                TrustedPeer(hash_id=hash_id, name=name, transport=transport)
            ).to_dict()
        return web.json_response(peer)

    async def trusted_remove(request: web.Request) -> web.Response:
        hash_id = request.match_info.get("hash_id", "")
        ok = node.backend.trusted.remove(hash_id)
        return web.json_response({"removed": ok})

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
        if not node.backend.is_trusted(recipient):
            return web.json_response({"error": "peer not trusted"}, status=403)
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

    async def ping_peer(request: web.Request) -> web.Response:
        data = await request.json()
        hash_id = data.get("hash_id", "")
        if not hash_id:
            return web.json_response({"error": "hash_id required"}, status=400)
        await node.backend.ping_peer(hash_id)
        metrics = node.backend.get_peer_metrics(hash_id)
        return web.json_response(metrics)

    async def announce(_request: web.Request) -> web.Response:
        transport = _request.rel_url.query.get("transport")
        await node.backend.announce(transport)
        return web.json_response({"announced": True})

    async def upload_file(request: web.Request) -> web.Response:
        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != "file":
            return web.json_response({"error": "file field required"}, status=400)
        filename = field.filename or "upload.bin"
        dest = upload_dir / filename
        size = 0
        with dest.open("wb") as f:
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                f.write(chunk)
                size += len(chunk)
        return web.json_response({"path": str(dest), "filename": filename, "size": size})

    async def send_file(request: web.Request) -> web.Response:
        data = await request.json()
        recipient = data.get("recipient_hash", "")
        path_str = data.get("path", "")
        transport = data.get("transport", "tcp")
        if not recipient or not path_str:
            return web.json_response({"error": "recipient_hash and path required"}, status=400)
        if not node.backend.is_trusted(recipient):
            return web.json_response({"error": "peer not trusted"}, status=403)
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

    async def settings_get(_request: web.Request) -> web.Response:
        return web.json_response(node.settings.settings.to_dict())

    async def settings_put(request: web.Request) -> web.Response:
        data = await request.json()
        updated = await node.apply_settings(**data)
        return web.json_response(updated)

    async def browse_folders(request: web.Request) -> web.Response:
        path = request.rel_url.query.get("path")
        return web.json_response(list_directory(path))

    async def identity_regenerate(request: web.Request) -> web.Response:
        transport = request.match_info.get("transport", "tcp")
        if transport not in ("tcp", "serial"):
            return web.json_response({"error": "invalid transport"}, status=400)
        identity = node.backend.identity_store.regenerate(
            node.config.name, transport  # type: ignore[arg-type]
        )
        node.backend.identities[transport] = identity
        return web.json_response(
            {
                "name": identity.name,
                "hash_id": identity.hash_id,
                "transport": identity.transport,
                "public_key": identity.public_bytes().hex(),
            }
        )

    async def identity_delete(request: web.Request) -> web.Response:
        transport = request.match_info.get("transport", "tcp")
        if transport not in ("tcp", "serial"):
            return web.json_response({"error": "invalid transport"}, status=400)
        ok = node.backend.identity_store.delete(transport)  # type: ignore[arg-type]
        node.backend.identities.pop(transport, None)
        return web.json_response({"deleted": ok})

    async def identity_create(request: web.Request) -> web.Response:
        data = await request.json()
        transport = data.get("transport", "tcp")
        if transport not in ("tcp", "serial"):
            return web.json_response({"error": "invalid transport"}, status=400)
        identity = node.backend.identity_store.load_or_create(
            node.config.name, transport  # type: ignore[arg-type]
        )
        node.backend.identities[transport] = identity
        return web.json_response(
            {
                "name": identity.name,
                "hash_id": identity.hash_id,
                "transport": identity.transport,
                "public_key": identity.public_bytes().hex(),
            }
        )

    async def release_notes(_request: web.Request) -> web.Response:
        if RELEASE_NOTES_PATH.exists():
            text = RELEASE_NOTES_PATH.read_text(encoding="utf-8")
        else:
            text = f"# SRLTCP {__version__}\n\nNo release notes available."
        return web.json_response({"version": __version__, "notes": text})

    async def restart(_request: web.Request) -> web.Response:
        async def _do_restart() -> None:
            await asyncio.sleep(0.5)
            await node.stop()
            os.execv(sys.executable, [sys.executable, "-m", "srltcp", "web"])

        asyncio.create_task(_do_restart())
        return web.json_response({"restarting": True})

    async def config_get(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "name": node.config.name,
                "tcp_port": node.config.tcp_port,
                "relay_mode": node.config.relay_mode,
                "enable_tcp": node.config.enable_tcp,
                "enable_serial": node.config.enable_serial,
                "auto_announce": node.config.announce,
                "version": __version__,
            }
        )

    app.router.add_get("/api/status", status)
    app.router.add_get("/api/identities", identities)
    app.router.add_get("/api/peers", peers)
    app.router.add_get("/api/trusted", trusted_list)
    app.router.add_post("/api/trusted", trusted_add)
    app.router.add_delete("/api/trusted/{hash_id}", trusted_remove)
    app.router.add_get("/api/messages", messages)
    app.router.add_post("/api/messages", send_message)
    app.router.add_post("/api/connect", connect)
    app.router.add_post("/api/ping", ping_peer)
    app.router.add_post("/api/announce", announce)
    app.router.add_post("/api/upload", upload_file)
    app.router.add_post("/api/transfer", send_file)
    app.router.add_get("/api/transfers", transfers)
    app.router.add_post("/api/share/create", create_share)
    app.router.add_get("/api/config", config_get)
    app.router.add_get("/api/settings", settings_get)
    app.router.add_put("/api/settings", settings_put)
    app.router.add_get("/api/browse", browse_folders)
    app.router.add_post("/api/identities/{transport}", identity_create)
    app.router.add_post("/api/identities/{transport}/regenerate", identity_regenerate)
    app.router.add_delete("/api/identities/{transport}", identity_delete)
    app.router.add_get("/api/release-notes", release_notes)
    app.router.add_post("/api/restart", restart)