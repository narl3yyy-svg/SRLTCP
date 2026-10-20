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


def test_a_pasted_tor_address_is_cleaned_up_or_refused() -> None:
    import pytest

    from srltcp.core.messaging.tor import normalize_onion

    good = make_onion()
    bare = good.removesuffix(".onion")
    for pasted in (good, good.upper(), f"  {bare}\n", f"http://{good}/", f"{good}:80"):
        assert normalize_onion(pasted) == good, pasted
    assert normalize_onion("   ") == ""
    for junk in (bare[:-1], good.replace(bare[-1], "1" if bare[-1] != "1" else "2"), "a" * 57,
                 "example.com", f"{bare}.onion.evil"):
        with pytest.raises(ValueError):
            normalize_onion(junk)


async def test_contacts_added_or_edited_by_hand_keep_their_tor_address(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    import srltcp.web.server  # noqa: F401  (import order, as the app loads them)
    from srltcp.core.messaging.backend import NodeConfig
    from srltcp.core.node import SRLTCPNode
    from srltcp.core.settings import AppSettings
    from srltcp.routes.api import register_api_routes

    node = SRLTCPNode(NodeConfig(name="me", incoming_dir=str(tmp_path / "in")), AppSettings())
    app = web.Application()
    register_api_routes(app, node)
    first, second = make_onion(), make_onion()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/trusted", json={"hash_id": "c" * 32, "onion": "nope"})
        assert resp.status == 400 and node.backend.trusted.get("c" * 32) is None
        resp = await client.post("/api/trusted", json={
            "hash_id": "c" * 32, "name": "Edge", "onion": f" {first.upper()} "})
        assert resp.status == 200 and (await resp.json())["onion"] == first
        assert node.backend.peer_onion("c" * 32) == first
        resp = await client.patch(f"/api/trusted/{'c' * 32}", json={"onion": "bad.onion"})
        assert resp.status == 400 and node.backend.peer_onion("c" * 32) == first
        resp = await client.patch(f"/api/trusted/{'c' * 32}",
                                  json={"onion": second.removesuffix(".onion")})
        assert resp.status == 200 and node.backend.peer_onion("c" * 32) == second
        resp = await client.patch(f"/api/trusted/{'c' * 32}", json={"onion": ""})
        assert resp.status == 200 and node.backend.peer_onion("c" * 32) == ""


async def test_new_tor_address_replaces_the_old_one(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import pytest

    from tests.test_e2e_nodes import make_node

    node = make_node(tmp_path, "alice")
    with pytest.raises(RuntimeError):
        await node.new_tor_address()  # (Tor not connected)
    sent: list[str] = []
    ids = iter(["a" * 56, "b" * 56])

    async def command(line: str) -> list[str]:
        sent.append(line)
        if line.startswith("ADD_ONION"):
            return [f"ServiceID={next(ids)}", "PrivateKey=ED25519-V3:secret"]
        return []

    node._tor_control = SimpleNamespace(command=command, connected=True)
    node.tcp_transport = SimpleNamespace(start_onion_listener=AsyncMock(return_value=4321))
    node.addresses_changed = AsyncMock()  # type: ignore[method-assign]
    await node._tor_publish_onion()
    node._tor_state = "ready"
    key = node._tor_dir() / "onion_key"
    assert node._tor_onion == "a" * 56 + ".onion" and key.read_text() == "ED25519-V3:secret"
    sent.clear()
    assert await node.new_tor_address() == "b" * 56 + ".onion"
    assert sent[0].startswith("ADD_ONION NEW:ED25519-V3")  # a fresh key, not the saved one
    assert sent[-1] == "DEL_ONION " + "a" * 56  # the old one goes, after contacts were told
    node.addresses_changed.assert_awaited()
