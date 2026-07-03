"""Discovery registry tests."""

from __future__ import annotations

from srltcp.core.discovery import DiscoveryRegistry
from srltcp.core.protocol.messages import encode_payload


def test_announce_upsert() -> None:
    registry = DiscoveryRegistry(ttl=60)
    payload = encode_payload(
        {
            "type": "announce",
            "hash_id": "abc123" * 5 + "ab",
            "name": "peer-one",
            "public_key": "aa" * 32,
            "tcp_host": "10.0.0.5",
            "tcp_port": 7825,
        }
    )
    peer = registry.upsert_from_announce("10.0.0.5:7825", "tcp", payload)
    assert peer is not None
    assert peer.name == "peer-one"
    assert len(registry.list_peers()) == 1