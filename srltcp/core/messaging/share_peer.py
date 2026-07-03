"""End-to-end encrypted peer folder sharing over established links."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from srltcp.core.protocol.messages import MessageType, encode_payload
from srltcp.utils.files import walk_directory
from srltcp.utils.logging import get_logger

if TYPE_CHECKING:
    from srltcp.core.messaging.backend import MessagingBackend

log = get_logger(__name__)

SHARE_GRANT_TTL = 7200.0
MAX_SHARE_ENTRIES = 500


@dataclass
class ShareGrant:
    grant_id: str
    owner_hash: str
    recipient_hash: str
    root: Path
    expires: float
    label: str = "shared"

    def valid_for(self, requester_hash: str) -> bool:
        return (
            time.time() < self.expires
            and secrets.compare_digest(self.recipient_hash, requester_hash)
        )


class SharePeerMixin:
    """Offer and browse shared folders exclusively over E2EE sessions."""

    _share_grants: dict[str, ShareGrant]
    _remote_share_grants: dict[str, ShareGrant]

    def _init_share_peer(self: MessagingBackend) -> None:
        self._share_grants = {}
        self._remote_share_grants = {}

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
        now = time.time()
        for store in (self._share_grants, self._remote_share_grants):
            expired = [gid for gid, g in store.items() if g.expires <= now]
            for gid in expired:
                store.pop(gid, None)

    def list_local_share_grants(self: MessagingBackend) -> list[dict[str, Any]]:
        self._prune_share_grants()
        return [
            {
                "grant_id": g.grant_id,
                "owner_hash": g.owner_hash,
                "recipient_hash": g.recipient_hash,
                "label": g.label,
                "expires": g.expires,
                "remote": False,
            }
            for g in self._share_grants.values()
        ]

    def list_remote_share_grants(self: MessagingBackend) -> list[dict[str, Any]]:
        self._prune_share_grants()
        return [
            {
                "grant_id": g.grant_id,
                "owner_hash": g.owner_hash,
                "recipient_hash": g.recipient_hash,
                "label": g.label,
                "expires": g.expires,
                "remote": True,
            }
            for g in self._remote_share_grants.values()
        ]

    async def offer_share_folder(
        self: MessagingBackend,
        recipient_hash: str,
        *,
        folder: Path | None = None,
        label: str = "shared",
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
        grant = ShareGrant(
            grant_id=grant_id,
            owner_hash=identity.hash_id,
            recipient_hash=recipient_hash,
            root=root,
            expires=time.time() + SHARE_GRANT_TTL,
            label=label or root.name,
        )
        self._share_grants[grant_id] = grant
        body = encode_payload(
            {
                "action": "offer",
                "grant_id": grant_id,
                "label": grant.label,
                "expires": grant.expires,
                "owner_hash": identity.hash_id,
            }
        )
        packet = await self._encrypt_for_link(link, MessageType.SHARE_LIST, body)
        await self._send_raw(link.transport_peer_id, link.transport, packet)
        log.info("Share offer %s to %s", grant_id[:8], recipient_hash[:8])
        return {
            "grant_id": grant_id,
            "recipient_hash": recipient_hash,
            "label": grant.label,
            "expires": grant.expires,
        }

    async def request_share_list(
        self: MessagingBackend, owner_hash: str, grant_id: str
    ) -> bool:
        grant = self._remote_share_grants.get(grant_id)
        if not grant or grant.owner_hash != owner_hash:
            return False
        link = self.get_link(owner_hash)
        if not link or not link.handshake_complete:
            return False
        identity = self._identity_for_transport(link.transport)
        body = encode_payload(
            {
                "action": "list",
                "grant_id": grant_id,
                "requester_hash": identity.hash_id,
            }
        )
        packet = await self._encrypt_for_link(link, MessageType.SHARE_REQUEST, body)
        await self._send_raw(link.transport_peer_id, link.transport, packet)
        return True

    async def request_share_file(
        self: MessagingBackend,
        owner_hash: str,
        grant_id: str,
        rel_path: str,
    ) -> bool:
        grant = self._remote_share_grants.get(grant_id)
        if not grant or grant.owner_hash != owner_hash:
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
            }
        )
        packet = await self._encrypt_for_link(link, MessageType.SHARE_REQUEST, body)
        await self._send_raw(link.transport_peer_id, link.transport, packet)
        return True

    async def _handle_share_list(
        self: MessagingBackend, hash_id: str, body: bytes
    ) -> None:
        from srltcp.core.protocol.messages import decode_payload

        data = decode_payload(body)
        action = data.get("action", "")
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
                expires=float(data.get("expires", time.time() + SHARE_GRANT_TTL)),
                label=data.get("label", "shared"),
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
                    }
                )
            return
        if action == "listing":
            grant_id = data.get("grant_id", "")
            if self._on_event:
                await self._on_event(
                    {
                        "kind": "share_listing",
                        "hash_id": hash_id,
                        "grant_id": grant_id,
                        "entries": data.get("entries", []),
                    }
                )

    async def _handle_share_request(
        self: MessagingBackend, hash_id: str, body: bytes
    ) -> None:
        from srltcp.core.protocol.messages import decode_payload

        data = decode_payload(body)
        grant_id = data.get("grant_id", "")
        grant = self._share_grants.get(grant_id)
        if not grant or not grant.valid_for(hash_id):
            log.warning("Share request denied for grant %s from %s", grant_id[:8], hash_id[:8])
            return
        action = data.get("action", "")
        link = self.get_link(hash_id)
        if not link or not link.handshake_complete:
            return

        if action == "list":
            entries = walk_directory(grant.root)[:MAX_SHARE_ENTRIES]
            response = encode_payload(
                {
                    "action": "listing",
                    "grant_id": grant_id,
                    "entries": entries,
                }
            )
            packet = await self._encrypt_for_link(link, MessageType.SHARE_LIST, response)
            await self._send_raw(link.transport_peer_id, link.transport, packet)
            return

        if action == "fetch":
            rel = str(data.get("path", "")).replace("\\", "/").lstrip("/")
            if ".." in rel.split("/"):
                return
            target = (grant.root / rel).resolve()
            if not str(target).startswith(str(grant.root)) or not target.is_file():
                return
            await self.offer_file(hash_id, target, transport=link.transport)