"""Files sent to an unreachable contact wait and go out when the contact is
back; an offer that is never answered does not freeze the contact's link."""

from __future__ import annotations

import os
import socket
import time
from pathlib import Path

import pytest

import srltcp.core.messaging.transfer as tr
from srltcp.core.messaging.models import TransferState
from tests.test_e2e_nodes import make_node, trust, wait_for


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def test_file_to_offline_contact_is_queued_then_delivered(tmp_path: Path) -> None:
    a, b = make_node(tmp_path, "alice"), make_node(tmp_path, "bob")
    await a.start()
    await b.start()
    trust(a, b)
    trust(b, a)
    try:
        bh = b.identities["tcp"].hash_id
        real_port = b.config.tcp_port
        # Bob is "on another network": his saved address doesn't answer.
        a.trusted.update(bh, tcp_port=_closed_port(), alt_hosts=[])
        src = tmp_path / "photo.jpg"
        payload = os.urandom(300_000)
        src.write_bytes(payload)
        sent = await a.send_file(bh, src)
        assert sent is not None, "send must queue, not fail"
        assert sent["state"] == "paused"
        tid = sent["id"]
        assert bh in a.peers_with_queued_files()
        assert not a.has_active_transfer_for(bh)  # pings/reconnects keep running
        # Bob is reachable again.
        a.trusted.update(bh, tcp_port=real_port)
        await a.connect_to_peer(bh)
        assert await wait_for(
            lambda: tid in b._transfers and b._transfers[tid].state == "complete", timeout=30
        )
        assert b._incoming_paths[tid].read_bytes() == payload
        assert await wait_for(lambda: a._transfers[tid].state == "complete", timeout=10)
        assert bh not in a.peers_with_queued_files()
    finally:
        await a.stop()
        await b.stop()


async def test_unanswered_offer_is_requeued_and_resent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = make_node(tmp_path, "alice"), make_node(tmp_path, "bob")
    await a.start()
    await b.start()
    trust(a, b)
    trust(b, a)
    try:
        bh = b.identities["tcp"].hash_id
        await a.connect_to_peer(bh)
        assert await a.wait_for_handshake(bh, timeout=10)
        # Bob loses the offer (as when the link dies just after it was sent).
        real = b._handle_file_offer
        dropped: list[bytes] = []

        async def drop(hash_id: str, body: bytes) -> None:
            dropped.append(body)

        monkeypatch.setattr(b, "_handle_file_offer", drop)
        src = tmp_path / "doc.pdf"
        payload = os.urandom(100_000)
        src.write_bytes(payload)
        tid = (await a.send_file(bh, src))["id"]
        assert await wait_for(lambda: bool(dropped), timeout=5)
        assert a.has_active_transfer_for(bh)
        # Offer timed out twice: the file is queued again, the link is not
        # held "busy" any more.
        for _ in range(2):
            sent_at, tries = a._offer_sent_at[tid]
            a._offer_sent_at[tid] = (time.monotonic() - tr.OFFER_TIMEOUT - 1, tries)
            await a._check_stale_offers()
        assert a._transfers[tid].state == TransferState.PAUSED
        assert not a.has_active_transfer_for(bh)
        # Next session: the offer is sent again and the file arrives.
        monkeypatch.setattr(b, "_handle_file_offer", real)
        assert await a.resume_transfer(tid)
        assert await wait_for(
            lambda: tid in b._transfers and b._transfers[tid].state == "complete", timeout=20
        )
        assert b._incoming_paths[tid].read_bytes() == payload
    finally:
        await a.stop()
        await b.stop()


async def test_link_down_requeues_pending_offer(tmp_path: Path) -> None:
    a, b = make_node(tmp_path, "alice"), make_node(tmp_path, "bob")
    await a.start()
    await b.start()
    trust(a, b)
    trust(b, a)
    try:
        bh = b.identities["tcp"].hash_id
        await a.connect_to_peer(bh)
        assert await a.wait_for_handshake(bh, timeout=10)
        src = tmp_path / "x.bin"
        src.write_bytes(os.urandom(1000))
        transfer = await a.queue_file(bh, src)
        transfer.state = TransferState.OFFERED
        await a._pause_outgoing_for_peer(bh)
        assert transfer.state == TransferState.PAUSED
    finally:
        await a.stop()
        await b.stop()
