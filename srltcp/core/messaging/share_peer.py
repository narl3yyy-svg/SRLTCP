"""End-to-end encrypted peer folder sharing over established links."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from srltcp.core.protocol.messages import MessageType, encode_payload
from srltcp.utils.files import walk_directory
from srltcp.utils.logging import get_logger

if TYPE_CHECKING:
    from srltcp.core.messaging.backend import MessagingBackend

log = get_logger(__name__)

MAX_SHARE_ENTRIES = 500
FOREVER_EXPIRES = time.time() + 100 * 365 * 86400

SHARE_TTL_SECONDS: dict[str, float] = {
    "1m": 60,
    "5m": 300,
    "1h": 3600,
    "1d": 86400,
    "1w": 604800,
    "forever": FOREVER_EXPIRES - time.time(),
}

SHARE_DOWNLOAD_LIMITS: dict[str, int] = {
    "1": 1,
    "2": 2,
    "5": 5,
    "10": 10,
    "25": 25,
    "unlimited": 0,
}


def resolve_share_ttl(preset: str) -> float:
    """Return absolute expiry timestamp for a TTL preset name."""
    if preset == "forever":
        return FOREVER_EXPIRES
    seconds = SHARE_TTL_SECONDS.get(preset, SHARE_TTL_SECONDS["1h"])
    return time.time() + seconds


def resolve_download_limit(preset: str) -> int:
    return SHARE_DOWNLOAD_LIMITS.get(preset, 0)


@dataclass
class ShareGrant:
    grant_id: str
    owner_hash: str
    recipient_hash: str
    root: Path
    expires: float
    label: str = "shared"
    max_downloads: int = 0
    download_count: int = 0
    revoked: bool = False
    ttl_preset: str = "1h"
    download_limit_preset: str = "unlimited"
    _temp_files: list[Path] = field(default_factory=list)

    def is_expired(self) -> bool:
        return time.time() >= self.expires

    def downloads_exhausted(self) -> bool:
        return self.max_downloads > 0 and self.download_count >= self.max_downloads

    def valid_for(self, requester_hash: str) -> bool:
        return (
            not self.revoked
            and not self.is_expired()
            and not self.downloads_exhausted()
            and secrets.compare_digest(self.recipient_hash, requester_hash)
        )

    def record_download(self) -> bool:
        if self.downloads_exhausted():
            return False
        self.download_count += 1
        return True

    def to_public_dict(self, *, remote: bool) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "owner_hash": self.owner_hash,
            "recipient_hash": self.recipient_hash,
            "label": self.label,
            "expires": self.expires,
            "max_downloads": self.max_downloads,
            "download_count": self.download_count,
            "downloads_remaining": (
                None
                if self.max_downloads == 0
                else max(0, self.max_downloads - self.download_count)
            ),
            "ttl_preset": self.ttl_preset,
            "download_limit_preset": self.download_limit_preset,
            "revoked": self.revoked,
            "remote": remote,
        }


class SharePeerMixin:
    """Offer and browse shared folders exclusively over E2EE sessions."""

    _share_grants: dict[str, ShareGrant]
    _remote_share_grants: dict[str, ShareGrant]

    def _init_share_peer(self: MessagingBackend) -> None:
        self._share_grants = {}
        self._remote_share_grants = {}
        self._share_listing_waiters: dict[str, asyncio.Future[list[dict[str, Any]]]] = {}

    def _share_root(self: MessagingBackend) -> Path:
        if self.config.incoming_dir:
            base = Path(self.config.incoming_dir).parent
        else:
            from srltcp.utils.platform import data_dir

            base = data_dir()
        shared = base / "shared"
        if hasattr(self, "_transfer_dir"):
            candidate = self._transfer_dir.parent / "shared"
            if candidate.exists():
                shared = candidate
        return shared.resolve()

    def _prune_share_grants(self: MessagingBackend) -> None:
        for store in (self._share_grants, self._remote_share_grants):
            expired = [
                gid
                for gid, g in store.items()
                if g.is_expired() or g.revoked
            ]
            for gid in expired:
                grant = store.pop(gid, None)
                if grant:
                    for tmp in grant._temp_files:
                        with contextlib.suppress(OSError):
                            tmp.unlink(missing_ok=True)

    def list_local_share_grants(self: MessagingBackend) -> list[dict[str, Any]]:
        self._prune_share_grants()
        return [g.to_public_dict(remote=False) for g in self._share_grants.values()]

    def list_remote_share_grants(self: MessagingBackend) -> list[dict[str, Any]]:
        self._prune_share_grants()
        return [g.to_public_dict(remote=True) for g in self._remote_share_grants.values()]

    def revoke_share_grant(self: MessagingBackend, grant_id: str) -> bool:
        grant = self._share_grants.get(grant_id)
        if not grant or grant.revoked:
            return False
        grant.revoked = True
        return True

    async def notify_share_revoked(
        self: MessagingBackend, grant_id: str, recipient_hash: str
    ) -> bool:
        grant = self._share_grants.get(grant_id)
        if not grant:
            return False
        grant.revoked = True
        link = self.get_link(recipient_hash)
        if not link or not link.handshake_complete:
            return True
        body = encode_payload({"action": "revoke", "grant_id": grant_id})
        packet = await self._encrypt_for_link(link, MessageType.SHARE_LIST, body)
        await self._send_raw(link.transport_peer_id, link.transport, packet)
        return True

    async def offer_share_folder(
        self: MessagingBackend,
        recipient_hash: str,
        *,
        folder: Path | None = None,
        label: str = "shared",
        ttl_preset: str = "1h",
        download_limit_preset: str = "unlimited",
    ) -> dict[str, Any] | None:
        if not self.trusted.is_trusted(recipient_hash):
            return None
        link = self.get_link(recipient_hash)
        if not link or not link.handshake_complete:
            return None
        root = (folder or self._share_root()).resolve()
        if not root.is_dir():
            raise ValueError("shared folder does not exist")
        identity = self._identity_for_transport(link.transport)
        grant_id = secrets.token_hex(16)
        expires = resolve_share_ttl(ttl_preset)
        max_downloads = resolve_download_limit(download_limit_preset)
        grant = ShareGrant(
            grant_id=grant_id,
            owner_hash=identity.hash_id,
            recipient_hash=recipient_hash,
            root=root,
            expires=expires,
            label=label or root.name,
            max_downloads=max_downloads,
            ttl_preset=ttl_preset,
            download_limit_preset=download_limit_preset,
        )
        self._share_grants[grant_id] = grant
        body = encode_payload(
            {
                "action": "offer",
                "grant_id": grant_id,
                "label": grant.label,
                "expires": grant.expires,
                "owner_hash": identity.hash_id,
                "max_downloads": grant.max_downloads,
                "ttl_preset": ttl_preset,
                "download_limit_preset": download_limit_preset,
            }
        )
        packet = await self._encrypt_for_link(link, MessageType.SHARE_LIST, body)
        await self._send_raw(link.transport_peer_id, link.transport, packet)
        log.info("Share offer %s to %s", grant_id[:8], recipient_hash[:8])
        return grant.to_public_dict(remote=False)

    async def request_share_list(
        self: MessagingBackend,
        owner_hash: str,
        grant_id: str,
        *,
        timeout: float = 12.0,
    ) -> list[dict[str, Any]] | None:
        grant = self._remote_share_grants.get(grant_id)
        if not grant or grant.owner_hash != owner_hash or grant.revoked:
            return None
        link = self.get_link(owner_hash)
        if not link or not link.handshake_complete:
            transport = link.transport if link else "tcp"
            await self.connect_to_peer(owner_hash, transport=transport)
            await self.wait_for_handshake(owner_hash, timeout=10.0)
            link = self.get_link(owner_hash)
        if not link or not link.handshake_complete:
            return None
        identity = self._identity_for_transport(link.transport)
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[list[dict[str, Any]]] = loop.create_future()
        self._share_listing_waiters[grant_id] = waiter
        body = encode_payload(
            {
                "action": "list",
                "grant_id": grant_id,
                "requester_hash": identity.hash_id,
            }
        )
        try:
            packet = await self._encrypt_for_link(link, MessageType.SHARE_REQUEST, body)
            await self._send_raw(link.transport_peer_id, link.transport, packet)
            return await asyncio.wait_for(waiter, timeout=timeout)
        except TimeoutError:
            log.warning("Share listing timeout for grant %s", grant_id[:8])
            return None
        finally:
            self._share_listing_waiters.pop(grant_id, None)

    async def request_share_file(
        self: MessagingBackend,
        owner_hash: str,
        grant_id: str,
        rel_path: str,
        *,
        as_folder: bool = False,
    ) -> bool:
        grant = self._remote_share_grants.get(grant_id)
        if not grant or grant.owner_hash != owner_hash or grant.revoked:
            return False
        link = self.get_link(owner_hash)
        if not link or not link.handshake_complete:
            return False
        identity = self._identity_for_transport(link.transport)
        body = encode_payload(
            {
                "action": "fetch",
                "grant_id": grant_id,
                "path": rel_path,
                "requester_hash": identity.hash_id,
                "as_folder": as_folder,
            }
        )
        packet = await self._encrypt_for_link(link, MessageType.SHARE_REQUEST, body)
        await self._send_raw(link.transport_peer_id, link.transport, packet)
        return True

    def _zip_share_path(self: MessagingBackend, grant: ShareGrant, rel: str) -> Path | None:
        rel_norm = rel.replace("\\", "/").strip("/")
        target = (grant.root / rel_norm).resolve() if rel_norm else grant.root
        if not str(target).startswith(str(grant.root.resolve())):
            return None
        if not target.exists():
            return None

        fd, tmp_name = tempfile.mkstemp(suffix=".zip", prefix="srltcp-share-")
        import os

        os.close(fd)
        zip_path = Path(tmp_name)
        base_name = target.name if target.is_dir() else f"{target.stem}.zip"

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            if target.is_file():
                zf.write(target, target.name)
            else:
                for entry in walk_directory(target):
                    if entry.get("type") != "file":
                        continue
                    rel_file = str(entry.get("name", ""))
                    full = target / rel_file
                    arcname = f"{base_name}/{rel_file}" if rel_norm else rel_file
                    zf.write(full, arcname)

        grant._temp_files.append(zip_path)
        return zip_path

    async def _handle_share_list(
        self: MessagingBackend, hash_id: str, body: bytes
    ) -> None:
        from srltcp.core.protocol.messages import decode_payload

        data = decode_payload(body)
        action = data.get("action", "")
        if action == "revoke":
            grant_id = data.get("grant_id", "")
            if grant_id in self._remote_share_grants:
                self._remote_share_grants[grant_id].revoked = True
                if self._on_event:
                    await self._on_event(
                        {
                            "kind": "share_revoked",
                            "hash_id": hash_id,
                            "grant_id": grant_id,
                        }
                    )
            return
        if action == "offer":
            grant_id = data.get("grant_id", "")
            if not grant_id:
                return
            identity = ""
            link = self.get_link(hash_id)
            if link:
                identity = self._identity_for_transport(link.transport).hash_id
            grant = ShareGrant(
                grant_id=grant_id,
                owner_hash=hash_id,
                recipient_hash=identity,
                root=Path("/"),
                expires=float(data.get("expires", time.time() + 3600)),
                label=data.get("label", "shared"),
                max_downloads=int(data.get("max_downloads", 0)),
                ttl_preset=data.get("ttl_preset", "1h"),
                download_limit_preset=data.get("download_limit_preset", "unlimited"),
            )
            self._remote_share_grants[grant_id] = grant
            if self._on_event:
                await self._on_event(
                    {
                        "kind": "share_offer",
                        "hash_id": hash_id,
                        "grant_id": grant_id,
                        "label": grant.label,
                        "expires": grant.expires,
                        "max_downloads": grant.max_downloads,
                    }
                )
            return
        if action == "listing":
            grant_id = data.get("grant_id", "")
            entries = data.get("entries", [])
            normalized = []
            for entry in entries:
                name = str(entry.get("name", entry.get("path", "")))
                normalized.append(
                    {
                        "name": name,
                        "path": name,
                        "type": entry.get("type", "file"),
                        "size": entry.get("size", 0),
                    }
                )
            waiter = self._share_listing_waiters.get(grant_id)
            if waiter and not waiter.done():
                waiter.set_result(normalized)
            if self._on_event:
                payload: dict[str, Any] = {
                    "kind": "share_listing",
                    "hash_id": hash_id,
                    "grant_id": grant_id,
                    "entries": normalized,
                }
                if data.get("error"):
                    payload["error"] = data["error"]
                await self._on_event(payload)

    async def _handle_share_request(
        self: MessagingBackend, hash_id: str, body: bytes
    ) -> None:
        from srltcp.core.protocol.messages import decode_payload

        data = decode_payload(body)
        grant_id = data.get("grant_id", "")
        requester = str(data.get("requester_hash") or hash_id)
        grant = self._share_grants.get(grant_id)
        action = data.get("action", "")
        link = self.get_link(hash_id)
        if not link or not link.handshake_complete:
            return

        if not grant or not grant.valid_for(requester):
            log.warning(
                "Share request denied for grant %s from %s",
                grant_id[:8],
                requester[:8],
            )
            if action == "list" and grant_id:
                denied = encode_payload(
                    {
                        "action": "listing",
                        "grant_id": grant_id,
                        "entries": [],
                        "error": "access_denied",
                    }
                )
                packet = await self._encrypt_for_link(link, MessageType.SHARE_LIST, denied)
                await self._send_raw(link.transport_peer_id, link.transport, packet)
            return

        if action == "list":
            entries = walk_directory(grant.root)[:MAX_SHARE_ENTRIES]
            listing = [
                {
                    "name": str(e.get("name", "")),
                    "path": str(e.get("name", "")),
                    "type": e.get("type", "file"),
                    "size": e.get("size", 0),
                }
                for e in entries
            ]
            response = encode_payload(
                {
                    "action": "listing",
                    "grant_id": grant_id,
                    "entries": listing,
                }
            )
            packet = await self._encrypt_for_link(link, MessageType.SHARE_LIST, response)
            await self._send_raw(link.transport_peer_id, link.transport, packet)
            return

        if action == "fetch":
            rel = str(data.get("path", "")).replace("\\", "/").lstrip("/")
            if ".." in rel.split("/"):
                return
            as_folder = bool(data.get("as_folder", False))
            root_resolved = grant.root.resolve()
            target = (root_resolved / rel).resolve() if rel else root_resolved
            if not str(target).startswith(str(root_resolved)):
                return

            send_path: Path | None = None
            if as_folder or target.is_dir():
                send_path = self._zip_share_path(grant, rel)
            elif target.is_file():
                send_path = target
            if not send_path or not send_path.is_file():
                log.warning("Share fetch path invalid: %s", rel)
                return

            if not grant.record_download():
                log.warning("Share download limit reached for grant %s", grant_id[:8])
                return

            transfer = await self.offer_file(hash_id, send_path, transport=link.transport)
            if transfer:
                log.info(
                    "Share file %s sent to %s (grant %s)",
                    send_path.name,
                    hash_id[:8],
                    grant_id[:8],
                )
            else:
                grant.download_count = max(0, grant.download_count - 1)
                log.warning("Share offer_file failed for grant %s", grant_id[:8])