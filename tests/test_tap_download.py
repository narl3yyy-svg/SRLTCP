"""Large files wait for a tap; deleting a chat; the ping shown; STUN input."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from srltcp.core.messaging.backend import MessagingBackend, NodeConfig
from srltcp.core.messaging.links import PeerLink
from srltcp.core.messaging.models import ChatMessage
from tests.test_e2e_nodes import wait_for
from tests.test_transfer_engine import pair  # noqa: F401  (fixture)

MiB = 1024 * 1024


async def test_large_file_waits_for_a_tap(pair, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    a, b = pair
    b.config.auto_download_mb = 0  # ask for every file
    src = tmp_path / "clip.mp4"
    payload = os.urandom(300 * 1024)
    src.write_bytes(payload)
    ah, bh = a.identities["tcp"].hash_id, b.identities["tcp"].hash_id
    tid = (await a.send_file(bh, src))["id"]
    assert await wait_for(lambda: a._transfers[tid].metadata.get("waiting"), timeout=5)
    rx = b._transfers[tid]
    assert rx.state == "offered" and rx.metadata.get("ask")
    assert tid not in b._in and not list((tmp_path / "bob").rglob("clip.mp4*"))
    # Waiting is not a transfer in progress: pings, reconnects go on as usual.
    assert not a.has_active_transfer_for(bh) and not b.has_active_transfer_for(ah)
    msg = next(m for m in b.get_messages() if m["metadata"].get("transfer_id") == tid)
    assert msg["metadata"]["ask"] is True
    # Nothing is offered again or paused meanwhile.
    await a._check_stale_offers()
    assert a._transfers[tid].state == "offered"

    assert await b.download_transfer(tid)
    assert await wait_for(lambda: b._transfers[tid].state == "complete", timeout=10)
    assert b._transfers[tid].path.read_bytes() == payload
    assert await wait_for(lambda: a._transfers[tid].state == "complete", timeout=5)
    assert not a._transfers[tid].metadata.get("waiting")
    msg = next(m for m in b.get_messages() if m["metadata"].get("transfer_id") == tid)
    assert msg["metadata"]["ask"] is False
    assert not await b.download_transfer(tid)  # (once)


async def test_declining_a_waiting_file_tells_the_sender(pair, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    a, b = pair
    b.config.auto_download_mb = 0
    src = tmp_path / "big.bin"
    src.write_bytes(os.urandom(64 * 1024))
    tid = (await a.send_file(b.identities["tcp"].hash_id, src))["id"]
    assert await wait_for(lambda: b._transfers.get(tid) is not None
                          and b._transfers[tid].metadata.get("ask"), timeout=5)
    assert await b.cancel_transfer(tid)
    assert await wait_for(lambda: a._transfers[tid].state == "cancelled", timeout=5)


def test_the_radio_has_its_own_limit() -> None:
    backend = MessagingBackend(NodeConfig(auto_download_mb=2048, auto_download_radio_mb=2))
    assert backend.auto_download_limit("tcp") == 2048 * MiB
    assert backend.auto_download_limit("serial") == 2 * MiB
    backend.config.auto_download_mb = 0
    assert backend.auto_download_limit("serial") == 0


async def test_delete_chat_keeps_other_chats_and_groups(tmp_path: Path) -> None:
    backend = MessagingBackend(NodeConfig(incoming_dir=str(tmp_path)))
    me, peer, other = "0" * 32, "1" * 32, "2" * 32
    backend._messages = [
        ChatMessage.create(peer, me, "hi", "tcp"),
        ChatMessage.create(me, peer, "hello", "tcp"),
        ChatMessage.create(other, me, "not this chat", "tcp"),
        ChatMessage.create(peer, me, "in a group", "tcp", metadata={"group_id": "g" * 32}),
    ]
    removed: list[list[str]] = []

    async def on_event(event: dict) -> None:  # type: ignore[type-arg]
        removed.append(event["ids"])

    backend._on_event = on_event
    assert await backend.delete_chat(peer=peer) == 2
    assert [m.text for m in backend._messages] == ["not this chat", "in a group"]
    assert len(removed) == 1 and len(removed[0]) == 2
    assert await backend.delete_chat(group="g" * 32) == 1
    assert await backend.delete_chat(peer=peer) == 0


async def test_delete_chat_can_remove_received_files(pair, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    a, b = pair
    src = tmp_path / "photo.jpg"
    src.write_bytes(os.urandom(20 * 1024))
    tid = (await a.send_file(b.identities["tcp"].hash_id, src))["id"]
    assert await wait_for(lambda: b._transfers.get(tid) is not None
                          and b._transfers[tid].state == "complete", timeout=10)
    received = b._transfers[tid].path
    assert received.exists()
    assert await b.delete_chat(peer=a.identities["tcp"].hash_id, delete_files=True) == 1
    assert not received.exists() and tid not in b._transfers
    assert src.exists()  # (the sender's own file is never touched)


def test_ping_shown_is_the_median_of_the_last_few() -> None:
    link = PeerLink(hash_id="a" * 32, transport_peer_id="p", transport="tcp", address="",
                    public_key=b"")
    for rtt in (300, 310, 900, 305):  # one ping waited behind other traffic
        link.add_rtt(rtt)
    assert link.rtt_ms == pytest.approx(307.5)
    for rtt in (400, 410, 420, 430, 440):
        link.add_rtt(rtt)
    assert link.rtt_ms == 420 and len(link.rtt_samples) == 5


@pytest.mark.parametrize("text", ["8.8.8.8", "1.1.1.1:53", "stun.example.org:x"])
def test_stun_field_rejects_dns_servers_and_junk(text: str) -> None:
    from srltcp.transports.udp import parse_stun_servers

    with pytest.raises(ValueError):
        parse_stun_servers(text)
    assert parse_stun_servers("stun.nextcloud.com:443") == (("stun.nextcloud.com", 443),)


async def test_deleting_messages_takes_their_received_files(pair, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    a, b = pair
    src = tmp_path / "clip.mp4"
    src.write_bytes(os.urandom(10 * 1024))
    tid = (await a.send_file(b.identities["tcp"].hash_id, src))["id"]
    await a.send_message(b.identities["tcp"].hash_id, "keep me")
    assert await wait_for(lambda: tid in b._transfers and b._transfers[tid].state == "complete"
                          and any(m["text"] == "keep me" for m in b.get_messages()), timeout=10)
    received = b._transfers[tid].path
    file_msg = next(m for m in b.get_messages() if m["metadata"].get("transfer_id") == tid)
    ah = a.identities["tcp"].hash_id
    assert await b.delete_messages([file_msg["id"]], peer="0" * 32) == 0  # (another chat's)
    assert await b.delete_messages([file_msg["id"], "no-such-id"], peer=ah) == 1
    assert not received.exists() and tid not in b._transfers
    assert [m["text"] for m in b.get_messages()] == ["keep me"]
