"""Settings → Privacy → "Only verified contacts can call me or send me files"."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from srltcp.core.protocol.messages import MessageType, encode_payload
from tests.test_e2e_nodes import make_node, trust, wait_for


async def test_unverified_contacts_can_chat_but_not_call_or_send_files(tmp_path: Path) -> None:
    a = make_node(tmp_path, "alice")
    b = make_node(tmp_path, "bob", verified_only=True)  # bob: verified contacts only
    a_events: list[dict[str, Any]] = []
    b_events: list[dict[str, Any]] = []

    async def a_event(e: dict[str, Any]) -> None:
        a_events.append(e)

    async def b_event(e: dict[str, Any]) -> None:
        b_events.append(e)

    await a.start()
    await b.start()
    a._on_event = a_event
    b._on_event = b_event
    trust(a, b)
    trust(b, a)
    try:
        ah, bh = a.identities["tcp"].hash_id, b.identities["tcp"].hash_id
        await a.connect_to_peer(bh)
        assert await a.wait_for_handshake(bh, timeout=8)
        assert not b.contact_verified(ah)

        # Chat still works.
        await a.send_message(bh, "hello, not verified yet")
        assert await wait_for(lambda: any(
            m["text"] == "hello, not verified yet" for m in b.get_messages()))

        # A file is refused, and alice's side says why.
        src = tmp_path / "photo.jpg"
        src.write_bytes(os.urandom(64 * 1024))
        tid = (await a.send_file(bh, src))["id"]
        assert await wait_for(lambda: a._transfers[tid].state == "failed", timeout=10)
        assert a._transfers[tid].metadata.get("reason") == "not_verified"
        assert tid not in b._transfers
        assert await wait_for(lambda: any(
            e.get("kind") == "refused_unverified" and e.get("what") == "a file" for e in b_events))

        # A call is declined with the reason; it never rings on bob's side.
        await a.send_call_signal(bh, {"type": "offer", "call_id": "c1", "video": False, "sdp": "x"})
        assert await wait_for(lambda: any(
            e.get("kind") == "call_signal" and e["signal"].get("reason") == "not_verified"
            for e in a_events))
        assert not any(e.get("kind") == "call_signal" for e in b_events)

        # A folder share offer and a sync invite are refused too.
        link = a.get_link(bh)
        assert link is not None
        for msg_type, body in (
            (MessageType.SHARE_LIST, {"action": "offer", "grant_id": "g1", "label": "Docs"}),
            (MessageType.SYNC, {"op": "invite", "id": "s1", "name": "Docs"}),
        ):
            packet = a._seal(link, msg_type, encode_payload(body))
            await a._send_raw(link.transport_peer_id, link.transport, packet)
        assert await wait_for(lambda: {e.get("what") for e in b_events} >= {
            "a file", "a call", "a shared folder", "a folder sync"})

        # Once verified (numbers compared), files come through.
        b.verify_contact(ah, confirm=True)
        assert b.contact_verified(ah)
        tid2 = (await a.send_file(bh, src))["id"]
        assert await wait_for(lambda: b._transfers.get(tid2) is not None
                              and b._transfers[tid2].state == "complete", timeout=15)
    finally:
        await a.stop()
        await b.stop()


async def test_off_by_default_nothing_is_refused(tmp_path: Path) -> None:
    a, b = make_node(tmp_path, "alice"), make_node(tmp_path, "bob")
    await a.start()
    await b.start()
    trust(a, b)
    trust(b, a)
    try:
        bh = b.identities["tcp"].hash_id
        assert not b.config.verified_only
        src = tmp_path / "f.bin"
        src.write_bytes(b"x" * 1000)
        tid = (await a.send_file(bh, src))["id"]
        assert await wait_for(lambda: b._transfers.get(tid) is not None
                              and b._transfers[tid].state == "complete", timeout=15)
        await asyncio.sleep(0)
    finally:
        await a.stop()
        await b.stop()
