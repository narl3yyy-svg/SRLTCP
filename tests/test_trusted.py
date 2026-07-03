"""Trusted peer store tests."""

from __future__ import annotations

from pathlib import Path

from srltcp.core.trusted import TrustedPeer, TrustedStore


def test_trusted_add_remove(tmp_path: Path) -> None:
    store = TrustedStore(path=tmp_path / "trusted.json")
    peer = TrustedPeer(hash_id="abc" * 10 + "ab", name="alice", transport="tcp")
    store.add(peer)
    assert store.is_trusted(peer.hash_id)
    assert len(store.list_peers()) == 1
    assert store.remove(peer.hash_id)
    assert not store.is_trusted(peer.hash_id)


def test_trusted_blocked_not_trusted(tmp_path: Path) -> None:
    store = TrustedStore(path=tmp_path / "trusted.json")
    peer = TrustedPeer(hash_id="def" * 10 + "de", name="bob", transport="tcp", blocked=True)
    store.add(peer)
    assert peer.hash_id in store._peers
    assert not store.is_trusted(peer.hash_id)


def test_trusted_update_rename_and_block(tmp_path: Path) -> None:
    store = TrustedStore(path=tmp_path / "trusted.json")
    peer = TrustedPeer(hash_id="ghi" * 10 + "gh", name="carol", transport="serial")
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
    peer = TrustedPeer(hash_id="jkl" * 10 + "jk", name="dave", transport="tcp")
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