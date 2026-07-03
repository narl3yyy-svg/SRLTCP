"""Trusted peer store tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from srltcp.core.trusted import TrustedPeer, TrustedStore, is_valid_hash_id


def _hash(seed: str) -> str:
    return (seed * 32)[:32]


def test_trusted_add_remove(tmp_path: Path) -> None:
    store = TrustedStore(path=tmp_path / "trusted.json")
    peer = TrustedPeer(hash_id=_hash("a"), name="alice", transport="tcp")
    store.add(peer)
    assert store.is_trusted(peer.hash_id)
    assert len(store.list_peers()) == 1
    assert store.remove(peer.hash_id)
    assert not store.is_trusted(peer.hash_id)


def test_trusted_blocked_not_trusted(tmp_path: Path) -> None:
    store = TrustedStore(path=tmp_path / "trusted.json")
    peer = TrustedPeer(hash_id=_hash("b"), name="bob", transport="tcp", blocked=True)
    store.add(peer)
    assert peer.hash_id in store._peers
    assert not store.is_trusted(peer.hash_id)


def test_trusted_update_rename_and_block(tmp_path: Path) -> None:
    store = TrustedStore(path=tmp_path / "trusted.json")
    peer = TrustedPeer(hash_id=_hash("c"), name="carol", transport="serial")
    store.add(peer)
    updated = store.update(peer.hash_id, name="Carol S", blocked=True)
    assert updated is not None
    assert updated.name == "Carol S"
    assert updated.blocked is True
    assert not store.is_trusted(peer.hash_id)
    store.update(peer.hash_id, blocked=False)
    assert store.is_trusted(peer.hash_id)


def test_trusted_update_wan_fields(tmp_path: Path) -> None:
    store = TrustedStore(path=tmp_path / "trusted.json")
    peer = TrustedPeer(hash_id=_hash("d"), name="dave", transport="tcp")
    store.add(peer)
    updated = store.update(
        peer.hash_id,
        wan_host="8.8.8.8",
        wan_port=9000,
        wan_enabled=True,
        connection_mode="wan",
    )
    assert updated is not None
    assert updated.wan_host == "8.8.8.8"
    assert updated.wan_port == 9000
    assert updated.wan_enabled is True
    assert updated.connection_mode == "wan"


def test_trusted_rejects_invalid_hash(tmp_path: Path) -> None:
    store = TrustedStore(path=tmp_path / "trusted.json")
    with pytest.raises(ValueError):
        store.add(TrustedPeer(hash_id="not-a-valid-hash", name="bad"))


def test_trusted_load_skips_invalid_entries(tmp_path: Path) -> None:
    path = tmp_path / "trusted.json"
    path.write_text(
        '{"peers": ['
        '{"hash_id": "deadbeef", "name": "bad"},'
        f'{{"hash_id": "{_hash("e")}", "name": "good"}}'
        "]}",
        encoding="utf-8",
    )
    store = TrustedStore(path=path)
    assert len(store.list_peers()) == 1
    assert store.list_peers()[0].name == "good"


def test_is_valid_hash_id() -> None:
    assert is_valid_hash_id("a" * 32)
    assert not is_valid_hash_id("short")
    assert not is_valid_hash_id("g" * 32)
    assert not is_valid_hash_id("a" * 64)


def test_trusted_manual_add_with_host(tmp_path: Path) -> None:
    store = TrustedStore(path=tmp_path / "trusted.json")
    peer = TrustedPeer(
        hash_id=_hash("f"),
        name="remote-peer",
        transport="tcp",
        tcp_host="10.0.0.9",
        tcp_port=7825,
    )
    store.add(peer)
    saved = store.get(peer.hash_id)
    assert saved is not None
    assert saved.tcp_host == "10.0.0.9"