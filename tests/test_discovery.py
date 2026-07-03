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
    peer, is_new = registry.upsert_from_announce("10.0.0.5:7825", "tcp", payload)
    assert peer is not None
    assert is_new is True
    assert peer.name == "peer-one"
    assert len(registry.list_peers()) == 1

    peer2, is_new2 = registry.upsert_from_announce("10.0.0.5:7825", "tcp", payload)
    assert peer2 is not None
    assert is_new2 is False


def test_update_metrics() -> None:
    registry = DiscoveryRegistry(ttl=60)
    payload = encode_payload(
        {
            "type": "announce",
            "hash_id": "def456" * 5 + "de",
            "name": "peer-two",
            "public_key": "bb" * 32,
        }
    )
    peer, _ = registry.upsert_from_announce("10.0.0.6:7825", "tcp", payload)
    assert peer is not None
    registry.update_metrics(peer.hash_id, rtt_ms=12.5, link_quality_pct=98.0)
    updated = registry.get(peer.hash_id)
    assert updated is not None
    assert updated.rtt_ms == 12.5
    assert updated.link_quality_pct == 98.0