"""Peer discovery and announce handling."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from srltcp.core.protocol.messages import decode_payload
from srltcp.core.protocol.signed import verify_signed_payload
from srltcp.utils.logging import get_logger

log = get_logger(__name__)


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
    rtt_ms: float | None = None
    link_quality_pct: float | None = None
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
            "rtt_ms": self.rtt_ms,
            "link_quality_pct": self.link_quality_pct,
            "metadata": self.metadata,
        }


class DiscoveryRegistry:
    """Track discovered peers with TTL expiry."""

    def __init__(self, ttl: float = 120.0) -> None:
        self._peers: dict[str, DiscoveredPeer] = {}
        self.ttl = ttl

    def upsert_from_announce(
        self, address: str, transport: str, payload: bytes
    ) -> tuple[DiscoveredPeer | None, bool]:
        try:
            data = decode_payload(payload)
        except Exception:
            return None, False
        ann_type = data.get("type", "")
        if ann_type not in ("announce", "hub_presence", "hub_register"):
            return None, False
        if ann_type == "announce":
            if not verify_signed_payload(data):
                log.debug("Rejected unsigned or invalid announce signature")
                return None, False
        hash_id = data.get("hash_id", "")
        if not hash_id:
            return None, False
        ann_transport = str(data.get("transport", transport))
        if data.get("via_hub") or ann_type in ("hub_presence", "hub_register"):
            ann_transport = "hub"
        peer_key = f"{ann_transport}:{hash_id}"
        is_new = peer_key not in self._peers
        existing = self._peers.get(peer_key)
        peer = DiscoveredPeer(
            hash_id=hash_id,
            name=data.get("name", "unknown"),
            transport=ann_transport,
            address=address,
            public_key=data.get("public_key", ""),
            tcp_host=data.get("tcp_host", ""),
            tcp_port=int(data.get("tcp_port", 7825)),
            last_seen=time.time(),
            rtt_ms=existing.rtt_ms if existing else None,
            link_quality_pct=existing.link_quality_pct if existing else None,
            metadata=data,
        )
        self._peers[peer_key] = peer
        return peer, is_new

    def _find_key(self, hash_id: str, transport: str | None = None) -> str | None:
        if transport:
            key = f"{transport}:{hash_id}"
            return key if key in self._peers else None
        for key in self._peers:
            if key.endswith(f":{hash_id}"):
                return key
        return None

    def update_metrics(
        self,
        hash_id: str,
        *,
        rtt_ms: float | None = None,
        link_quality_pct: float | None = None,
    ) -> None:
        key = self._find_key(hash_id)
        if not key:
            return
        peer = self._peers.get(key)
        if not peer:
            return
        if rtt_ms is not None:
            peer.rtt_ms = rtt_ms
        if link_quality_pct is not None:
            peer.link_quality_pct = link_quality_pct

    def get(self, hash_id: str, transport: str | None = None) -> DiscoveredPeer | None:
        key = self._find_key(hash_id, transport)
        if not key:
            return None
        peer = self._peers.get(key)
        if peer and time.time() - peer.last_seen > self.ttl:
            del self._peers[key]
            return None
        return peer

    def list_peers(self) -> list[DiscoveredPeer]:
        now = time.time()
        alive: list[DiscoveredPeer] = []
        expired: list[str] = []
        for key, peer in self._peers.items():
            if now - peer.last_seen > self.ttl:
                expired.append(key)
            else:
                alive.append(peer)
        for key in expired:
            del self._peers[key]
        return sorted(alive, key=lambda p: (p.name.lower(), p.transport))

    def remove(self, hash_id: str) -> None:
        for key in list(self._peers):
            if key.endswith(f":{hash_id}"):
                del self._peers[key]