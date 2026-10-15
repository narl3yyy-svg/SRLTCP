"""Windowed transfers: ordering, verification, cancel both ways, recovery, Tor lanes."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import srltcp.core.messaging.transfer as tr
from srltcp.core.messaging.backend import MessagingBackend, NodeConfig
from srltcp.core.messaging.links import PeerLink
from srltcp.core.messaging.models import FileTransfer, TransferState
from srltcp.core.protocol.crypto import CryptoBox, SessionKeys
from srltcp.core.protocol.messages import (
    Flags,
    MessageType,
    build_header,
    encode_payload,
    header_aad,
    pack_file_chunk,
)
from srltcp.transports.base import TransportPeer
from tests.test_e2e_nodes import make_node, trust, wait_for

SENDER = "f" * 32
TID = "aabbccddeeff0101"


def incoming(backend: MessagingBackend, tmp_path: Path, payload: bytes) -> FileTransfer:
    dest = tmp_path / "in.bin"
    transfer = FileTransfer(
        id=TID, sender_hash=SENDER, recipient_hash="0" * 32, filename="in.bin", path=dest,
        size=len(payload), sha256="", transport="tcp", state=TransferState.ACCEPTED,
    )
    backend._transfers[TID] = transfer
    backend._incoming_paths[TID] = dest
    backend._in[TID] = tr._Incoming(path=dest)
    backend._send_control = AsyncMock(return_value=True)  # type: ignore[method-assign]
    return transfer


@pytest.fixture
def backend(tmp_path: Path) -> MessagingBackend:
    b = MessagingBackend(NodeConfig(name="test-node", incoming_dir=str(tmp_path / "inc")))
    b._on_transfer_complete = AsyncMock()
    return b


async def settle(backend: MessagingBackend) -> None:
    rx = backend._in.get(TID)
    for _ in range(100):
        if rx is None or rx.writer is None or rx.writer.done():
            return
        await asyncio.sleep(0.01)


async def test_out_of_order_chunks_are_written_in_order(
    backend: MessagingBackend, tmp_path: Path
) -> None:
    payload = os.urandom(5 * 1000)
    transfer = incoming(backend, tmp_path, payload)
    blocks = [(i * 1000, payload[i * 1000:(i + 1) * 1000]) for i in range(5)]
    for off, data in [blocks[3], blocks[1], blocks[4], blocks[0], blocks[0], blocks[2]]:
        await backend._handle_file_chunk(SENDER, pack_file_chunk(TID, off, data))
    await settle(backend)
    sha = hashlib.sha256(payload).hexdigest()
    await backend._handle_file_complete(
        SENDER, encode_payload({"transfer_id": TID, "sha256": sha})
    )
    await settle(backend)
    assert transfer.state == TransferState.COMPLETE
    assert (tmp_path / "in.bin").read_bytes() == payload
    backend._on_transfer_complete.assert_awaited_once()


async def test_hash_mismatch_fails_and_discards(backend: MessagingBackend, tmp_path: Path) -> None:
    payload = b"hello world!"
    transfer = incoming(backend, tmp_path, payload)
    await backend._handle_file_chunk(SENDER, pack_file_chunk(TID, 0, payload))
    await backend._handle_file_complete(
        SENDER, encode_payload({"transfer_id": TID, "sha256": "1" * 64})
    )
    await settle(backend)
    assert transfer.state == TransferState.FAILED
    assert not (tmp_path / "in.bin").exists()


async def test_chunk_past_declared_size_rejected(backend: MessagingBackend, tmp_path: Path) -> None:
    incoming(backend, tmp_path, b"abcd")
    await backend._handle_file_chunk(SENDER, pack_file_chunk(TID, 2, b"XXXXXXXX"))
    await backend._handle_file_chunk(SENDER, pack_file_chunk(TID, 1_000_000, b"X"))
    await settle(backend)
    assert backend._in[TID].expected == 0
    assert (tmp_path / "in.bin").stat().st_size == 0 if (tmp_path / "in.bin").exists() else True


async def test_chunks_for_cancelled_transfer_answered_with_reject(
    backend: MessagingBackend, tmp_path: Path
) -> None:
    transfer = incoming(backend, tmp_path, b"abcd")
    transfer.state = TransferState.CANCELLED
    await backend._handle_file_chunk(SENDER, pack_file_chunk(TID, 0, b"ab"))
    backend._send_control.assert_awaited()  # type: ignore[attr-defined]
    kind = backend._send_control.await_args.args[1]  # type: ignore[attr-defined]
    assert kind == MessageType.FILE_REJECT


@pytest.fixture
async def pair(tmp_path: Path):  # type: ignore[no-untyped-def]
    a, b = make_node(tmp_path, "alice"), make_node(tmp_path, "bob")
    await a.start()
    await b.start()
    trust(a, b)
    trust(b, a)
    bh = b.identities["tcp"].hash_id
    await a.connect_to_peer(bh)
    assert await a.wait_for_handshake(bh, timeout=8)
    yield a, b
    await a.stop()
    await b.stop()


def pace(node: MessagingBackend, monkeypatch: pytest.MonkeyPatch, **kw: float) -> None:
    params = {"chunk": 32 * 1024, "window": 512 * 1024, "stall": 12.0, "level": 0, "delay": 0.01}
    params.update(kw)
    monkeypatch.setattr(node, "_path_params", lambda link: tr.PathParams(**params))  # type: ignore[arg-type]


async def test_large_file_arrives_intact(pair, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    src = tmp_path / "big.bin"
    payload = os.urandom(12 * 1024 * 1024 + 123)
    src.write_bytes(payload)
    tid = (await a.send_file(b.identities["tcp"].hash_id, src))["id"]
    assert await wait_for(lambda: b._transfers.get(tid) is not None
                          and b._transfers[tid].state == "complete", timeout=60)
    assert b._incoming_paths[tid].read_bytes() == payload
    # The sender only says "complete" once the receiver verified the file,
    # and its chat bubble gets ✓✓ like a delivered message.
    assert await wait_for(lambda: a._transfers[tid].state == "complete", timeout=5)
    msg = next(m for m in a.get_messages() if m["metadata"].get("transfer_id") == tid)
    assert msg["status"] == "delivered"
    assert a._transfers[tid].sha256 == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize("canceller", ["sender", "receiver"])
async def test_cancel_reaches_the_other_side(  # type: ignore[no-untyped-def]
    pair, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, canceller: str
) -> None:
    a, b = pair
    pace(a, monkeypatch)
    src = tmp_path / "f.bin"
    src.write_bytes(os.urandom(4 * 1024 * 1024))
    tid = (await a.send_file(b.identities["tcp"].hash_id, src))["id"]
    assert await wait_for(lambda: tid in b._transfers and b._transfers[tid].offset > 0)
    me, other = (a, b) if canceller == "sender" else (b, a)
    assert await me.cancel_transfer(tid)
    assert await wait_for(lambda: other._transfers[tid].state == "cancelled", timeout=5)
    await asyncio.sleep(0.3)
    assert not b._incoming_paths[tid].exists(), "partial download must be deleted"
    assert not a._transfer_tasks
    offset = b._transfers[tid].offset
    await asyncio.sleep(0.3)
    assert b._transfers[tid].offset == offset and b._transfers[tid].state == "cancelled"


async def test_lost_chunk_is_resent(  # type: ignore[no-untyped-def]
    pair, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = pair
    pace(a, monkeypatch, stall=0.5)
    real = b._handle_file_chunk
    dropped: list[int] = []

    async def lossy(hash_id: str, body: bytes, *, compressed: bool = False) -> None:
        if not dropped:
            dropped.append(1)  # the first chunk vanishes (like a dying path)
            return
        await real(hash_id, body, compressed=compressed)

    monkeypatch.setattr(b, "_handle_file_chunk", lossy)
    src = tmp_path / "f.bin"
    payload = os.urandom(300 * 1024)
    src.write_bytes(payload)
    tid = (await a.send_file(b.identities["tcp"].hash_id, src))["id"]
    assert await wait_for(lambda: tid in b._transfers
                          and b._transfers[tid].state == "complete", timeout=15)
    assert b._incoming_paths[tid].read_bytes() == payload


async def test_rekey_mid_transfer_goes_back_and_completes(  # type: ignore[no-untyped-def]
    pair, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = pair
    pace(a, monkeypatch)
    bh = b.identities["tcp"].hash_id
    src = tmp_path / "f.bin"
    payload = os.urandom(3 * 1024 * 1024)
    src.write_bytes(payload)
    tid = (await a.send_file(bh, src))["id"]
    assert await wait_for(lambda: tid in b._transfers and b._transfers[tid].offset > 256 * 1024)
    epoch = a.get_link(bh).crypto.epoch
    await a.connect_to_peer(bh, force=False)  # ping only; then force a re-key:
    await a._initiate_handshake(bh)
    assert await wait_for(lambda: a.get_link(bh).crypto.epoch > epoch, timeout=5)
    assert await wait_for(lambda: b._transfers[tid].state == "complete", timeout=30)
    assert b._incoming_paths[tid].read_bytes() == payload


async def test_reoffer_of_cancelled_transfer_is_refused(  # type: ignore[no-untyped-def]
    pair, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = pair
    pace(a, monkeypatch)
    src = tmp_path / "f.bin"
    src.write_bytes(os.urandom(2 * 1024 * 1024))
    tid = (await a.send_file(b.identities["tcp"].hash_id, src))["id"]
    assert await wait_for(lambda: tid in b._transfers)
    # Bob cancels, but the message is lost.
    monkeypatch.setattr(b, "_send_reject", AsyncMock(return_value=False))
    await b.cancel_transfer(tid)
    monkeypatch.undo()
    pace(a, monkeypatch)
    # Alice keeps sending; Bob's answer to the stray data cancels her side.
    assert await wait_for(lambda: a._transfers[tid].state == "cancelled", timeout=5)


def test_lane_join_accepts_session_key_only(backend: MessagingBackend) -> None:
    keys_a = SessionKeys(send_key=b"\x01" * 32, recv_key=b"\x02" * 32, salt=b"")
    keys_b = SessionKeys(send_key=b"\x02" * 32, recv_key=b"\x01" * 32, salt=b"")
    sender, receiver = CryptoBox(keys_a), CryptoBox(keys_b)
    assert sender.lane_hint(outgoing=True) == receiver.lane_hint(outgoing=False)
    assert sender.lane_hint(outgoing=True) != sender.lane_hint(outgoing=False)

    hash_id = "c" * 32
    from srltcp.core.trusted import TrustedPeer

    backend.trusted.add(TrustedPeer(hash_id=hash_id, name="peer"))
    link = PeerLink(hash_id=hash_id, transport_peer_id="main", transport="tcp",
                    address="tor", public_key=b"", handshake_complete=True, crypto=receiver)
    backend.register_link(link)

    class FakeTcp:
        tuned: list[str] = []

        def is_tor(self, peer_id: str) -> bool:
            return True

        def tune_for_lane(self, peer_id: str, high: int) -> None:
            self.tuned.append(peer_id)

        async def disconnect(self, peer_id: str) -> None:
            pass

    backend.tcp_transport = FakeTcp()  # type: ignore[assignment]
    flags = Flags.ENCRYPTED | Flags.E2EE

    def join(box: CryptoBox) -> bytes:
        sealed = box.encrypt(b"srltcp-lane", header_aad(MessageType.LANE_JOIN, flags))
        return sender.lane_hint(outgoing=True) + sealed

    async def run() -> None:
        forged = CryptoBox(SessionKeys(send_key=b"\x09" * 32, recv_key=b"\x09" * 32, salt=b""))
        await backend._handle_lane_join(TransportPeer("lane-x", "tor", "tcp"), flags, join(forged))
        assert backend.lane_owner("lane-x") is None
        await backend._handle_lane_join(TransportPeer("lane-1", "tor", "tcp"), flags, join(sender))
        assert backend.lane_owner("lane-1") == hash_id
        # File chunks on the lane are routed to the session.
        chunk = pack_file_chunk(TID, 0, b"data")
        sealed = sender.encrypt(chunk, header_aad(MessageType.FILE_CHUNK, flags))
        backend._handle_file_chunk = AsyncMock()  # type: ignore[method-assign]
        await backend._on_transport_frame(
            TransportPeer("lane-1", "tor", "tcp"),
            build_header(MessageType.FILE_CHUNK, flags=flags, body=sealed),
        )
        backend._handle_file_chunk.assert_awaited_once()
        # Anything else on a lane is ignored.
        text = sender.encrypt(b"{}", header_aad(MessageType.TEXT, flags))
        backend._handle_text = AsyncMock()  # type: ignore[method-assign]
        await backend._on_transport_frame(
            TransportPeer("lane-1", "tor", "tcp"),
            build_header(MessageType.TEXT, flags=flags, body=text),
        )
        backend._handle_text.assert_not_called()

    asyncio.run(run())


async def test_early_chunks_cannot_exhaust_memory(
    backend: MessagingBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sender holding back the first chunk can't make us buffer the rest."""
    monkeypatch.setattr(tr, "RX_BUFFER_MAX", 4 * 1024 * 1024)
    incoming(backend, tmp_path, b"\0" * (64 * 1024 * 1024))
    block = os.urandom(1024 * 1024)
    for i in range(1, 12):
        await backend._handle_file_chunk(SENDER, pack_file_chunk(TID, i * len(block), block))
    rx = backend._in[TID]
    assert rx.ahead_bytes <= 4 * 1024 * 1024
    assert rx.expected == 0


async def test_ack_from_another_contact_is_ignored(
    backend: MessagingBackend, tmp_path: Path
) -> None:
    backend.identities["tcp"] = backend.identity_store.load_or_create("me", "tcp")
    src = tmp_path / "out.bin"
    src.write_bytes(b"x" * 1000)
    transfer = backend._new_outgoing("b" * 32, src, "tcp", None)
    transfer.state = TransferState.TRANSFERRING
    backend._out[transfer.id] = tr._Outgoing()
    body = encode_payload({"transfer_id": transfer.id, "offset": 1000, "ok": True})
    await backend._handle_file_ack("c" * 32, body)  # not the recipient
    assert backend._out[transfer.id].acked == 0
    assert backend._out[transfer.id].result is None


async def test_lane_join_refused_off_tor(backend: MessagingBackend) -> None:
    class FakeTcp:
        def is_tor(self, peer_id: str) -> bool:
            return False

    backend.tcp_transport = FakeTcp()  # type: ignore[assignment]
    await backend._handle_lane_join(TransportPeer("lan-x", "10.0.0.2:1", "tcp"), 0, b"\0" * 64)
    assert backend.lane_owner("lan-x") is None


class FakeLanes:
    """Just enough of the TCP transport for lane bookkeeping."""

    def __init__(self, lanes: list[str]) -> None:
        self.up = set(lanes)
        self.sent: list[str] = []
        self.closed: list[str] = []

    def has_peer(self, peer_id: str) -> bool:
        return peer_id in self.up

    def buffered(self, peer_id: str) -> int:
        return 0

    async def send_framed(self, peer_id: str, frame: bytes) -> None:
        self.sent.append(peer_id)

    async def disconnect(self, peer_id: str) -> None:
        self.up.discard(peer_id)
        self.closed.append(peer_id)


async def test_stuck_lane_is_replaced_without_resending_the_window(
    backend: MessagingBackend, tmp_path: Path
) -> None:
    """The chunk the receiver waits for sits on a stuck Tor circuit: only that
    circuit's chunks go out again (on the others), and it is dropped."""
    backend.identities["tcp"] = backend.identity_store.load_or_create("me", "tcp")
    hash_id, size = "b" * 32, 6 * 64 * 1024
    src = tmp_path / "out.bin"
    src.write_bytes(os.urandom(size))
    transfer = backend._new_outgoing(hash_id, src, "tcp", None)
    keys = SessionKeys(send_key=b"\x01" * 32, recv_key=b"\x02" * 32, salt=b"")
    link = PeerLink(hash_id=hash_id, transport_peer_id="main", transport="tcp",
                    address="tor", public_key=b"", handshake_complete=True, crypto=CryptoBox(keys))
    fake = FakeLanes(["lane-a", "lane-b", "lane-c"])
    backend.tcp_transport = fake  # type: ignore[assignment]
    backend._tor_lanes[hash_id] = ["lane-a", "lane-b", "lane-c"]
    for lane in fake.up:
        backend._lane_owner[lane] = hash_id
    n = 64 * 1024
    out = tr._Outgoing(acked=n, next=size, hashed=size)
    out.lane_of = {i * n: (["lane-a", "lane-b", "lane-c"][i % 3], n) for i in range(6)}
    out.lane_of[n] = ("lane-b", n)  # the one the receiver waits for
    out.lane_of[4 * n] = ("lane-b", n)
    with open(src, "rb") as fh:
        await backend._repair_lanes(hash_id, link, fh, out, transfer)
    await asyncio.sleep(0)
    assert backend._tor_lanes[hash_id] == ["lane-a", "lane-c"]
    assert "lane-b" not in backend._lane_owner and fake.closed == ["lane-b"]
    assert len(fake.sent) == 2 and "lane-b" not in fake.sent  # its 2 chunks, nothing else
    assert {lane for lane, _ in out.lane_of.values()} == {"lane-a", "lane-c"}
    assert not out.rewind and out.next == size


def test_refused_lanes_cap_how_many_are_dialed(backend: MessagingBackend) -> None:
    """An older contact takes fewer lanes: lanes it closes right away aren't redialed."""
    hash_id = "b" * 32
    backend._tor_lanes[hash_id] = ["l1", "l2", "l3"]
    for lane in ("l1", "l2", "l3"):
        backend._lane_owner[lane] = hash_id
    backend._lane_opened["l1"] = tr.time.monotonic() - 600  # worked for a long time
    backend._lane_closed("l1")
    assert hash_id not in backend._lane_cap
    backend._lane_opened["l3"] = tr.time.monotonic()  # refused as soon as it opened
    backend._lane_closed("l3")
    assert backend._lane_cap[hash_id][0] == 1


async def test_offer_flood_from_a_contact_is_capped(pair: Any) -> None:  # type: ignore[no-untyped-def]
    """A contact can't pile up unlimited unfinished files (memory, disk inodes, chat)."""
    a, b, *_ = pair
    ah = a.identities["tcp"].hash_id
    for i in range(tr.MAX_PENDING_INCOMING + 20):
        await b._handle_file_offer(ah, encode_payload(
            {"transfer_id": f"{i:016x}", "filename": f"f{i}", "size": 0}))
    pending = [t for t in b._transfers.values() if t.sender_hash == ah]
    assert len(pending) == tr.MAX_PENDING_INCOMING
