"""Group chats: three real nodes over loopback TCP."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from srltcp.core.messaging.groups import GROUP_MSG_TYPE, GroupError, _signed_fields
from srltcp.core.protocol.crypto import sign_bytes
from srltcp.core.protocol.messages import MessageType
from tests.test_e2e_nodes import make_node, trust, wait_for


def group_texts(node, gid: str) -> list[str]:  # type: ignore[no-untyped-def]
    return [
        m["text"] for m in node.get_messages(limit=1000) if m["metadata"].get("group_id") == gid
    ]


def h(node) -> str:  # type: ignore[no-untyped-def]
    return node.identities["tcp"].hash_id


@pytest.fixture
async def trio(tmp_path: Path):  # type: ignore[no-untyped-def]
    """Alice knows Bob and Carol; Bob and Carol are not each other's contacts."""
    a, b, c = (make_node(tmp_path, n) for n in ("alice", "bob", "carol"))
    for n in (a, b, c):
        await n.start()
    trust(a, b)
    trust(b, a)
    trust(a, c)
    trust(c, a)
    yield a, b, c
    for n in (a, b, c):
        await n.stop()


async def test_group_messages_reach_members_who_are_not_contacts(trio) -> None:  # type: ignore[no-untyped-def]
    a, b, c = trio
    group = await a.create_group("Weekend", [h(b), h(c)])
    gid = group["id"]
    assert await wait_for(lambda: b.groups.get(gid) is not None and c.groups.get(gid) is not None)
    assert {m["hash_id"] for m in b.get_group_view(gid)["members"]} == {h(a), h(b), h(c)}

    await b.send_group_message(gid, "hi from bob")
    assert await wait_for(lambda: "hi from bob" in group_texts(c, gid))  # forwarded by alice
    assert "hi from bob" in group_texts(a, gid)
    await c.send_group_message(gid, "carol here")
    assert await wait_for(lambda: "carol here" in group_texts(b, gid))
    await a.send_group_message(gid, "alice too")
    assert await wait_for(
        lambda: "alice too" in group_texts(b, gid) and "alice too" in group_texts(c, gid)
    )
    # No duplicates, author attributed correctly, sender sees delivered.
    assert group_texts(c, gid).count("hi from bob") == 1
    msg = next(m for m in c.get_messages() if m["text"] == "hi from bob")
    assert msg["sender_hash"] == h(b) and msg["msg_type"] == GROUP_MSG_TYPE
    assert await wait_for(
        lambda: (
            next(m for m in a.get_messages() if m["text"] == "alice too")["status"] == "delivered"
        )
    )
    # Group messages never show up in one-to-one history logic
    assert h(b) not in a.peers_with_undelivered()


async def test_owner_cannot_forge_and_strangers_are_ignored(trio) -> None:  # type: ignore[no-untyped-def]
    a, b, c = trio
    gid = (await a.create_group("G", [h(b), h(c)]))["id"]
    assert await wait_for(lambda: c.groups.get(gid) is not None)
    # Alice forwards a message claiming to be from Bob, signed with her own key.
    sig = sign_bytes(
        a.identities["tcp"].private_key, _signed_fields(gid, "m1", h(b), "fake", 1.0)
    ).hex()
    await a.connect_to_peer(h(c))
    await a._send_group_packet(
        h(c),
        MessageType.GROUP_TEXT,
        {"g": gid, "i": "m1", "s": h(b), "t": "fake", "ts": 1.0, "sig": sig},
    )
    await asyncio.sleep(0.5)
    assert "fake" not in group_texts(c, gid)
    # Bob may not change Alice's group.
    await b.connect_to_peer(h(a))
    fake = dict(b.groups.get(gid))  # type: ignore[arg-type]
    fake["version"] = 99
    fake["name"] = "hijacked"
    await b._send_group_packet(h(a), MessageType.GROUP_UPDATE, {"group": fake})
    await asyncio.sleep(0.5)
    assert a.groups.get(gid)["name"] == "G"  # type: ignore[index]


async def test_rename_remove_leave_and_close(trio) -> None:  # type: ignore[no-untyped-def]
    a, b, c = trio
    gid = (await a.create_group("Old", [h(b), h(c)]))["id"]
    assert await wait_for(lambda: c.groups.get(gid) is not None)
    await a.update_group(gid, name="New", remove=[h(c)])
    assert await wait_for(lambda: b.groups.get(gid)["name"] == "New")  # type: ignore[index]
    assert await wait_for(lambda: c.groups.get(gid).get("left") is True)  # type: ignore[union-attr]
    with pytest.raises(GroupError):
        await c.send_group_message(gid, "still here?")
    with pytest.raises(GroupError):
        await b.update_group(gid, name="mine")
    await b.leave_group(gid)
    assert await wait_for(lambda: h(b) not in [m["hash_id"] for m in a.groups.get(gid)["members"]])  # type: ignore[index]
    await a.leave_group(gid)  # creator leaving closes the group
    assert a.groups.get(gid)["left"] is True  # type: ignore[index]
    assert await a.delete_group(gid)
    assert a.groups.get(gid) is None


async def test_member_offline_gets_messages_and_updates_later(tmp_path: Path) -> None:
    a, b, c = (make_node(tmp_path, n) for n in ("alice", "bob", "carol"))
    for n in (a, b, c):
        await n.start()
    try:
        trust(a, b), trust(b, a), trust(a, c), trust(c, a)
        gid = (await a.create_group("G", [h(b), h(c)]))["id"]
        assert await wait_for(lambda: c.groups.get(gid) is not None)
        carol_port = c.config.tcp_port
        await c.stop()
        await b.send_group_message(gid, "while carol was away")
        await a.update_group(gid, name="Renamed")
        assert await wait_for(lambda: "while carol was away" in group_texts(a, gid))
        c2 = make_node(tmp_path, "carol", tcp_port=carol_port)
        await c2.start()
        try:
            await a.connect_to_peer(h(c2))
            assert await wait_for(
                lambda: "while carol was away" in group_texts(c2, gid), timeout=15
            )
            assert await wait_for(lambda: c2.groups.get(gid)["name"] == "Renamed")  # type: ignore[index]
        finally:
            await c2.stop()
    finally:
        await a.stop()
        await b.stop()


async def test_invite_from_non_contact_is_ignored(tmp_path: Path) -> None:
    a, b = make_node(tmp_path, "alice"), make_node(tmp_path, "bob")
    await a.start()
    await b.start()
    try:
        trust(a, b)  # bob does not trust alice
        gid = (await a.create_group("Spam", [h(b)]))["id"]
        await asyncio.sleep(1.0)
        assert b.groups.get(gid) is None
    finally:
        await a.stop()
        await b.stop()
