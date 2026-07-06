"""Discovered peer filtering tests."""

from __future__ import annotations

from srltcp.core.identity import Identity
from srltcp.core.messaging.backend import MessagingBackend, NodeConfig
from srltcp.core.protocol.signed import sign_payload
from srltcp.core.trusted import TrustedPeer


def _signed_announce(identity: Identity, *, tcp_host: str = "10.0.0.5") -> bytes:
    return sign_payload(
        {
            "type": "announce",
            "hash_id": identity.hash_id,
            "name": identity.name,
            "public_key": identity.public_bytes().hex(),
            "transport": "tcp",
            "tcp_host": tcp_host,
            "tcp_port": 7825,
        },
        identity.private_key,
    )


def test_discovered_excludes_trusted() -> None:
    backend = MessagingBackend(NodeConfig())
    reg = backend.discovery
    trusted_id = Identity.generate("trusted", "tcp")
    discovered_id = Identity.generate("discovered", "tcp")
    reg.upsert_from_announce(
        "10.0.0.5:1111", "tcp", _signed_announce(trusted_id, tcp_host="10.0.0.5")
    )
    reg.upsert_from_announce(
        "10.0.0.6:2222", "tcp", _signed_announce(discovered_id, tcp_host="10.0.0.6")
    )
    backend.trusted.add(
        TrustedPeer(
            hash_id=trusted_id.hash_id,
            name="trusted",
            transport="tcp",
            public_key=trusted_id.public_bytes().hex(),
            tcp_host="10.0.0.5",
        )
    )
    peers = backend.get_discovered_peers()
    ids = {p["hash_id"] for p in peers}
    assert trusted_id.hash_id not in ids
    assert discovered_id.hash_id in ids