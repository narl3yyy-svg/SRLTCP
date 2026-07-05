"""Peer link management tests."""

from __future__ import annotations

from srltcp.core.messaging.backend import MessagingBackend, NodeConfig
from srltcp.core.messaging.links import PeerLink


def test_remove_link_for_peer_only_active() -> None:
    backend = MessagingBackend(NodeConfig())
    link_a = PeerLink(
        hash_id="aaa" * 10 + "aa",
        transport_peer_id="peer-a",
        transport="tcp",
        address="10.0.0.1:7825",
        public_key=b"\x01" * 32,
        peer_name="alice",
    )
    backend.register_link(link_a)
    assert backend.remove_link_for_peer("peer-a") == link_a.hash_id
    assert backend.get_link(link_a.hash_id) is None
    assert backend.remove_link_for_peer("peer-a") is None


def test_register_link_replaces_stale_peer_mapping() -> None:
    backend = MessagingBackend(NodeConfig())
    hash_id = "bbb" * 10 + "bb"
    old = PeerLink(
        hash_id=hash_id,
        transport_peer_id="old-peer",
        transport="tcp",
        address="10.0.0.2:7825",
        public_key=b"\x02" * 32,
    )
    new = PeerLink(
        hash_id=hash_id,
        transport_peer_id="new-peer",
        transport="tcp",
        address="10.0.0.2:7826",
        public_key=b"\x02" * 32,
    )
    backend.register_link(old)
    backend.register_link(new)
    assert backend.get_link_by_peer_id("old-peer") is None
    assert backend.get_link_by_peer_id("new-peer") is not None