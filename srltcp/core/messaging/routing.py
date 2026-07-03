"""Multi-hop routing table for relay mode."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RouteEntry:
    dest_hash: str
    next_hop_peer_id: str
    transport: str
    hops: int = 1
    last_updated: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dest_hash": self.dest_hash,
            "next_hop_peer_id": self.next_hop_peer_id,
            "transport": self.transport,
            "hops": self.hops,
            "last_updated": self.last_updated,
        }


class RoutingTable:
    """Simple TTL-based routing table for relay forwarding."""

    def __init__(self, ttl: float = 300.0) -> None:
        self._routes: dict[str, RouteEntry] = {}
        self.ttl = ttl

    def update(self, dest_hash: str, next_hop_peer_id: str, transport: str, hops: int) -> None:
        existing = self._routes.get(dest_hash)
        if existing and existing.hops <= hops:
            return
        self._routes[dest_hash] = RouteEntry(
            dest_hash=dest_hash,
            next_hop_peer_id=next_hop_peer_id,
            transport=transport,
            hops=hops,
        )

    def get(self, dest_hash: str) -> RouteEntry | None:
        entry = self._routes.get(dest_hash)
        if entry and time.time() - entry.last_updated > self.ttl:
            del self._routes[dest_hash]
            return None
        return entry

    def remove_for_peer(self, peer_id: str) -> None:
        expired = [h for h, e in self._routes.items() if e.next_hop_peer_id == peer_id]
        for h in expired:
            del self._routes[h]

    def all_routes(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self._routes.values()]