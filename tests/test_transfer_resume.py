"""A file transfer cut off by a dropped connection resumes after reconnecting."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

import srltcp.core.messaging.transfer as tr
from tests.test_e2e_nodes import make_node, trust, wait_for


async def test_transfer_resumes_after_link_drop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tr, "CHUNK_SIZE", 32 * 1024)
    monkeypatch.setattr(tr, "CHUNK_SEND_DELAY", 0.05)
    a, b = make_node(tmp_path, "alice"), make_node(tmp_path, "bob")
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
