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

    # Anyone can make a fresh identity and sign announces for it, so the
    # registry is bounded: an announce flood can't exhaust memory.
    MAX_PEERS = 1024

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
        if not isinstance(data, dict):
            return None, False
        ann_type = data.get("type", "")
        if ann_type == "announce":
            if not verify_signed_payload(data):
                log.debug("Rejected unsigned or invalid announce signature")
                return None, False
        else:
            return None, False
        hash_id = str(data.get("hash_id", "")).lower()
        if not hash_id:
            return None, False
        ann_transport = str(data.get("transport", transport))
        if ann_transport not in ("tcp", "serial"):
            ann_transport = transport
        try:
            tcp_port = int(data.get("tcp_port", 7825))
        except (TypeError, ValueError):
            tcp_port = 7825
        if not 1 <= tcp_port <= 65535:
            tcp_port = 7825
        tcp_host = str(data.get("tcp_host", ""))[:255]
        if ann_transport == "tcp" and address and ":" in address:
            # Trust the packet's source IP over the self-reported address so a
            # LAN host cannot point us at an arbitrary third party.
            src_ip = address.rsplit(":", 1)[0]
            if src_ip and src_ip not in ("127.0.0.1", "::1") and tcp_host != src_ip:
                tcp_host = src_ip
        peer_key = f"{ann_transport}:{hash_id}"
        is_new = peer_key not in self._peers
        existing = self._peers.get(peer_key)
        peer = DiscoveredPeer(
            hash_id=hash_id,
            name=str(data.get("name", "unknown"))[:64],
            transport=ann_transport,
            address=address,
            public_key=str(data.get("public_key", "")),
            tcp_host=tcp_host,
            tcp_port=tcp_port,
            last_seen=time.time(),
            rtt_ms=existing.rtt_ms if existing else None,
            link_quality_pct=existing.link_quality_pct if existing else None,
            metadata={
                "type": ann_type,
                "discovery_port": data.get("discovery_port"),
                "reply": bool(data.get("reply")),
                "ts": data.get("ts") if isinstance(data.get("ts"), int | float) else 0,
            },
        )
        self._peers[peer_key] = peer
        if len(self._peers) > self.MAX_PEERS:
            self._evict(keep=peer_key)
        return peer, is_new

    def _evict(self, keep: str) -> None:
        """Drop expired entries, then the stalest ones (contact requests last)."""
        now = time.time()
        for key in [k for k, p in self._peers.items() if now - p.last_seen > self._ttl_for(p)]:
            if key != keep:
                del self._peers[key]
        if len(self._peers) <= self.MAX_PEERS:
            return
        order = sorted(
            (k for k in self._peers if k != keep),
            key=lambda k: (bool(self._peers[k].metadata.get("contact_request")),
                           self._peers[k].last_seen),
        )
        for key in order[: len(self._peers) - self.MAX_PEERS]:
            del self._peers[key]

    def upsert_contact_request(
        self,
        *,
        hash_id: str,
        name: str,
        public_key: str,
        transport: str,
        address: str,
    ) -> DiscoveredPeer:
        """Record a verified peer that connected to us but is not trusted yet."""
        key = f"{transport}:{hash_id}"
        existing = self._peers.get(key)
        tcp_host = existing.tcp_host if existing else ""
        tcp_port = existing.tcp_port if existing else 7825
        peer = DiscoveredPeer(
            hash_id=hash_id,
            name=(name or (existing.name if existing else "") or f"Peer {hash_id[:8]}")[:64],
            transport=transport,
            address=address,
            public_key=public_key,
            tcp_host=tcp_host,
            tcp_port=tcp_port,
            last_seen=time.time(),
            metadata={"contact_request": True},
        )
        self._peers[key] = peer
        return peer

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

    def _ttl_for(self, peer: DiscoveredPeer) -> float:
        # Contact requests stay visible until the user acts on them (or a day).
        return 86400.0 if peer.metadata.get("contact_request") else self.ttl

    def get(self, hash_id: str, transport: str | None = None) -> DiscoveredPeer | None:
        key = self._find_key(hash_id, transport)
        if not key:
            return None
        peer = self._peers.get(key)
        if peer and time.time() - peer.last_seen > self._ttl_for(peer):
            del self._peers[key]
            return None
        return peer

    def list_peers(self) -> list[DiscoveredPeer]:
        now = time.time()
        alive: list[DiscoveredPeer] = []
        expired: list[str] = []
        for key, peer in self._peers.items():
            if now - peer.last_seen > self._ttl_for(peer):
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