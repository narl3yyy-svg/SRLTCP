"""Peer discovery and announce handling."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from srltcp.core.protocol.messages import decode_payload


@dataclass
class DiscoveredPeer:
    hash_id: str
    name: str
    transport: str
    address: str
    public_key: str
    tcp_host: str = ""
    tcp_port: int = 7825
    last_seen: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hash_id": self.hash_id,
            "name": self.name,
            "transport": self.transport,
            "address": self.address,
            "public_key": self.public_key,
            "tcp_host": self.tcp_host,
            "tcp_port": self.tcp_port,
            "last_seen": self.last_seen,
            "metadata": self.metadata,
        }


class DiscoveryRegistry:
    """Track discovered peers with TTL expiry."""

    def __init__(self, ttl: float = 60.0) -> None:
        self._peers: dict[str, DiscoveredPeer] = {}
        self.ttl = ttl

    def upsert_from_announce(
        self, address: str, transport: str, payload: bytes
    ) -> DiscoveredPeer | None:
        try:
            data = decode_payload(payload)
        except Exception:
            return None
        if data.get("type") != "announce":
            return None
        hash_id = data.get("hash_id", "")
        if not hash_id:
            return None
        peer = DiscoveredPeer(
            hash_id=hash_id,
            name=data.get("name", "unknown"),
            transport=transport,
            address=address,
            public_key=data.get("public_key", ""),
            tcp_host=data.get("tcp_host", ""),
            tcp_port=int(data.get("tcp_port", 7825)),
            last_seen=time.time(),
            metadata=data,
        )
        self._peers[hash_id] = peer
        return peer

    def get(self, hash_id: str) -> DiscoveredPeer | None:
        peer = self._peers.get(hash_id)
        if peer and time.time() - peer.last_seen > self.ttl:
            del self._peers[hash_id]
            return None
        return peer

    def list_peers(self) -> list[DiscoveredPeer]:
        now = time.time()
        alive: list[DiscoveredPeer] = []
        expired: list[str] = []
        for hash_id, peer in self._peers.items():
            if now - peer.last_seen > self.ttl:
                expired.append(hash_id)
            else:
                alive.append(peer)
        for hash_id in expired:
            del self._peers[hash_id]
        return sorted(alive, key=lambda p: p.name.lower())

    def remove(self, hash_id: str) -> None:
        self._peers.pop(hash_id, None)