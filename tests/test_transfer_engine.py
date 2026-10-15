"""Windowed transfers: ordering, verification, cancel both ways, recovery."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

import srltcp.core.messaging.transfer as tr
from srltcp.core.messaging.backend import MessagingBackend, NodeConfig
from srltcp.core.messaging.models import FileTransfer, TransferState
from srltcp.core.protocol.messages import (
    MessageType,
    encode_payload,
    pack_file_chunk,
)
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










async def test_offer_flood_from_a_contact_is_capped(pair: Any) -> None:  # type: ignore[no-untyped-def]
    """A contact can't pile up unlimited unfinished files (memory, disk inodes, chat)."""
    a, b, *_ = pair
    ah = a.identities["tcp"].hash_id
    for i in range(tr.MAX_PENDING_INCOMING + 20):
        await b._handle_file_offer(ah, encode_payload(
            {"transfer_id": f"{i:016x}", "filename": f"f{i}", "size": 0}))
    pending = [t for t in b._transfers.values() if t.sender_hash == ah]
    assert len(pending) == tr.MAX_PENDING_INCOMING


async def test_download_shows_under_its_name_only_once_verified(
    backend: MessagingBackend, tmp_path: Path
) -> None:
    """Bytes go to a hidden partial file (the gallery and other apps skip
    it); the real name appears only after the SHA-256 check, and a damaged
    download never appears at all."""
    for good in (True, False):
        payload = os.urandom(3000)
        transfer = incoming(backend, tmp_path, payload)
        final = transfer.path
        part = tr.part_path(final)
        assert part.name.startswith(".") and part.parent == final.parent
        backend._incoming_paths[TID] = part
        backend._in[TID] = tr._Incoming(path=part)
        await backend._handle_file_chunk(SENDER, pack_file_chunk(TID, 0, payload[:1000]))
        await settle(backend)
        assert part.exists() and not final.exists()  # half-received: hidden
        await backend._handle_file_chunk(SENDER, pack_file_chunk(TID, 1000, payload[1000:]))
        sha = hashlib.sha256(payload).hexdigest() if good else "1" * 64
        await backend._handle_file_complete(
            SENDER, encode_payload({"transfer_id": TID, "sha256": sha})
        )
        await settle(backend)
        assert not part.exists()
        if good:
            assert transfer.state == TransferState.COMPLETE
            assert final.read_bytes() == payload
            assert backend.transfer_file_path(TID) == final
            final.unlink()
        else:
            assert transfer.state == TransferState.FAILED and not final.exists()
        backend._transfers.pop(TID)


def test_new_download_never_takes_a_name_in_use(backend: MessagingBackend) -> None:
    first = backend._free_dest("photo.jpg")
    backend._transfers["1" * 16] = FileTransfer(
        id="1" * 16, sender_hash=SENDER, recipient_hash="0" * 32, filename="photo.jpg",
        path=first, size=1, sha256="", transport="tcp", state=TransferState.TRANSFERRING,
    )
    second = backend._free_dest("photo.jpg")
    assert second != first and second.name == "photo1.jpg"
