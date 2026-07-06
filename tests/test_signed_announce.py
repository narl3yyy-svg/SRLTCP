"""Signed LAN announce tests."""

from __future__ import annotations

from srltcp.core.discovery import DiscoveryRegistry
from srltcp.core.identity import Identity
from srltcp.core.messaging.announce import AnnounceMixin
from srltcp.core.protocol.messages import decode_payload, encode_payload
from srltcp.core.protocol.signed import sign_payload, verify_signed_payload


class _AnnounceHost(AnnounceMixin):
    def __init__(self, identity: Identity) -> None:
        self.identities = {"tcp": identity}
        self.config = type("Cfg", (), {"tcp_port": 7825, "lan_ip": "10.0.0.1"})()
        self.tcp_transport = type("Tcp", (), {"discovery_port": 7826})()


def test_sign_and_verify_announce_payload() -> None:
    identity = Identity.generate("alice", "tcp")
    host = _AnnounceHost(identity)
    payload = host.build_announce_payload("tcp")
    data = decode_payload(payload)
    assert data["type"] == "announce"
    assert verify_signed_payload(data)


def test_discovery_rejects_unsigned_announce() -> None:
    reg = DiscoveryRegistry()
    unsigned = encode_payload(
        {
            "type": "announce",
            "hash_id": "a" * 32,
            "name": "spoof",
            "transport": "tcp",
            "public_key": "bb" * 32,
        }
    )
    peer, is_new = reg.upsert_from_announce("10.0.0.9:1111", "tcp", unsigned)
    assert peer is None
    assert is_new is False


def test_discovery_rejects_bad_signature() -> None:
    identity = Identity.generate("bob", "tcp")
    payload = sign_payload(
        {
            "type": "announce",
            "hash_id": identity.hash_id,
            "name": identity.name,
            "public_key": identity.public_bytes().hex(),
            "transport": "tcp",
            "tcp_host": "10.0.0.2",
            "tcp_port": 7825,
        },
        identity.private_key,
    )
    data = decode_payload(payload)
    data["signature"] = "00" * 64
    peer, is_new = DiscoveryRegistry().upsert_from_announce(
        "10.0.0.2:2222", "tcp", encode_payload(data)
    )
    assert peer is None
    assert is_new is False


def test_discovery_accepts_valid_signed_announce() -> None:
    identity = Identity.generate("carol", "tcp")
    payload = sign_payload(
        {
            "type": "announce",
            "hash_id": identity.hash_id,
            "name": identity.name,
            "public_key": identity.public_bytes().hex(),
            "transport": "tcp",
            "tcp_host": "10.0.0.3",
            "tcp_port": 7825,
        },
        identity.private_key,
    )
    peer, is_new = DiscoveryRegistry().upsert_from_announce(
        "10.0.0.3:3333", "tcp", payload
    )
    assert is_new
    assert peer is not None
    assert peer.hash_id == identity.hash_id
    assert peer.name == "carol"