"""Hub presence registry — maps peer hash IDs to live hub connections."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class HubMember:
    hash_id: str
    conn_peer_id: str
    announce: bytes
    registered_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hash_id": self.hash_id,
            "conn_peer_id": self.conn_peer_id,
            "registered_at": self.registered_at,
        }


class HubPresenceRegistry:
    """Track clients connected to a headless hub server."""

    def __init__(self) -> None:
        self._by_hash: dict[str, HubMember] = {}
        self._by_conn: dict[str, str] = {}

    def register(self, hash_id: str, conn_peer_id: str, announce: bytes) -> HubMember | None:
        """Register or replace a member. Returns previous member on same conn if hash changed."""
        old_conn_hash = self._by_conn.get(conn_peer_id)
        previous: HubMember | None = None
        if old_conn_hash and old_conn_hash != hash_id:
            previous = self._by_hash.pop(old_conn_hash, None)
        if hash_id in self._by_hash:
            previous = self._by_hash[hash_id]
        member = HubMember(hash_id=hash_id, conn_peer_id=conn_peer_id, announce=announce)
        self._by_hash[hash_id] = member
        self._by_conn[conn_peer_id] = hash_id
        return previous

    def unregister_conn(self, conn_peer_id: str) -> HubMember | None:
        hash_id = self._by_conn.pop(conn_peer_id, None)
        if not hash_id:
            return None
        return self._by_hash.pop(hash_id, None)

    def get_conn(self, hash_id: str) -> str | None:
        member = self._by_hash.get(hash_id)
        return member.conn_peer_id if member else None

    def get_member(self, hash_id: str) -> HubMember | None:
        return self._by_hash.get(hash_id)

    def list_members(self) -> list[HubMember]:
        return list(self._by_hash.values())

    def list_other_conns(self, exclude_conn: str) -> list[str]:
        return [m.conn_peer_id for m in self._by_hash.values() if m.conn_peer_id != exclude_conn]