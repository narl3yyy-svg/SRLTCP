"""Peer link management mixin."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from srltcp.core.protocol.crypto import CryptoBox, SessionKeys

if TYPE_CHECKING:
    from srltcp.core.messaging.backend import MessagingBackend


@dataclass
class PeerLink:
    hash_id: str
    transport_peer_id: str
    transport: str
    address: str
    public_key: bytes
    crypto: CryptoBox = field(default_factory=CryptoBox)
    connected: bool = False
    handshake_complete: bool = False
    last_ping: float = 0.0
    rtt_ms: float | None = None
    link_quality_pct: float | None = None
    peer_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hash_id": self.hash_id,
            "transport_peer_id": self.transport_peer_id,
            "transport": self.transport,
            "address": self.address,
            "connected": self.connected,
            "handshake_complete": self.handshake_complete,
            "rtt_ms": self.rtt_ms,
            "link_quality_pct": self.link_quality_pct,
            "peer_name": self.peer_name,
            "metadata": self.metadata,
        }


class PeerLinkMixin:
    """Manage active peer links keyed by identity hash."""

    _links: dict[str, PeerLink]
    _peer_id_to_hash: dict[str, str]

    def _init_links(self: MessagingBackend) -> None:
        self._links = {}
        self._peer_id_to_hash = {}

    def get_link(self: MessagingBackend, hash_id: str) -> PeerLink | None:
        return self._links.get(hash_id)

    def get_link_by_peer_id(self: MessagingBackend, peer_id: str) -> PeerLink | None:
        hash_id = self._peer_id_to_hash.get(peer_id)
        if hash_id:
            return self._links.get(hash_id)
        return None

    def register_link(self: MessagingBackend, link: PeerLink) -> None:
        self._links[link.hash_id] = link
        self._peer_id_to_hash[link.transport_peer_id] = link.hash_id

    def remove_link(self: MessagingBackend, hash_id: str) -> None:
        link = self._links.pop(hash_id, None)
        if link:
            self._peer_id_to_hash.pop(link.transport_peer_id, None)

    def list_links(self: MessagingBackend) -> list[dict[str, Any]]:
        return [link.to_dict() for link in self._links.values()]

    def set_link_keys(self: MessagingBackend, hash_id: str, keys: SessionKeys) -> None:
        link = self._links.get(hash_id)
        if link:
            link.crypto.set_keys(keys)
            link.handshake_complete = True
            link.connected = True

    def touch_link(self: MessagingBackend, hash_id: str) -> None:
        link = self._links.get(hash_id)
        if link:
            link.last_ping = time.time()