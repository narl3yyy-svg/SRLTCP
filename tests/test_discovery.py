"""Discovery registry tests."""

from __future__ import annotations

from srltcp.core.discovery import DiscoveryRegistry
from srltcp.core.identity import Identity
from srltcp.core.protocol.signed import sign_payload


def _signed_announce(identity: Identity, *, transport: str, tcp_host: str = "10.0.0.1") -> bytes:
    return sign_payload(
        {
            "type": "announce",
            "hash_id": identity.hash_id,
            "name": identity.name,
            "public_key": identity.public_bytes().hex(),
            "transport": transport,
            "tcp_host": tcp_host,
            "tcp_port": 7825,
        },
        identity.private_key,
    )


def test_upsert_tcp_and_serial_separate() -> None:
    reg = DiscoveryRegistry()
    tcp_id = Identity.generate("node-a", "tcp")
    serial_id = Identity.generate("node-a", "serial")
    p1, new1 = reg.upsert_from_announce(
        "10.0.0.1:1234", "tcp", _signed_announce(tcp_id, transport="tcp")
    )
    p2, new2 = reg.upsert_from_announce(
        "serial", "serial", _signed_announce(serial_id, transport="serial", tcp_host="")
    )
    assert new1 and new2
    assert p1 and p2
    assert p1.transport == "tcp"
    assert p2.transport == "serial"
    peers = reg.list_peers()
    assert len(peers) == 2


def test_get_by_hash_id() -> None:
    reg = DiscoveryRegistry()
    identity = Identity.generate("peer", "tcp")
    reg.upsert_from_announce(
        "10.0.0.2:9999", "tcp", _signed_announce(identity, transport="tcp")
    )
    peer = reg.get(identity.hash_id)
    assert peer is not None
    assert peer.name == "peer"