"""REST API routes."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from srltcp import __version__
from srltcp.core.messaging.groups import GroupError, is_valid_group_id
from srltcp.core.protocol.messages import is_valid_transfer_id
from srltcp.core.relay_proto import RelayCode, RelayCodeError
from srltcp.core.settings import AppSettings, SettingsStore
from srltcp.core.trusted import TrustedPeer, is_valid_hash_id
from srltcp.utils.files import uploads_dir
from srltcp.utils.folders import list_directory
from srltcp.utils.logging import get_logger
from srltcp.utils.network import list_interfaces
from srltcp.utils.serial_ports import baud_rates, list_serial_ports
from srltcp.utils.system_stats import list_timezones, system_stats

RELEASE_NOTES_PATH = Path(__file__).resolve().parents[1] / "RELEASE_NOTES.md"
log = get_logger(__name__)

if TYPE_CHECKING:
    from srltcp.core.node import SRLTCPNode


def _validate_path(path_str: str, *, must_exist: bool = False) -> Path | None:
    if not path_str or ".." in Path(path_str).parts:
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
    # Copies of files sent from the browser. They only matter while their
    # transfer can run: the backend removes the rest at startup.
    upload_dir = uploads_dir()
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
        try:
            peer = node.add_trusted_from_discovered(hash_id, transport)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        if peer:
            # Accepting a contact request / discovered peer: bring the session up
            # so anything they queued for us is delivered.
            if node.backend.get_link(hash_id):
                await node.backend.notify_contact_accepted(hash_id)
            else:
                node.backend._kick_connect(hash_id, peer.get("transport", "tcp"))
        if not peer:
            tcp_port_raw = data.get("tcp_port", 7825)
            try:
                tcp_port = int(tcp_port_raw)
            except (TypeError, ValueError):
                return web.json_response({"error": "invalid tcp_port"}, status=400)
            wan_port_raw = data.get("wan_port", 7825)
            try:
                wan_port = int(wan_port_raw)
            except (TypeError, ValueError):
                return web.json_response({"error": "invalid wan_port"}, status=400)
            connection_mode = data.get("connection_mode", "auto")
            if connection_mode not in ("auto", "lan", "wan"):
                connection_mode = "auto"
            try:
                peer = node.backend.trusted.add(
                    TrustedPeer(
                        hash_id=hash_id.lower(),
                        name=(data.get("name") or "peer").strip() or "peer",
                        transport=data.get("transport", "tcp"),
                        public_key=data.get("public_key", ""),
                        tcp_host=(data.get("tcp_host") or "").strip(),
                        tcp_port=tcp_port,
                        wan_host=(data.get("wan_host") or "").strip(),
                        wan_port=wan_port,
                        wan_enabled=bool(data.get("wan_enabled")),
                        connection_mode=connection_mode,
                    )
                ).public_dict()
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
                msg = str(exc)
                if "private" in msg:
                    msg = (
                        "That is a LAN/VPN address — put it in the Address field; "
                        "the internet address is for public hosts only"
                    )
                return web.json_response({"error": msg}, status=400)
        if tcp_host is not None:
            tcp_host = str(tcp_host).strip()
            if len(tcp_host) > 253 or any(c.isspace() for c in tcp_host):
                return web.json_response({"error": "invalid address"}, status=400)
        if tcp_port is not None:
            try:
                tcp_port = int(tcp_port)
                if not 1 <= tcp_port <= 65535:
                    raise ValueError
            except (TypeError, ValueError):
                return web.json_response({"error": "port must be 1-65535"}, status=400)
        if name is not None and not str(name).strip():
            return web.json_response({"error": "name cannot be empty"}, status=400)
        if wan_port is not None:
            from srltcp.utils.wan import validate_wan_port

            try:
                wan_port = validate_wan_port(int(wan_port))
            except (ValueError, TypeError) as exc:
                return web.json_response({"error": str(exc)}, status=400)
        peer = node.backend.trusted.update(
            hash_id,
            name=str(name)[:64] if name is not None else None,
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
        return web.json_response(peer.public_dict())

    async def trusted_clear_chat(request: web.Request) -> web.Response:
        hash_id = request.match_info.get("hash_id", "")
        if not node.backend.trusted.get(hash_id):
            return web.json_response({"error": "peer not found"}, status=404)
        removed = node.backend.clear_messages_for_peer(hash_id)
        return web.json_response({"cleared": removed})

    async def trusted_diagnose(request: web.Request) -> web.Response:
        hash_id = request.match_info.get("hash_id", "").lower()
        if not is_valid_hash_id(hash_id) or not node.backend.trusted.get(hash_id):
            return web.json_response({"error": "peer not found"}, status=404)
        probe = request.rel_url.query.get("probe_tor", "1") != "0"
        return web.json_response(await node.backend.diagnose_contact(hash_id, probe_tor=probe))

    async def trusted_remove(request: web.Request) -> web.Response:
        hash_id = request.match_info.get("hash_id", "")
        await node.backend.disconnect_peer(hash_id)
        node.backend.discovery.remove(hash_id)
        ok = node.backend.trusted.remove(hash_id)
        return web.json_response({"removed": ok, "hash_id": hash_id})

    async def messages(request: web.Request) -> web.Response:
        query = request.rel_url.query
        try:
            limit = max(1, min(int(query.get("limit", "200")), 2000))
        except ValueError:
            return web.json_response({"error": "invalid limit"}, status=400)
        peer = query.get("peer", "").lower()
        group = query.get("group", "").lower()
        if peer and not is_valid_hash_id(peer):
            return web.json_response({"error": "invalid peer"}, status=400)
        if group and not is_valid_group_id(group):
            return web.json_response({"error": "invalid group"}, status=400)
        return web.json_response(node.backend.get_messages(limit=limit, peer=peer, group=group))

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
        if transport not in ("tcp", "serial", "relay"):
            transport = "tcp"
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
        except (KeyError, RuntimeError, OSError, TimeoutError, ConnectionError) as exc:
            return web.json_response(
                {"connected": False, "error": str(exc), "handshake_complete": False},
                status=503,
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
        from srltcp.utils.serial_access import serial_group_status

        return web.json_response(
            {"ports": list_serial_ports(), "group": serial_group_status()}
        )

    async def serial_baud_rates(_request: web.Request) -> web.Response:
        return web.json_response({"rates": baud_rates()})

    async def ping_peer(request: web.Request) -> web.Response:
        data = await request.json()
        hash_id = data.get("hash_id", "")
        if not hash_id:
            return web.json_response({"error": "hash_id required"}, status=400)
        await node.backend.ping_peer(hash_id)
        return web.json_response(node.backend.get_peer_metrics(hash_id))

    async def announce(request: web.Request) -> web.Response:
        from srltcp.core.messaging.announce import ANNOUNCE_BURSTS, AnnounceError

        raw = (request.rel_url.query.get("transport") or "").strip().lower()
        target = (request.rel_url.query.get("target") or "").strip()
        if raw and raw not in ("tcp", "serial"):
            return web.json_response({"error": f"invalid transport: {raw}"}, status=400)
        if target:
            try:
                await node.backend.announce_to(target)
            except AnnounceError as exc:
                return web.json_response({"error": exc.reason}, status=400)
            return web.json_response({"announced": True, "target": target})
        try:
            announced = await node.backend.announce(raw or None)
        except AnnounceError as exc:
            return web.json_response(
                {"error": exc.reason, "transport": exc.transport},
                status=503,
            )
        return web.json_response(
            {
                "announced": True,
                "transports": announced,
                "bursts": ANNOUNCE_BURSTS,
            }
        )

    async def upload_file(request: web.Request) -> web.Response:
        try:
            reader = await request.multipart()
            field = await reader.next()
        except (ValueError, AssertionError, OSError) as exc:
            log.warning("Upload rejected: %s", exc)
            return web.json_response(
                {"error": "Upload failed: the file didn't arrive in one piece"}, status=400
            )
        if field is None or field.name != "file":
            return web.json_response({"error": "file field required"}, status=400)
        from srltcp.utils.files import safe_filename

        # Never "." / ".." or a hidden name: those would escape or hide in
        # the uploads folder.
        filename = safe_filename(field.filename or "upload.bin").lstrip(".") or "upload.bin"
        dest = upload_dir / filename
        if dest.exists():
            stem = dest.stem
            suffix = dest.suffix
            dest = upload_dir / f"{stem}_{int(time.time())}{suffix}"
        try:
            upload_dir.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as f:
                while True:
                    chunk = await field.read_chunk()
                    if not chunk:
                        break
                    f.write(chunk)
        except OSError as exc:
            dest.unlink(missing_ok=True)
            log.warning("Upload of %s failed: %s", filename, exc)
            reason = "device storage is full" if exc.errno == 28 else "could not save the file"
            return web.json_response({"error": f"Upload failed: {reason}"}, status=507)
        except Exception as exc:  # noqa: BLE001 - the page must learn why
            dest.unlink(missing_ok=True)
            log.warning("Upload of %s broke off: %s", filename, exc)
            return web.json_response(
                {"error": f"Upload failed: the file stopped arriving ({type(exc).__name__})"},
                status=400,
            )
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

    async def transfers(request: web.Request) -> web.Response:
        active_only = request.rel_url.query.get("all") not in ("1", "true", "yes")
        return web.json_response(node.backend.list_transfers(active_only=active_only))

    async def send_folder(request: web.Request) -> web.Response:
        data = await request.json()
        recipient = data.get("recipient_hash", "")
        path_str = data.get("path", "")
        transport = data.get("transport", "tcp")
        if not recipient or not path_str:
            return web.json_response(
                {"error": "recipient_hash and path required"}, status=400
            )
        if not node.backend.is_trusted(recipient):
            return web.json_response({"error": "peer not trusted"}, status=403)
        path = _validate_path(path_str, must_exist=True)
        if not path or not path.is_dir():
            return web.json_response({"error": "folder not found"}, status=404)
        try:
            result = await node.backend.send_folder(recipient, path, transport=transport)
        except Exception as exc:
            from srltcp.utils.files import FolderZipError

            if isinstance(exc, FolderZipError):
                return web.json_response({"error": str(exc)}, status=507)
            log.exception("Folder transfer failed for %s", path)
            return web.json_response({"error": "folder transfer failed"}, status=500)
        if result:
            return web.json_response(result)
        return web.json_response(
            {"error": "folder transfer failed — peer not connected"},
            status=503,
        )

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
        from srltcp.core.messaging.models import TransferState

        transfer_id = request.match_info.get("transfer_id", "")
        if not is_valid_transfer_id(transfer_id):
            return web.json_response({"error": "invalid transfer_id"}, status=400)
        transfer = node.backend._transfers.get(transfer_id)
        if not transfer:
            return web.json_response({"error": "transfer not found"}, status=404)
        path = node.backend.transfer_file_path(transfer_id)
        if path is None:
            return web.json_response({"error": "file not ready"}, status=404)
        from urllib.parse import quote

        fname = transfer.filename or path.name
        download = request.rel_url.query.get("download") in ("1", "true", "yes")
        if not download and transfer.state != TransferState.COMPLETE:
            return web.json_response(
                {"error": "transfer in progress — preview when complete"},
                status=409,
            )
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
        """Share a folder (or files uploaded via /api/share/files) with
        contacts and/or the members of a group who are your contacts."""
        from srltcp.core.messaging.groups import member_hashes

        data = await request.json()
        recipients = [str(h).lower() for h in data.get("recipients", []) if isinstance(h, str)]
        if data.get("recipient_hash"):
            recipients.append(str(data["recipient_hash"]).lower())
        group_id = str(data.get("group_id", "")).lower()
        if group_id:
            group = node.backend.groups.get(group_id)
            if not group:
                return web.json_response({"error": "group not found"}, status=404)
            recipients += member_hashes(group)
        me = node.backend._own_hash_ids()
        recipients = [
            h for h in dict.fromkeys(recipients)
            if is_valid_hash_id(h) and h not in me and node.backend.is_trusted(h)
        ]
        if not recipients:
            return web.json_response(
                {"error": "choose at least one trusted contact (or a group with contacts)"},
                status=400,
            )
        kind = "files" if data.get("kind") == "files" else "folder"
        path_str = str(data.get("path", ""))
        if kind == "files":
            folder = _validate_path(path_str, must_exist=True)
            base = node.backend.share_files_dir().resolve()
            if not folder or not folder.is_relative_to(base) or folder == base:
                return web.json_response({"error": "upload the files first"}, status=400)
        else:
            folder = (
                _validate_path(path_str, must_exist=True)
                if path_str
                else node.settings.resolved_shared_folder()
            )
        if not folder or not folder.is_dir():
            return web.json_response({"error": "folder not found"}, status=404)
        try:
            grants = await node.backend.offer_share(
                recipients,
                folder=folder,
                label=str(data.get("label", "") or folder.name)[:128],
                ttl_preset=str(data.get("ttl_preset", "1h")),
                download_limit_preset=str(data.get("download_limit_preset", "unlimited")),
                password=str(data.get("password", "")),
                kind=kind,
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        if not grants:
            return web.json_response({"error": "nothing shared"}, status=400)
        return web.json_response({"grants": grants, **grants[0]})

    async def share_files_upload(request: web.Request) -> web.Response:
        """Copy picked files into a new private share folder (multipart, field "file",
        repeatable). Returns the folder to pass to /api/share/peer/offer."""
        import secrets as _secrets

        from srltcp.utils.files import safe_filename

        base = node.backend.share_files_dir()
        folder = base / _secrets.token_hex(8)
        folder.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(base, 0o700)
        reader = await request.multipart()
        names: list[str] = []
        total = 0
        while True:
            field = await reader.next()
            if field is None:
                break
            if getattr(field, "name", "") != "file":
                continue
            name = safe_filename(getattr(field, "filename", None) or "file.bin")
            dest = folder / name
            n = 1
            while dest.exists():
                dest = folder / f"{Path(name).stem}_{n}{Path(name).suffix}"
                n += 1
            with dest.open("wb") as f:
                while True:
                    chunk = await field.read_chunk()
                    if not chunk:
                        break
                    f.write(chunk)
                    total += len(chunk)
            names.append(dest.name)
            if len(names) >= 500:
                break
        if not names:
            shutil.rmtree(folder, ignore_errors=True)
            return web.json_response({"error": "no files received"}, status=400)
        return web.json_response({"path": str(folder), "files": names, "size": total})

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
        password = data.get("password")
        entries = await node.backend.request_share_list(
            owner, grant_id, password=str(password) if password is not None else None
        )
        if entries is None:
            return web.json_response(
                {"error": "list request failed or timed out — is the contact connected?"},
                status=408,
            )
        return web.json_response(
            {
                "owner_hash": owner,
                "grant_id": grant_id,
                "entries": entries,
                "error": node.backend.share_list_error(grant_id),
            }
        )

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

    async def pairing_card(request: web.Request) -> web.Response:
        from srltcp.core.messaging.pairing import PairingError

        one_time = request.rel_url.query.get("one_time", "1") not in ("0", "false", "no")
        try:
            return web.json_response(node.backend.create_pairing_card(one_time=one_time))
        except PairingError as exc:
            return web.json_response({"error": str(exc)}, status=400)

    # ── Group chats ──

    def _group_id(request: web.Request) -> str:
        gid = request.match_info.get("group_id", "").lower()
        if not is_valid_group_id(gid):
            raise web.HTTPBadRequest(text='{"error": "invalid group id"}',
                                     content_type="application/json")
        return gid

    async def groups_list(_request: web.Request) -> web.Response:
        return web.json_response(node.backend.list_groups())

    async def groups_create(request: web.Request) -> web.Response:
        data = await request.json()
        members = data.get("members") or []
        if not isinstance(members, list) or not all(isinstance(h, str) for h in members):
            return web.json_response({"error": "members must be a list of contact ids"}, status=400)
        if not all(is_valid_hash_id(h) for h in members):
            return web.json_response({"error": "invalid contact id"}, status=400)
        try:
            group = await node.backend.create_group(str(data.get("name", "")), members)
        except GroupError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(group)

    async def groups_update(request: web.Request) -> web.Response:
        gid = _group_id(request)
        data = await request.json()
        lists = {}
        for key in ("add", "remove"):
            value = data.get(key) or []
            if not isinstance(value, list) or not all(
                isinstance(h, str) and is_valid_hash_id(h) for h in value
            ):
                return web.json_response({"error": f"invalid {key} list"}, status=400)
            lists[key] = value
        name = data.get("name")
        try:
            group = await node.backend.update_group(
                gid, name=str(name) if name is not None else None,
                add=lists["add"], remove=lists["remove"],
            )
        except GroupError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(group)

    async def groups_leave(request: web.Request) -> web.Response:
        gid = _group_id(request)
        try:
            await node.backend.leave_group(gid)
        except GroupError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(node.backend.get_group_view(gid) or {"ok": True})

    async def groups_delete(request: web.Request) -> web.Response:
        gid = _group_id(request)
        if not await node.backend.delete_group(gid):
            return web.json_response({"error": "group not found"}, status=404)
        return web.json_response({"ok": True})

    async def groups_send(request: web.Request) -> web.Response:
        gid = _group_id(request)
        data = await request.json()
        try:
            msg = await node.backend.send_group_message(gid, str(data.get("text", "")))
        except GroupError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(msg.to_dict())

    async def contact_request_decline(request: web.Request) -> web.Response:
        hash_id = request.match_info.get("hash_id", "").lower()
        if not is_valid_hash_id(hash_id):
            return web.json_response({"error": "invalid hash_id"}, status=400)
        node.backend.decline_contact_request(hash_id)
        return web.json_response({"ok": True})

    async def pairing_import(request: web.Request) -> web.Response:
        from srltcp.core.messaging.pairing import PairingError

        data = await request.json()
        code = str(data.get("code", "")).strip()
        if not code:
            return web.json_response({"error": "pairing code required"}, status=400)
        try:
            peer = await node.backend.import_pairing_card(
                code, in_person=bool(data.get("in_person"))  # scanned QR / NFC, not a link
            )
        except PairingError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(peer)

    async def pairing_shared(request: web.Request) -> web.Response:
        data = await request.json()
        node.backend.mark_pairing_shared(str(data.get("uri", ""))[:4096])
        return web.json_response({"ok": True})

    def _contact(request: web.Request) -> str:
        hash_id = request.match_info.get("hash_id", "").lower()
        if not is_valid_hash_id(hash_id) or not node.backend.trusted.get(hash_id):
            raise web.HTTPNotFound(
                text='{"error": "not a contact"}', content_type="application/json"
            )
        return hash_id

    async def trusted_safety(request: web.Request) -> web.Response:
        return web.json_response(node.backend.safety_info(_contact(request)))

    async def trusted_verify(request: web.Request) -> web.Response:
        from srltcp.core.messaging.pairing import PairingError

        hash_id = _contact(request)
        data = await request.json()
        if data.get("verified") is False:
            return web.json_response(node.backend.unverify_contact(hash_id))
        try:
            info = node.backend.verify_contact(
                hash_id, code=str(data.get("code", ""))[:512], confirm=data.get("confirm") is True
            )
        except PairingError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(info)

    async def call_signal(request: web.Request) -> web.Response:
        data = await request.json()
        recipient = str(data.get("recipient_hash", ""))
        signal = data.get("signal")
        if not is_valid_hash_id(recipient) or not isinstance(signal, dict):
            return web.json_response({"error": "recipient_hash and signal required"}, status=400)
        if not node.backend.is_trusted(recipient):
            return web.json_response({"error": "peer not trusted"}, status=403)
        link = node.backend.get_link(recipient)
        if not link or not link.handshake_complete:
            await node.backend.connect_to_peer(recipient)
            await node.backend.wait_for_handshake(recipient, timeout=8.0)
        ok = await node.backend.send_call_signal(recipient, signal)
        node.track_call_signal(recipient, signal)  # answered/declined here: stop ringing
        if not ok:
            return web.json_response({"error": "peer is not reachable"}, status=503)
        return web.json_response({"sent": True})

    async def call_ice(request: web.Request) -> web.Response:
        peer = request.rel_url.query.get("peer", "")
        if not is_valid_hash_id(peer) or not node.backend.is_trusted(peer):
            return web.json_response({"error": "unknown contact"}, status=400)
        cfg = await node.backend.call_ice_config(peer)
        if cfg is None:
            return web.json_response({"error": "call relay unavailable"}, status=503)
        return web.json_response(cfg)

    async def version_info(_request: web.Request) -> web.Response:
        from srltcp.utils.platform import is_android

        return web.json_response(
            {
                "version": __version__,
                "release_notes": "/api/release-notes",
                "platform": "android" if is_android() else "desktop",
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
        updated.tcp_port = max(1024, min(updated.tcp_port, 65535))
        updated.discovery_port = max(1024, min(updated.discovery_port, 65535))
        updated.relay_enabled = bool(updated.relay_enabled)
        updated.direct_internet = bool(updated.direct_internet)
        updated.tor_enabled = bool(updated.tor_enabled)
        updated.shrink_photos = bool(updated.shrink_photos)
        from srltcp.core.messaging.disappear import CHOICES

        if updated.disappear_seconds not in CHOICES:
            updated.disappear_seconds = 0
        tor_path = str(updated.tor_path or "").strip()
        if tor_path and not (Path(tor_path).is_file() and Path(tor_path).name.lower() in (
            "tor", "tor.exe"
        )):
            return web.json_response(
                {"error": "Tor path must point to the tor (tor.exe) program"}, status=400
            )
        updated.tor_path = tor_path
        relay_text = str(updated.relay_code or "").strip()
        if relay_text:
            try:
                relay_text = RelayCode.parse(relay_text).encode()
            except RelayCodeError as exc:
                return web.json_response({"error": f"Internet relay: {exc}"}, status=400)
        updated.relay_code = relay_text
        if updated.clock_source not in ("system", "ntp"):
            updated.clock_source = "system"
        updated.handshake_protocol = "identity"  # the only supported handshake
        stun = str(updated.call_stun_server or "").strip()
        if stun and (
            not stun.startswith(("stun:", "stuns:", "turn:", "turns:"))
            or len(stun) > 255
            or " " in stun
        ):
            return web.json_response(
                {"error": "Call server must look like stun:host:3478 or turn:host:3478"},
                status=400,
            )
        updated.call_stun_server = stun
        updated.call_turn_username = str(updated.call_turn_username or "").strip()[:128]
        updated.call_turn_password = str(updated.call_turn_password or "")[:256]
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
        payload = updated.to_dict()
        payload["transports"] = node.backend.get_transport_status()
        return web.json_response(payload)

    async def browse_folders(request: web.Request) -> web.Response:
        path = request.rel_url.query.get("path")
        dirs_only = request.rel_url.query.get("dirs_only") in ("1", "true", "yes")
        return web.json_response(list_directory(path, dirs_only=dirs_only))

    async def delete_folder(request: web.Request) -> web.Response:
        from srltcp.utils.folders import delete_directory

        data = await request.json()
        path_str = str(data.get("path", "")).strip()
        if not path_str:
            return web.json_response({"error": "path required"}, status=400)
        try:
            removed = delete_directory(path_str)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except OSError as exc:
            return web.json_response({"error": f"delete failed: {exc}"}, status=500)
        return web.json_response({"deleted": str(removed)})

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
        from srltcp.utils.platform import is_android

        if is_android():
            # execv would kill the Android app process; settings that need a
            # restart are applied the next time the app is started.
            return web.json_response(
                {"restarting": False, "error": "Close and reopen the app to apply"},
                status=409,
            )

        async def _do_restart() -> None:
            await asyncio.sleep(0.5)
            await node.stop()
            os.execv(sys.executable, [sys.executable, "-m", "srltcp", *sys.argv[1:]])

        asyncio.create_task(_do_restart())
        return web.json_response({"restarting": True})

    # ── Folder sync ──

    def _sync_default_root(name: str) -> str:
        from srltcp.utils.files import safe_filename

        base = node.settings.resolved_incoming_dir().parent / "Sync"
        return str(base / (safe_filename(name).lstrip(".") or "Folder"))

    def _sync_views(peer: str = "") -> list[dict]:
        views = node.backend.list_syncs(peer)
        for view in views:
            if view["state"] == "offered":
                view["suggested_root"] = _sync_default_root(view["name"])
        return views

    def _sync_id(request: web.Request) -> str:
        sid = request.match_info.get("sync_id", "")
        if sid not in node.backend._syncs:
            raise web.HTTPNotFound(
                text='{"error": "no such synced folder"}', content_type="application/json"
            )
        return sid

    async def sync_list(request: web.Request) -> web.Response:
        peer = request.rel_url.query.get("peer", "").lower()
        return web.json_response(_sync_views(peer if is_valid_hash_id(peer) else ""))

    async def sync_create(request: web.Request) -> web.Response:
        data = await request.json()
        peer = str(data.get("recipient_hash", "")).lower()
        path = _validate_path(str(data.get("path", "")), must_exist=True)
        if not is_valid_hash_id(peer) or not node.backend.is_trusted(peer):
            return web.json_response({"error": "not a contact"}, status=400)
        if not path or not path.is_dir():
            return web.json_response({"error": "choose an existing folder"}, status=400)
        try:
            view = await node.backend.create_sync(peer, path, str(data.get("name", ""))[:64])
        except (ValueError, OSError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(view)

    async def sync_accept(request: web.Request) -> web.Response:
        sid = _sync_id(request)
        data = await request.json()
        raw = str(data.get("path", "")) or _sync_default_root(node.backend._syncs[sid].name)
        path = _validate_path(raw, must_exist=False)
        if not path or (path.exists() and not path.is_dir()):
            return web.json_response({"error": "choose a folder"}, status=400)
        try:
            view = await node.backend.accept_sync(sid, path)
        except (ValueError, OSError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(view)

    async def sync_decline(request: web.Request) -> web.Response:
        await node.backend.decline_sync(_sync_id(request))
        return web.json_response({"ok": True})

    async def sync_pause(request: web.Request) -> web.Response:
        sid = _sync_id(request)
        data = await request.json()
        try:
            view = await node.backend.set_sync_paused(sid, bool(data.get("paused")))
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(view)

    async def sync_now(request: web.Request) -> web.Response:
        sid = _sync_id(request)
        await node.backend.sync_now(sid)
        return web.json_response(node.backend._sync_view(sid) or {})

    async def sync_remove(request: web.Request) -> web.Response:
        await node.backend.remove_sync(_sync_id(request))
        return web.json_response({"ok": True})

    # ── Encrypted backup / restore ──

    def _backup_dir() -> Path:
        return node.backend.trusted.path.parent / "backups"

    async def backup_create(request: web.Request) -> web.Response:
        from srltcp.core.backup import BackupError, create_backup

        data = await request.json()
        password = str(data.get("password", ""))
        root = node.backend.trusted.path.parent
        node.backend.save_message_history()  # the backup has the latest chats
        try:
            blob = await asyncio.to_thread(
                create_backup, root, password, app_version=__version__
            )
        except BackupError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        folder = _backup_dir()
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob("srltcp-backup-*.srltcp"):
            old.unlink(missing_ok=True)  # only the newest one is kept here
        name = f"srltcp-backup-{time.strftime('%Y-%m-%d-%H%M')}.srltcp"
        fd = os.open(folder / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        return web.json_response({"filename": name, "size": len(blob)})

    async def backup_file(request: web.Request) -> web.Response:
        name = request.match_info.get("name", "")
        path = _backup_dir() / name
        from srltcp.core.backup import valid_backup_name

        if not valid_backup_name(name) or not path.is_file():
            return web.json_response({"error": "backup not found"}, status=404)
        return web.FileResponse(path, headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Content-Type": "application/octet-stream",
        })

    async def backup_restore(request: web.Request) -> web.Response:
        from srltcp.core.backup import (
            MAX_BACKUP,
            BackupError,
            open_backup,
            stage_restore,
            summary,
        )
        from srltcp.utils.platform import is_android

        blob, password = b"", ""
        try:
            reader = await request.multipart()
            while (field := await reader.next()) is not None:
                if field.name == "file":
                    while chunk := await field.read_chunk():
                        blob += chunk
                        if len(blob) > MAX_BACKUP:
                            raise BackupError("that file is too big to be a backup")
                elif field.name == "password":
                    password = await field.text()
                    if len(password) > 1024:
                        raise BackupError("that password is too long")
        except BackupError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except (ValueError, AssertionError, OSError):
            return web.json_response({"error": "the backup file didn't arrive"}, status=400)
        try:
            data = await asyncio.to_thread(open_backup, blob, password)
        except BackupError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        info = summary(data)
        # Staged: applied at the next start, before anything is loaded, so
        # nothing this process still writes can overwrite the restore.
        await asyncio.to_thread(stage_restore, node.backend.trusted.path.parent, data)
        await node.stop()
        log.info("Restored a backup of %s (%d contacts)", info["name"], info["contacts"])
        if is_android():
            return web.json_response({"restored": True, "restart": "app", **info})

        async def _restart() -> None:
            await asyncio.sleep(0.5)
            os.execv(sys.executable, [sys.executable, "-m", "srltcp", *sys.argv[1:]])

        asyncio.create_task(_restart())
        return web.json_response({"restored": True, "restart": "self", **info})

    async def interfaces(_request: web.Request) -> web.Response:
        return web.json_response({"interfaces": list_interfaces()})

    async def cancel_transfer(request: web.Request) -> web.Response:
        transfer_id = request.match_info.get("transfer_id", "")
        if not is_valid_transfer_id(transfer_id):
            return web.json_response({"error": "invalid transfer_id"}, status=400)
        transfer = node.backend._transfers.get(transfer_id)
        if not transfer:
            return web.json_response({"error": "transfer not found"}, status=404)
        ok = await node.backend.cancel_transfer(transfer_id)
        if not ok:
            return web.json_response({"error": "transfer already finished"}, status=409)
        return web.json_response({"cancelled": True, "id": transfer_id})

    async def system(_request: web.Request) -> web.Response:
        # Reads /proc, /sys or WMI (and maybe NTP): never on the event loop.
        stats = await asyncio.to_thread(
            system_stats,
            timezone=node.settings.timezone,
            clock_source=node.settings.clock_source,
            ntp_server=node.settings.ntp_server,
        )
        return web.json_response(stats)

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
    app.router.add_post("/api/trusted/{hash_id}/diagnose", trusted_diagnose)
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
    app.router.add_post("/api/transfer-folder", send_folder)
    app.router.add_get("/api/transfers", transfers)
    app.router.add_get("/api/transfers/{transfer_id}/file", transfer_file)
    app.router.add_post("/api/transfers/{transfer_id}/cancel", cancel_transfer)
    app.router.add_get("/api/share/grants", share_grants)
    app.router.add_post("/api/share/peer/offer", share_peer_offer)
    app.router.add_post("/api/share/files", share_files_upload)
    app.router.add_post("/api/share/peer/list", share_peer_list)
    app.router.add_post("/api/share/peer/fetch", share_peer_fetch)
    app.router.add_post("/api/share/peer/revoke", share_peer_revoke)
    app.router.add_get("/api/version", version_info)
    app.router.add_get("/api/pairing/card", pairing_card)
    app.router.add_post("/api/pairing/import", pairing_import)
    app.router.add_post("/api/pairing/shared", pairing_shared)
    app.router.add_get("/api/trusted/{hash_id}/safety", trusted_safety)
    app.router.add_post("/api/trusted/{hash_id}/verify", trusted_verify)
    app.router.add_post("/api/contact-requests/{hash_id}/decline", contact_request_decline)
    app.router.add_get("/api/groups", groups_list)
    app.router.add_post("/api/groups", groups_create)
    app.router.add_post("/api/groups/{group_id}", groups_update)
    app.router.add_post("/api/groups/{group_id}/leave", groups_leave)
    app.router.add_delete("/api/groups/{group_id}", groups_delete)
    app.router.add_post("/api/groups/{group_id}/messages", groups_send)
    app.router.add_post("/api/call/signal", call_signal)
    app.router.add_get("/api/call/ice", call_ice)
    app.router.add_post("/api/share/create", create_share)
    app.router.add_get("/api/settings", settings_get)
    app.router.add_post("/api/settings", settings_post)
    app.router.add_get("/api/browse", browse_folders)
    app.router.add_post("/api/folders/delete", delete_folder)
    app.router.add_post("/api/identities/{transport}/regenerate", identity_regenerate)
    app.router.add_delete("/api/identities/{transport}", identity_delete)
    app.router.add_get("/api/release-notes", release_notes)
    app.router.add_post("/api/restart", restart)
    app.router.add_get("/api/sync", sync_list)
    app.router.add_post("/api/sync", sync_create)
    app.router.add_post("/api/sync/{sync_id}/accept", sync_accept)
    app.router.add_post("/api/sync/{sync_id}/decline", sync_decline)
    app.router.add_post("/api/sync/{sync_id}/pause", sync_pause)
    app.router.add_post("/api/sync/{sync_id}/now", sync_now)
    app.router.add_delete("/api/sync/{sync_id}", sync_remove)
    app.router.add_post("/api/backup", backup_create)
    app.router.add_get("/api/backup/{name}", backup_file)
    app.router.add_post("/api/backup/restore", backup_restore)
    app.router.add_get("/api/interfaces", interfaces)
    app.router.add_get("/api/system", system)
    app.router.add_get("/api/timezones", timezones)
    app.router.add_get("/api/network", network_view)