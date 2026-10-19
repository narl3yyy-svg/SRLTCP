"""Attacks found in the 0.13.8 security review, each shown not to work."""

from __future__ import annotations

import asyncio
import os
import time
import zlib
from pathlib import Path
from typing import Any

import srltcp.core  # noqa: F401  (import order: core before transports)
from srltcp.core.protocol import beacon
from srltcp.core.protocol.crypto import KeyExchange
from srltcp.core.protocol.messages import MessageType, build_header, encode_payload
from srltcp.transports.serial_link import (
    ACK_HDR,
    ACK_RATE,
    CRC,
    K_ACK,
    K_DATA_LAST,
    MAGIC,
    SerialLink,
)
from tests.test_e2e_nodes import make_node, wait_for
from tests.test_path_switch import radio_pair  # noqa: F401  (fixture)
from tests.test_radio import _on_radio
from tests.test_serial_link import _pair, _until
from tests.test_transfer_engine import pair  # noqa: F401  (fixture)

# ── The radio: anyone in range can transmit ──


async def test_a_stranger_on_the_radio_cant_replace_a_contacts_session(
    radio_pair: Any, tmp_path: Path  # noqa: F811
) -> None:
    """Everyone in range shares the one radio "connection": a validly signed
    handshake from a throwaway identity used to tear down the contact's session."""
    a, b, _bridge = radio_pair
    eve = make_node(tmp_path / "eve", "eve")  # (an attacker running the app)
    await eve.start()
    ah = a.identities["tcp"].hash_id
    kx = KeyExchange(eve.identities["tcp"].private_key, eve.identities["tcp"].pq)
    kx.start_kem()
    packet = eve._identity_hello(MessageType.HANDSHAKE, kx, "tcp", ah)
    await eve.stop()
    assert a.serial_transport is not None
    peer = a.serial_transport.peers()[0]
    await a._on_transport_frame(peer, packet)
    await asyncio.sleep(1)
    assert _on_radio(a, b) and _on_radio(b, a)
    assert a.get_link(eve.identities["tcp"].hash_id) is None


async def test_a_replayed_beacon_doesnt_drop_the_radio_session(
    radio_pair: Any, monkeypatch: Any  # noqa: F811
) -> None:
    from srltcp.core.messaging import radio

    monkeypatch.setattr(radio, "RADIO_SESSION_SETTLE", 0.0)
    a, b, _bridge = radio_pair
    ah = a.identities["tcp"].hash_id
    key = b._radio_key(ah)
    assert key is not None
    old = beacon.build([key], now=time.time() - 60)  # recorded before the session
    link = b.get_link(ah)
    await b._handle_radio_beacon(old)
    assert b.get_link(ah) is link and _on_radio(b, a)
    b._radio_seen.clear()  # (junk beacons pushed it out of the replay list)
    await b._handle_radio_beacon(old)
    assert b.get_link(ah) is link and _on_radio(b, a)


async def test_forged_pings_answers_change_nothing(pair: Any) -> None:  # noqa: F811
    """Pings travel encrypted: a plaintext answer, even echoing a ping's
    contents, neither keeps a connection alive nor sets the ping time."""
    a, b = pair
    bh = b.identities["tcp"].hash_id
    link = a.get_link(bh)
    await a.ping_peer(bh)
    nonce = next(iter(a._pending_pings[bh]))
    seen, rtt = link.last_seen, link.rtt_ms
    forged = build_header(MessageType.PONG, body=encode_payload({"n": nonce}))
    assert a.tcp_transport is not None
    peer = next(p for p in a.tcp_transport.peers() if p.peer_id == link.transport_peer_id)
    await a._on_transport_frame(peer, forged)
    assert link.last_seen == seen and link.rtt_ms == rtt
    assert nonce in a._pending_pings[bh]


# ── Files ──


async def test_deleting_a_cancelled_file_never_deletes_another_file(
    pair: Any, tmp_path: Path  # noqa: F811
) -> None:
    """A cancelled offer kept the name its file would have had; a later file
    with that name landed there, and deleting the dead bubble deleted it."""
    a, b = pair
    b.config.auto_download_mb = 0  # (the offer waits: nothing is written)
    src = tmp_path / "report.pdf"
    src.write_bytes(os.urandom(4096))
    tid = (await a.send_file(b.identities["tcp"].hash_id, src))["id"]
    assert await wait_for(lambda: tid in b._transfers, timeout=5)
    assert await b.cancel_transfer(tid)
    victim = b._transfers[tid].path  # the name it would have had
    victim.write_bytes(b"someone else's report")  # (another contact's file, the user's own)
    assert b.transfer_file_path(tid) is None  # not served under the dead bubble either
    assert await b.delete_chat(peer=a.identities["tcp"].hash_id, delete_files=True) == 1
    assert victim.read_bytes() == b"someone else's report"


async def test_a_cancelled_waiting_file_cant_be_restarted(pair: Any, tmp_path: Path) -> None:  # noqa: F811
    a, b = pair
    b.config.auto_download_mb = 0
    src = tmp_path / "big.bin"
    src.write_bytes(os.urandom(4096))
    tid = (await a.send_file(b.identities["tcp"].hash_id, src))["id"]
    assert await wait_for(lambda: tid in b._transfers, timeout=5)
    assert await b.cancel_transfer(tid)
    assert not await b.download_transfer(tid)
    assert b._transfers[tid].state == "cancelled"


# ── The radio link layer ──


def _ack(msg_id: int, count: int, have: set[int]) -> bytes:
    bitmap = bytearray((count + 7) // 8)
    for i in have:
        bitmap[i // 8] |= 1 << (i % 8)
    head = (ACK_HDR.pack(MAGIC, K_ACK, msg_id, count, len(bitmap)) + bytes(bitmap)
            + ACK_RATE.pack(0))
    return head + CRC.pack(zlib.crc32(head))


async def test_a_forged_ack_with_another_part_count_is_ignored() -> None:
    a, _b, got = await _pair(loss=0.0, corrupt=0.0)
    msg_id = a._next_id
    a._acked[msg_id] = set()
    a._sent_count[msg_id] = 5
    a._ack_events[msg_id] = asyncio.Event()
    await a.feed(_ack(msg_id, 1, {0}))  # "all one part of it arrived"
    assert a._acked[msg_id] == set()
    await a.feed(_ack(msg_id, 5, {0, 1}))
    assert a._acked[msg_id] == {0, 1}


async def test_a_forged_packet_for_the_next_id_doesnt_swallow_the_real_message() -> None:
    """Message ids are visible on the air: a one-packet message sent ahead
    under the next id used to make the real one count as a duplicate."""
    got: list[bytes] = []

    async def deliver(msg: bytes) -> None:
        got.append(msg)

    async def write(_data: bytes) -> None:
        return None

    link = SerialLink(write, deliver, 57600)
    await link._on_data(K_DATA_LAST, 4242, 0, 1, b"forged")
    await link._on_data(K_DATA_LAST, 4242, 0, 1, b"the real one")
    assert got == [b"forged", b"the real one"]
    await link._on_data(K_DATA_LAST, 4242, 0, 1, b"the real one")  # (a resend)
    assert got == [b"forged", b"the real one"]


async def test_messages_still_cross_a_lossy_radio() -> None:
    a, b, got = await _pair(loss=0.05, corrupt=0.02)
    for i in range(5):
        await a.send(f"m{i}".encode() * 30)
    assert await _until(lambda: len(got[1]) == 5, timeout=60)
    await a.stop()
    await b.stop()
