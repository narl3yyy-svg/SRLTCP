"""Discovered peer filtering tests."""

from __future__ import annotations

from srltcp.core.messaging.backend import MessagingBackend, NodeConfig
from srltcp.core.protocol.messages import encode_payload
from srltcp.core.trusted import TrustedPeer


def _announce(hash_id: str, name: str, transport: str = "tcp") -> bytes:
    return encode_payload(
        {
            "type": "announce",
            "hash_id": hash_id,
            "name": name,
            "transport": transport,
            "public_key": "aa" * 32,
            "tcp_host": "10.0.0.5",
            "tcp_port": 7825,
        }
    )


def test_discovered_excludes_trusted() -> None:
    backend = MessagingBackend(NodeConfig())
    reg = backend.discovery
    trusted_hash = "aa" * 32
    discovered_hash = "bb" * 32
    reg.upsert_from_announce("10.0.0.5:1111", "tcp", _announce(trusted_hash, "trusted"))
    reg.upsert_from_announce("10.0.0.6:2222", "tcp", _announce(discovered_hash, "discovered"))
    backend.trusted.add(
        TrustedPeer(
            hash_id=trusted_hash,
            name="trusted",
            transport="tcp",
            public_key="aa" * 32,
            tcp_host="10.0.0.5",
        )
    )
    peers = backend.get_discovered_peers()
    ids = {p["hash_id"] for p in peers}
    assert trusted_hash not in ids
    assert discovered_hash in ids