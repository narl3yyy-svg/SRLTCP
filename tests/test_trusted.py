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