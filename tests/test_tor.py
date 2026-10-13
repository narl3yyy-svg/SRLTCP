"""Tor transport: address validation and what contacts can make us store."""

from __future__ import annotations

import base64
import hashlib
import os

from srltcp.core.messaging.tor import valid_onion


def make_onion(pubkey: bytes | None = None) -> str:
    pubkey = pubkey or os.urandom(32)
    version = b"\x03"
    checksum = hashlib.sha3_256(b".onion checksum" + pubkey + version).digest()[:2]
    return base64.b32encode(pubkey + checksum + version).decode().lower() + ".onion"


def test_valid_onion_accepts_real_v3_addresses() -> None:
    for _ in range(20):
        assert valid_onion(make_onion())
    assert valid_onion(make_onion().upper())


def test_valid_onion_rejects_junk() -> None:
    good = make_onion()
    bad_checksum = good[:50] + ("a" if good[50] != "a" else "b") + good[51:]
    for addr in (
        "", "example.com", "127.0.0.1", good[:-6], good + ".evil.com", "x" * 56 + ".onion",
        bad_checksum, good.replace(".onion", ".onio"), "facebookcorewwwi.onion",  # v2
    ):
        assert not valid_onion(addr), addr


class _Peer:
    onion = ""


class _Trusted:
    def __init__(self) -> None:
        self.peer = _Peer()

    def get(self, _h: str) -> _Peer:
        return self.peer

    def update(self, _h: str, onion: str) -> None:
        self.peer.onion = onion


def test_contact_can_only_store_a_valid_onion() -> None:
    from srltcp.core.messaging.tor import TorMixin

    node = TorMixin()
    node.trusted = _Trusted()  # type: ignore[attr-defined]
    node.store_peer_onion("a" * 32, {"o": "http://evil/\r\n"})  # type: ignore[arg-type]
    assert node.trusted.peer.onion == ""  # type: ignore[attr-defined]
    good = make_onion()
    node.store_peer_onion("a" * 32, {"o": good})  # type: ignore[arg-type]
    assert node.trusted.peer.onion == good  # type: ignore[attr-defined]
    node.store_peer_onion("a" * 32, {"o": ""})  # type: ignore[arg-type]  # they turned Tor off
    assert node.trusted.peer.onion == ""  # type: ignore[attr-defined]
    node.store_peer_onion("a" * 32, {})  # type: ignore[arg-type]  # older version: unchanged
    assert node.trusted.peer.onion == ""  # type: ignore[attr-defined]
