"""Discovery registry tests."""

from __future__ import annotations

from srltcp.core.discovery import DiscoveryRegistry
from srltcp.core.protocol.messages import encode_payload


def test_upsert_tcp_and_serial_separate() -> None:
    reg = DiscoveryRegistry()
    tcp_payload = encode_payload(
        {
            "type": "announce",
            "hash_id": "aaa111",
            "name": "node-a",
            "transport": "tcp",
            "public_key": "aa" * 32,
            "tcp_host": "10.0.0.1",
            "tcp_port": 7825,
        }
    )
    serial_payload = encode_payload(
        {
            "type": "announce",
            "hash_id": "bbb222",
            "name": "node-a",
            "transport": "serial",
            "public_key": "bb" * 32,
            "tcp_host": "",
            "tcp_port": 7825,
        }
    )
    p1, new1 = reg.upsert_from_announce("10.0.0.1:1234", "tcp", tcp_payload)
    p2, new2 = reg.upsert_from_announce("serial", "serial", serial_payload)
    assert new1 and new2
    assert p1 and p2
    assert p1.transport == "tcp"
    assert p2.transport == "serial"
    peers = reg.list_peers()
    assert len(peers) == 2


def test_get_by_hash_id() -> None:
    reg = DiscoveryRegistry()
    payload = encode_payload(
        {
            "type": "announce",
            "hash_id": "ccc333",
            "name": "peer",
            "transport": "tcp",
            "public_key": "cc" * 32,
        }
    )
    reg.upsert_from_announce("10.0.0.2:9999", "tcp", payload)
    peer = reg.get("ccc333")
    assert peer is not None
    assert peer.name == "peer"