"""Disappearing messages: deleted from both phones when the sender's timer runs out."""

from __future__ import annotations

import os
import time
from pathlib import Path

from srltcp.core.messaging.disappear import MAX_TTL, wire_ttl
from tests.test_e2e_nodes import make_node, trust, wait_for


def test_timer_from_a_contact_is_bounded() -> None:
    assert wire_ttl({"ttl": 3600}) == 3600
    for bad in (0, 5, MAX_TTL + 1, -60, "3600", True, None, 3600.5):
        assert wire_ttl({"ttl": bad}) == 0
    assert wire_ttl({}) == 0


def _run_out(node, ids: set[str]) -> None:  # type: ignore[no-untyped-def]
    """Pretend the timers ran out."""
    for m in node._messages:
        if m.id in ids or m.metadata.get("transfer_id") in ids:
            m.metadata["expires_at"] = time.time() - 1


async def test_messages_and_files_disappear_on_both_phones(tmp_path: Path) -> None:
    a = make_node(tmp_path, "alice", disappear_seconds=3600)
    b = make_node(tmp_path, "bob")  # Bob keeps his own messages
    await a.start()
    await b.start()
    events: list[dict] = []

    async def on_event(evt: dict) -> None:
        events.append(evt)

    b.set_callbacks(on_event=on_event)
    try:
        trust(a, b)
        trust(b, a)
        ah, bh = a.identities["tcp"].hash_id, b.identities["tcp"].hash_id
        msg = await a.send_message(bh, "self-destruct")
        assert msg is not None and msg.metadata["ttl"] == 3600
        assert await wait_for(lambda: any(m["text"] == "self-destruct" for m in b.get_messages()))
        got = next(m for m in b.get_messages() if m["text"] == "self-destruct")
        assert 3500 < got["metadata"]["expires_at"] - time.time() <= 3600
        reply = await b.send_message(ah, "I keep mine")
        assert reply is not None and "ttl" not in reply.metadata

        src = tmp_path / "photo.jpg"
        src.write_bytes(os.urandom(200_000))
        tid = (await a.send_file(bh, src))["id"]
        assert await wait_for(
            lambda: b._transfers.get(tid) and b._transfers[tid].state == "complete"
        )
        received = b._incoming_paths[tid]
        assert received.exists() and b._transfers[tid].metadata["ttl"] == 3600

        for node in (a, b):
            _run_out(node, {msg.id, tid})
            assert await node.expire_messages() == 2
        assert not any(m["text"] == "self-destruct" for m in a.get_messages() + b.get_messages())
        assert not received.exists()  # the received copy is gone too
        assert src.exists()  # the sender's own file is theirs to keep
        assert any(m["text"] == "I keep mine" for m in a.get_messages())
        assert any(e.get("kind") == "messages_removed" and msg.id in e["ids"] for e in events)
    finally:
        await a.stop()
        await b.stop()


async def test_expired_messages_are_gone_after_a_restart(tmp_path: Path) -> None:
    a, b = make_node(tmp_path, "alice", disappear_seconds=3600), make_node(tmp_path, "bob")
    await a.start()
    await b.start()
    try:
        trust(a, b)
        trust(b, a)
        msg = await a.send_message(b.identities["tcp"].hash_id, "brief")
        assert msg is not None
        _run_out(a, {msg.id})
        a.save_message_history()
    finally:
        await a.stop()
        await b.stop()
    again = make_node(tmp_path, "alice")
    await again.start()
    try:
        assert not any(m["text"] == "brief" for m in again.get_messages())
    finally:
        await again.stop()


async def test_received_disappearing_file_is_deleted_after_a_restart(tmp_path: Path) -> None:
    a = make_node(tmp_path, "alice", disappear_seconds=3600)
    b = make_node(tmp_path, "bob")
    await a.start()
    await b.start()
    try:
        trust(a, b)
        trust(b, a)
        src = tmp_path / "photo.jpg"
        src.write_bytes(os.urandom(50_000))
        tid = (await a.send_file(b.identities["tcp"].hash_id, src))["id"]
        assert await wait_for(
            lambda: b._transfers.get(tid) and b._transfers[tid].state == "complete"
        )
        received = b._incoming_paths[tid]
        _run_out(b, {tid})
        b.save_message_history()
        b.save_transfer_journal()
    finally:
        await a.stop()
        await b.stop()
    assert received.exists()
    again = make_node(tmp_path, "bob")
    await again.start()  # the timer ran out while the app was closed
    try:
        assert not received.exists()
        assert tid not in again._transfers
    finally:
        await again.stop()
