"""REST API routes."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from srltcp import __version__
from srltcp.core.messaging.share_peer import SHARE_DOWNLOAD_LIMITS, SHARE_TTL_SECONDS
from srltcp.core.settings import AppSettings, SettingsStore
from srltcp.core.trusted import TrustedPeer, is_valid_hash_id
from srltcp.utils.folders import list_directory
from srltcp.utils.network import list_interfaces
from srltcp.utils.platform import data_dir
from srltcp.utils.serial_ports import baud_rates, list_serial_ports
from srltcp.utils.system_stats import list_timezones, system_stats

RELEASE_NOTES_PATH = Path(__file__).resolve().parents[1] / "RELEASE_NOTES.md"

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
        if not is_valid_hash_id(hash_id):
            return web.json_response({"error": "invalid hash_id"}, status=400)
        transport = data.get("transport")
        peer = node.add_trusted_from_discovered(hash_id, transport)
        if not peer:
            try:
                peer = node.backend.trusted.add(
                    TrustedPeer(
                        hash_id=hash_id,
                        name=data.get("name", "peer"),
                        transport=data.get("transport", "tcp"),
                    )
                ).to_dict()
            except ValueError as exc:
                return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(peer)

    async def trusted_update(request: web.Request) -> web.Response:
        hash_id = request.match_info.get("hash_id", "")
        data = await request.json()
        name = data.get("name")
        blocked = data.get("blocked")
        wan_host = data.get("wan_host")
        wan_port = data.get("wan_port")
        wan_enabled = data.get("wan_enabled")
        connection_mode = data.get("connection_mode")
        tcp_host = data.get("tcp_host")
        tcp_port = data.get("tcp_port")
        if all(
            v is None
            for v in (
                name,
                blocked,
                wan_host,
                wan_port,
                wan_enabled,
                connection_mode,
                tcp_host,
                tcp_port,
            )
        ):
            return web.json_response({"error": "no fields to update"}, status=400)
        if wan_host:
            from srltcp.utils.wan import validate_wan_host

            try:
                validate_wan_host(str(wan_host))
            except ValueError as exc:
                return web.json_response({"error": str(exc)}, status=400)
        if wan_port is not None:
            from srltcp.utils.wan import validate_wan_port

            try:
                wan_port = validate_wan_port(int(wan_port))
            except (ValueError, TypeError) as exc:
                return web.json_response({"error": str(exc)}, status=400)
        peer = node.backend.trusted.update(
            hash_id,
            name=name if name is not None else None,
            blocked=blocked,
            tcp_host=tcp_host,
            tcp_port=int(tcp_port) if tcp_port is not None else None,
            wan_host=str(wan_host).strip() if wan_host is not None else None,
            wan_port=wan_port,
            wan_enabled=wan_enabled,
            connection_mode=connection_mode,
        )
        if not peer:
            return web.json_response({"error": "peer not found"}, status=404)
        if blocked is True:
            await node.backend.disconnect_peer(hash_id)
        return web.json_response(peer.to_dict())

    async def trusted_clear_chat(request: web.Request) -> web.Response:
        hash_id = request.match_info.get("hash_id", "")
        if not node.backend.trusted.get(hash_id):
            return web.json_response({"error": "peer not found"}, status=404)
        removed = node.backend.clear_messages_for_peer(hash_id)
        return web.json_response({"cleared": removed})

    async def trusted_remove(request: web.Request) -> web.Response:
        hash_id = request.match_info.get("hash_id", "")
        await node.backend.disconnect_peer(hash_id)
        node.backend.discovery.remove(hash_id)
        ok = node.backend.trusted.remove(hash_id)
        return web.json_response({"removed": ok, "hash_id": hash_id})

    async def messages(request: web.Request) -> web.Response:
        limit = min(int(request.rel_url.query.get("limit", "200")), 1000)
        return web.json_response(node.backend.get_messages(limit=limit))

    async def delete_message(request: web.Request) -> web.Response:
        message_id = request.match_info.get("message_id", "")
        if not message_id:
            return web.json_response({"error": "message_id required"}, status=400)
        ok = node.backend.delete_message(message_id)
        return web.json_response({"deleted": ok})

    async def send_message(request: web.Request) -> web.Response:
        data = await request.json()
        recipient = data.get("recipient_hash", "")
        text = str(data.get("text", ""))[:65536]
        transport = data.get("transport", "tcp")
        if not recipient or not text.strip():
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
        force = bool(data.get("force", False))
        try:
            ok = await node.backend.connect_to_peer(
                hash_id, host=host, port=port, transport=transport, force=force
            )
            if ok:
                await node.backend.wait_for_handshake(hash_id, timeout=15.0)
        except (KeyError, RuntimeError, OSError) as exc:
            return web.json_response(
                {"connected": False, "error": str(exc), "handshake_complete": False},
                status=500,
            )
        link = node.backend.get_link(hash_id)
        return web.json_response(
            {
                "connected": ok,
                "handshake_complete": bool(link and link.handshake_complete),
                "rtt_ms": link.rtt_ms if link else None,
            }
        )

    async def disconnect(request: web.Request) -> web.Response:
        data = await request.json()
        hash_id = data.get("hash_id", "")
        if not hash_id:
            return web.json_response({"error": "hash_id required"}, status=400)
        ok = await node.backend.disconnect_peer(hash_id)
        return web.json_response({"disconnected": ok})

    async def serial_ports(_request: web.Request) -> web.Response:
        return web.json_response({"ports": list_serial_ports()})

    async def serial_baud_rates(_request: web.Request) -> web.Response:
        return web.json_response({"rates": baud_rates()})

    async def ping_peer(request: web.Request) -> web.Response:
        data = await request.json()
        hash_id = data.get("hash_id", "")
        if not hash_id:
            return web.json_response({"error": "hash_id required"}, status=400)
        await node.backend.ping_peer(hash_id)
        return web.json_response(node.backend.get_peer_metrics(hash_id))

    async def announce(_request: web.Request) -> web.Response:
        transport = _request.rel_url.query.get("transport")
        await node.backend.announce(transport)
        return web.json_response({"announced": True})

    async def upload_file(request: web.Request) -> web.Response:
        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != "file":
            return web.json_response({"error": "file field required"}, status=400)
        from srltcp.utils.files import safe_filename

        filename = safe_filename(field.filename or "upload.bin")
        dest = upload_dir / filename
        if dest.exists():
            stem = dest.stem
            suffix = dest.suffix
            dest = upload_dir / f"{stem}_{int(time.time())}{suffix}"
        with dest.open("wb") as f:
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                f.write(chunk)
        return web.json_response(
            {"path": str(dest), "filename": filename, "size": dest.stat().st_size}
        )

    async def send_file(request: web.Request) -> web.Response:
        data = await request.json()
        recipient = data.get("recipient_hash", "")
        path_str = data.get("path", "")
        transport = data.get("transport", "tcp")
        if not recipient or not path_str:
            return web.json_response({"error": "recipient_hash and path required"}, status=400)
        if not node.backend.is_trusted(recipient):
            return web.json_response({"error": "peer not trusted"}, status=403)
        path = _validate_path(path_str, must_exist=True)
        if not path or not path.is_file():
            return web.json_response({"error": "file not found"}, status=404)
        result = await node.backend.send_file(recipient, path, transport=transport)
        if result:
            return web.json_response(result)
        return web.json_response({"error": "transfer failed"}, status=500)

    async def transfers(_request: web.Request) -> web.Response:
        return web.json_response(node.backend.list_transfers())

    def _mime_for_filename(name: str) -> str:
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        return {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "gif": "image/gif",
            "webp": "image/webp",
            "bmp": "image/bmp",
            "svg": "image/svg+xml",
            "mp4": "video/mp4",
            "webm": "video/webm",
            "mov": "video/quicktime",
            "mkv": "video/x-matroska",
            "avi": "video/x-msvideo",
            "m4v": "video/mp4",
            "ogv": "video/ogg",
        }.get(ext, "application/octet-stream")

    async def transfer_file(request: web.Request) -> web.Response:
        transfer_id = request.match_info.get("transfer_id", "")
        transfer = node.backend._transfers.get(transfer_id)
        if not transfer:
            return web.json_response({"error": "transfer not found"}, status=404)
        path = transfer.path
        if not path.is_file():
            incoming = node.backend._incoming_paths.get(transfer_id)
            if incoming and incoming.is_file():
                path = incoming
            elif path.exists() and path.stat().st_size > 0:
                pass
            else:
                return web.json_response({"error": "file not ready"}, status=404)
        from urllib.parse import quote

        fname = transfer.filename or path.name
        download = request.rel_url.query.get("download") in ("1", "true", "yes")
        disp_type = "attachment" if download else "inline"
        disposition = (
            f'{disp_type}; filename="{quote(fname)}"; filename*=UTF-8\'\'{quote(fname)}'
        )
        return web.FileResponse(
            path,
            headers={
                "Accept-Ranges": "bytes",
                "Cache-Control": "no-store",
                "Content-Disposition": disposition,
                "Content-Type": _mime_for_filename(fname),
            },
        )

    async def share_grants(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "local": node.backend.list_local_share_grants(),
                "remote": node.backend.list_remote_share_grants(),
            }
        )

    async def share_peer_offer(request: web.Request) -> web.Response:
        data = await request.json()
        recipient = data.get("recipient_hash", "")
        if not recipient:
            return web.json_response({"error": "recipient_hash required"}, status=400)
        if not node.backend.is_trusted(recipient):
            return web.json_response({"error": "peer not trusted"}, status=403)
        path_str = data.get("path", "")
        folder = (
            _validate_path(path_str, must_exist=True)
            if path_str
            else node.settings.resolved_shared_folder()
        )
        if not folder or not folder.is_dir():
            return web.json_response({"error": "shared folder not found"}, status=404)
        ttl_preset = data.get("ttl_preset", "1h")
        download_limit_preset = data.get("download_limit_preset", "unlimited")
        if ttl_preset not in SHARE_TTL_SECONDS:
            return web.json_response({"error": "invalid ttl_preset"}, status=400)
        if download_limit_preset not in SHARE_DOWNLOAD_LIMITS:
            return web.json_response({"error": "invalid download_limit_preset"}, status=400)
        try:
            result = await node.backend.offer_share_folder(
                recipient,
                folder=folder,
                label=data.get("label", folder.name),
                ttl_preset=ttl_preset,
                download_limit_preset=download_limit_preset,
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        if not result:
            return web.json_response({"error": "share offer failed — connect first"}, status=500)
        return web.json_response(result)

    async def share_peer_revoke(request: web.Request) -> web.Response:
        data = await request.json()
        grant_id = data.get("grant_id", "")
        if not grant_id:
            return web.json_response({"error": "grant_id required"}, status=400)
        grant = node.backend._share_grants.get(grant_id)
        if not grant:
            return web.json_response({"error": "grant not found"}, status=404)
        recipient = grant.recipient_hash
        ok = await node.backend.notify_share_revoked(grant_id, recipient)
        if not ok:
            return web.json_response({"error": "revoke failed"}, status=400)
        return web.json_response({"revoked": True, "grant_id": grant_id})

    async def share_peer_list(request: web.Request) -> web.Response:
        data = await request.json()
        owner = data.get("owner_hash", "")
        grant_id = data.get("grant_id", "")
        if not owner or not grant_id:
            return web.json_response({"error": "owner_hash and grant_id required"}, status=400)
        ok = await node.backend.request_share_list(owner, grant_id)
        if not ok:
            return web.json_response({"error": "list request failed"}, status=400)
        return web.json_response({"requested": True})

    async def share_peer_fetch(request: web.Request) -> web.Response:
        data = await request.json()
        owner = data.get("owner_hash", "")
        grant_id = data.get("grant_id", "")
        rel_path = data.get("path", "")
        if not owner or not grant_id or not rel_path:
            return web.json_response(
                {"error": "owner_hash, grant_id, and path required"}, status=400
            )
        as_folder = data.get("as_folder", False) in (True, "true", "1", 1)
        ok = await node.backend.request_share_file(
            owner, grant_id, rel_path, as_folder=as_folder
        )
        if not ok:
            return web.json_response({"error": "fetch request failed"}, status=400)
        return web.json_response({"requested": True})

    async def version_info(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "version": __version__,
                "release_notes": "/api/release-notes",
            }
        )

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
        updated.apply_retention_preset()
        preset = updated.message_retention_preset
        if preset not in ("forever", "restart"):
            updated.message_retention_hours = max(1, min(updated.message_retention_hours, 8760))
        updated.web_port = max(1024, min(updated.web_port, 65535))
        if updated.clock_source not in ("system", "ntp"):
            updated.clock_source = "system"
        if updated.ntp_server:
            updated.ntp_server = str(updated.ntp_server).strip()[:253]

        for field_name in ("incoming_files_dir", "shared_folder"):
            val = getattr(updated, field_name)
            if val:
                p = _validate_path(val)
                if not p:
                    return web.json_response({"error": f"invalid {field_name}"}, status=400)

        updated.setup_complete = True
        updated.version = __version__
        store.save(updated)
        await node.apply_settings(updated)
        return web.json_response(updated.to_dict())

    async def browse_folders(request: web.Request) -> web.Response:
        path = request.rel_url.query.get("path")
        return web.json_response(list_directory(path))

    async def identity_regenerate(request: web.Request) -> web.Response:
        transport = request.match_info.get("transport", "tcp")
        if transport not in ("tcp", "serial"):
            return web.json_response({"error": "invalid transport"}, status=400)
        identity = node.backend.identity_store.regenerate(
            node.config.name, transport
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
        ok = node.backend.identity_store.delete(transport)
        node.backend.identities.pop(transport, None)
        return web.json_response({"deleted": ok})

    async def release_notes(_request: web.Request) -> web.Response:
        if RELEASE_NOTES_PATH.exists():
            text = RELEASE_NOTES_PATH.read_text(encoding="utf-8")
        else:
            text = f"# SRLTCP {__version__}\n\nNo release notes."
        return web.json_response({"version": __version__, "notes": text})

    async def restart(_request: web.Request) -> web.Response:
        async def _do_restart() -> None:
            await asyncio.sleep(0.5)
            await node.stop()
            os.execv(sys.executable, [sys.executable, "-m", "srltcp", "web"])

        asyncio.create_task(_do_restart())
        return web.json_response({"restarting": True})

    async def interfaces(_request: web.Request) -> web.Response:
        return web.json_response({"interfaces": list_interfaces()})

    async def cancel_transfer(request: web.Request) -> web.Response:
        transfer_id = request.match_info.get("transfer_id", "")
        ok = await node.backend.cancel_transfer(transfer_id)
        if not ok:
            return web.json_response({"error": "transfer not found"}, status=404)
        return web.json_response({"cancelled": True, "id": transfer_id})

    async def system(_request: web.Request) -> web.Response:
        return web.json_response(
            system_stats(
                timezone=node.settings.timezone,
                clock_source=node.settings.clock_source,
                ntp_server=node.settings.ntp_server,
            )
        )

    async def timezones(_request: web.Request) -> web.Response:
        return web.json_response({"timezones": list_timezones()})

    async def network_view(_request: web.Request) -> web.Response:
        identities = node.backend.get_identities()
        discovered = node.backend.get_discovered_peers()
        trusted = node.backend.get_trusted_peers()
        links = node.backend.list_links()
        nodes = []
        edges = []
        for transport, ident in identities.items():
            nodes.append(
                {
                    "id": ident["hash_id"],
                    "label": ident["name"],
                    "role": "self",
                    "transport": transport,
                }
            )
        trusted_ids = {p["hash_id"] for p in trusted}
        seen: set[str] = set()
        node_ids: set[str] = {ident["hash_id"] for ident in identities.values()}

        for peer in discovered + trusted:
            pid = peer["hash_id"]
            if pid in seen:
                continue
            seen.add(pid)
            node_ids.add(pid)
            nodes.append(
                {
                    "id": pid,
                    "label": peer.get("name", pid[:8]),
                    "role": "trusted" if pid in trusted_ids else "discovered",
                    "transport": peer.get("transport", "tcp"),
                    "address": peer.get("tcp_host") or peer.get("address", ""),
                }
            )

        linked_hashes = {
            link["hash_id"] for link in links if link.get("handshake_complete")
        }
        for link in links:
            pid = link.get("hash_id", "")
            if pid and pid not in seen:
                seen.add(pid)
                node_ids.add(pid)
                nodes.append(
                    {
                        "id": pid,
                        "label": link.get("peer_name") or pid[:8],
                        "role": "linked",
                        "transport": link.get("transport", "tcp"),
                        "address": link.get("address", ""),
                    }
                )

        for ident in identities.values():
            local_hash = ident["hash_id"]
            for peer in discovered:
                pid = peer["hash_id"]
                if pid in linked_hashes:
                    continue
                edges.append(
                    {
                        "from": local_hash,
                        "to": pid,
                        "transport": peer.get("transport", "tcp"),
                        "state": "discovered",
                    }
                )

        for link in links:
            if not link.get("handshake_complete"):
                continue
            transport = link.get("transport", "tcp")
            local_hash = identities.get(transport, {}).get("hash_id", "")
            if not local_hash and identities:
                local_hash = next(iter(identities.values()))["hash_id"]
            remote = link.get("hash_id", "")
            if not local_hash or not remote:
                continue
            edges.append(
                {
                    "from": local_hash,
                    "to": remote,
                    "transport": transport,
                    "state": "up",
                }
            )
        return web.json_response(
            {
                "nodes": nodes,
                "edges": edges,
                "identities": identities,
                "discovered": discovered,
                "trusted": trusted,
                "links": links,
            }
        )

    app.router.add_get("/api/status", status)
    app.router.add_get("/api/identities", identities)
    app.router.add_get("/api/peers", peers)
    app.router.add_get("/api/trusted", trusted_list)
    app.router.add_post("/api/trusted", trusted_add)
    app.router.add_patch("/api/trusted/{hash_id}", trusted_update)
    app.router.add_post("/api/trusted/{hash_id}/clear-chat", trusted_clear_chat)
    app.router.add_delete("/api/trusted/{hash_id}", trusted_remove)
    app.router.add_get("/api/messages", messages)
    app.router.add_delete("/api/messages/{message_id}", delete_message)
    app.router.add_post("/api/messages", send_message)
    app.router.add_post("/api/connect", connect)
    app.router.add_post("/api/disconnect", disconnect)
    app.router.add_get("/api/serial/ports", serial_ports)
    app.router.add_get("/api/serial/baud-rates", serial_baud_rates)
    app.router.add_post("/api/ping", ping_peer)
    app.router.add_post("/api/announce", announce)
    app.router.add_post("/api/upload", upload_file)
    app.router.add_post("/api/transfer", send_file)
    app.router.add_get("/api/transfers", transfers)
    app.router.add_get("/api/transfers/{transfer_id}/file", transfer_file)
    app.router.add_post("/api/transfers/{transfer_id}/cancel", cancel_transfer)
    app.router.add_get("/api/share/grants", share_grants)
    app.router.add_post("/api/share/peer/offer", share_peer_offer)
    app.router.add_post("/api/share/peer/list", share_peer_list)
    app.router.add_post("/api/share/peer/fetch", share_peer_fetch)
    app.router.add_post("/api/share/peer/revoke", share_peer_revoke)
    app.router.add_get("/api/version", version_info)
    app.router.add_post("/api/share/create", create_share)
    app.router.add_get("/api/settings", settings_get)
    app.router.add_post("/api/settings", settings_post)
    app.router.add_get("/api/browse", browse_folders)
    app.router.add_post("/api/identities/{transport}/regenerate", identity_regenerate)
    app.router.add_delete("/api/identities/{transport}", identity_delete)
    app.router.add_get("/api/release-notes", release_notes)
    app.router.add_post("/api/restart", restart)
    app.router.add_get("/api/interfaces", interfaces)
    app.router.add_get("/api/system", system)
    app.router.add_get("/api/timezones", timezones)
    app.router.add_get("/api/network", network_view)