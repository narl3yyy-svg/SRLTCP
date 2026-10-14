"""A file transfer cut off by a dropped connection resumes after reconnecting."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

import srltcp.core.messaging.transfer as tr
from srltcp.utils import vault
from tests.test_e2e_nodes import make_node, trust, wait_for


async def test_transfer_resumes_after_link_drop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = make_node(tmp_path, "alice"), make_node(tmp_path, "bob")
    # Small, paced chunks so the link can be cut mid-transfer.
    slow = tr.PathParams(chunk=32 * 1024, window=256 * 1024, stall=12.0, level=0, delay=0.05)
    monkeypatch.setattr(a, "_path_params", lambda link: slow)
    await a.start()
    await b.start()
    trust(a, b)
    trust(b, a)
    try:
        bh = b.identities["tcp"].hash_id
        src = tmp_path / "f.bin"
        payload = os.urandom(2 * 1024 * 1024)
        src.write_bytes(payload)
        tid = (await a.send_file(bh, src))["id"]
        await asyncio.sleep(0.6)
        link = a.get_link(bh)
        assert link is not None
        await a.tcp_transport.disconnect(link.transport_peer_id)
        assert b._transfers[tid].offset < len(payload)
        assert await wait_for(lambda: b._transfers[tid].state == "complete", timeout=60)
        assert b._incoming_paths[tid].read_bytes() == payload
        assert await wait_for(lambda: not a.has_active_transfer_for(bh), timeout=10)
    finally:
        await a.stop()
        await b.stop()


SLOW = tr.PathParams(chunk=32 * 1024, window=256 * 1024, stall=12.0, level=0, delay=0.05)


async def _start_pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **ports: int):  # type: ignore[no-untyped-def]
    a = make_node(tmp_path, "alice", tcp_port=ports.get("a", 0))
    b = make_node(tmp_path, "bob", tcp_port=ports.get("b", 0))
    monkeypatch.setattr(a, "_path_params", lambda link: SLOW)
    await a.start()
    await b.start()
    trust(a, b)
    trust(b, a)
    return a, b


async def _send_halfway(a, b, tmp_path: Path) -> tuple[str, bytes]:  # type: ignore[no-untyped-def]
    bh = b.identities["tcp"].hash_id
    src = tmp_path / "f.bin"
    payload = os.urandom(3 * 1024 * 1024)
    src.write_bytes(payload)
    tid = (await a.send_file(bh, src))["id"]
    assert await wait_for(lambda: b._transfers.get(tid) and b._transfers[tid].offset > 256 * 1024)
    return tid, payload


async def test_receiver_restart_resumes_from_its_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = await _start_pair(tmp_path, monkeypatch)
    b2 = None
    try:
        tid, payload = await _send_halfway(a, b, tmp_path)
        port = b.config.tcp_port
        await b.stop()
        had = b._transfers[tid].offset
        assert 0 < had < len(payload)
        b2 = make_node(tmp_path, "bob", tcp_port=port)
        await b2.start()
        restored = b2._transfers[tid]
        assert restored.state == "paused" and restored.offset >= had
        msg = next(m for m in b2.get_messages() if m["metadata"].get("transfer_id") == tid)
        assert msg["metadata"]["state"] == "paused"  # not "failed" as before
        await b2.connect_to_peer(a.identities["tcp"].hash_id)
        assert await wait_for(lambda: b2._transfers[tid].state == "complete", timeout=60)
        assert b2._incoming_paths[tid].read_bytes() == payload
        assert await wait_for(lambda: a._transfers[tid].state == "complete", timeout=10)
    finally:
        await a.stop()
        if b2:
            await b2.stop()


async def test_sender_restart_resumes_and_hashes_what_was_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = await _start_pair(tmp_path, monkeypatch)
    a2 = None
    try:
        tid, payload = await _send_halfway(a, b, tmp_path)
        port = a.config.tcp_port
        await a.stop()
        a2 = make_node(tmp_path, "alice", tcp_port=port)
        await a2.start()
        assert a2._transfers[tid].state == "paused"
        await a2.connect_to_peer(b.identities["tcp"].hash_id)
        assert await wait_for(lambda: b._transfers[tid].state == "complete", timeout=60)
        assert b._incoming_paths[tid].read_bytes() == payload
        assert await wait_for(lambda: a2._transfers[tid].state == "complete", timeout=10)
        # Finished: nothing is left to resume after the next restart.
        a2.save_transfer_journal()
        assert '"transfers":[]' in vault.read_text(tmp_path / "alice" / "transfers.json")
    finally:
        if a2:
            await a2.stop()
        await b.stop()


async def test_file_changed_during_restart_fails_on_both_sides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = await _start_pair(tmp_path, monkeypatch)
    a2 = None
    try:
        tid, _payload = await _send_halfway(a, b, tmp_path)
        port = a.config.tcp_port
        await a.stop()
        (tmp_path / "f.bin").write_bytes(os.urandom(3 * 1024 * 1024))  # edited meanwhile
        a2 = make_node(tmp_path, "alice", tcp_port=port)
        await a2.start()
        assert a2._transfers[tid].state == "failed"
        await a2.connect_to_peer(b.identities["tcp"].hash_id)
        assert await wait_for(lambda: b._transfers[tid].state == "failed", timeout=15)
        assert not b._incoming_paths[tid].exists()  # partial download removed
    finally:
        if a2:
            await a2.stop()
        await b.stop()


async def test_upload_copies_of_unfinished_transfers_survive_the_restart_purge(
    tmp_path: Path,
) -> None:
    a = make_node(tmp_path, "alice")
    uploads = tmp_path / "alice" / "uploads"
    await a.start()
    try:
        uploads.mkdir(parents=True, exist_ok=True)
        pending, old = uploads / "pending.bin", uploads / "old.bin"  # picked in the browser
        pending.write_bytes(b"x" * 10)
        old.write_bytes(b"y")
        transfer = await a.queue_file("b" * 32, pending)
        a.save_transfer_journal()
    finally:
        await a.stop()
    a2 = make_node(tmp_path, "alice")
    await a2.start()
    try:
        assert pending.exists() and not old.exists()
        assert a2._transfers[transfer.id].state == "paused"
    finally:
        await a2.stop()
