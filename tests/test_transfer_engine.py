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
    # The sender only says "complete" once the receiver verified the file.
    assert await wait_for(lambda: a._transfers[tid].state == "complete", timeout=5)
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
