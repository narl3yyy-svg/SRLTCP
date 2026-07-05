"""Transfer stability and connection guard tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from srltcp.core.messaging.backend import MessagingBackend, NodeConfig
from srltcp.core.messaging.links import PeerLink
from srltcp.core.messaging.models import FileTransfer, TransferState
from srltcp.transports.base import Connection, TransportPeer


@pytest.fixture
def backend() -> MessagingBackend:
    b = MessagingBackend(NodeConfig(name="test-node"))
    b._init_transfer()
    b._init_ping()
    b._init_connect()
    return b


@pytest.mark.asyncio
async def test_file_chunk_dispatched_as_background_task(backend: MessagingBackend) -> None:
    hash_id = "a" * 32
    peer_id = "peer-1"
    link = PeerLink(
        hash_id=hash_id,
        transport_peer_id=peer_id,
        transport="tcp",
        address="127.0.0.1:7825",
        public_key=b"\x00" * 32,
        peer_name="peer",
        handshake_complete=True,
    )
    backend.register_link(link)
    backend._handle_file_chunk = AsyncMock()

    with patch("asyncio.create_task") as mock_task:
        peer = TransportPeer(peer_id=peer_id, address="127.0.0.1:7825", transport="tcp")
        from srltcp.core.protocol.messages import MessageType, build_header

        packet = build_header(MessageType.FILE_CHUNK, body=b"encrypted")
        await backend._on_transport_frame(peer, packet)

    mock_task.assert_called_once()


@pytest.mark.asyncio
async def test_complete_handshake_skips_ping_during_transfer(
    backend: MessagingBackend,
) -> None:
    hash_id = "b" * 32
    link = PeerLink(
        hash_id=hash_id,
        transport_peer_id="peer-2",
        transport="tcp",
        address="127.0.0.1:7825",
        public_key=b"\x00" * 32,
        peer_name="peer",
        handshake_complete=False,
    )
    backend.register_link(link)
    backend.identities["tcp"] = backend.identity_store.load_or_create("test", "tcp")
    backend._transfers["t1"] = FileTransfer(
        id="t1",
        sender_hash="sender",
        recipient_hash=hash_id,
        filename="big.bin",
        path=Path("/tmp/big.bin"),
        size=1024,
        sha256="",
        transport="tcp",
        state=TransferState.TRANSFERRING,
    )
    backend.ping_peer = AsyncMock()
    backend._on_link_up = None
    backend._on_peer_metrics = None

    await backend._complete_handshake(hash_id, "peer")
    backend.ping_peer.assert_not_called()


def test_connection_send_lock_exists() -> None:
    peer = TransportPeer(peer_id="p", address="127.0.0.1:1", transport="tcp")
    conn = Connection(peer, MagicMock(), MagicMock())
    assert hasattr(conn, "_send_lock")


def test_transfer_cooldown(backend: MessagingBackend) -> None:
    hash_id = "c" * 32
    assert backend.in_transfer_cooldown(hash_id) is False
    backend._mark_transfer_cooldown(hash_id)
    assert backend.in_transfer_cooldown(hash_id) is True


@pytest.mark.asyncio
async def test_recover_incoming_waits_for_pending_chunk_tasks(
    backend: MessagingBackend, tmp_path: Path
) -> None:
    sender = "f" * 32
    recipient = "0" * 32
    payload = b"chunk-payload"
    dest = tmp_path / "file.bin"
    import hashlib

    digest = hashlib.sha256(payload).hexdigest()
    transfer = FileTransfer(
        id="aabbccddeeff0101",
        sender_hash=sender,
        recipient_hash=recipient,
        filename="file.bin",
        path=dest,
        size=len(payload),
        sha256=digest,
        transport="tcp",
        state=TransferState.TRANSFERRING,
        offset=0,
    )
    backend._transfers[transfer.id] = transfer
    backend._incoming_paths[transfer.id] = dest

    async def slow_chunk() -> None:
        await asyncio.sleep(0.05)
        dest.write_bytes(payload)
        transfer.offset = len(payload)

    task = asyncio.create_task(slow_chunk())
    backend._track_chunk_task(sender, task)
    backend._on_transfer_complete = AsyncMock()

    with (
        patch("srltcp.core.messaging.transfer.fsync_file", AsyncMock()),
        patch(
            "srltcp.core.messaging.transfer.sha256_file",
            AsyncMock(return_value=digest),
        ),
    ):
        await backend._recover_incoming_transfers_for_peer(sender)

    assert transfer.state == TransferState.COMPLETE
    backend._on_transfer_complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_incoming_transfer_auto_finalizes_when_all_bytes_received(
    backend: MessagingBackend, tmp_path: Path
) -> None:
    sender = "d" * 32
    recipient = "e" * 32
    payload = b"png-bytes-here"
    dest = tmp_path / "shot.png"
    dest.write_bytes(payload)
    import hashlib

    digest = hashlib.sha256(payload).hexdigest()
    transfer = FileTransfer(
        id="aabbccddeeff0102",
        sender_hash=sender,
        recipient_hash=recipient,
        filename="shot.png",
        path=dest,
        size=len(payload),
        sha256=digest,
        transport="tcp",
        state=TransferState.TRANSFERRING,
        offset=len(payload),
    )
    backend._transfers[transfer.id] = transfer
    backend._incoming_paths[transfer.id] = dest
    backend._on_transfer_complete = AsyncMock()

    with patch("srltcp.core.messaging.transfer.fsync_file", AsyncMock()):
        ok = await backend._maybe_finalize_incoming_transfer(
            transfer.id, peer_hash=sender
        )
    assert ok is True
    assert transfer.state == TransferState.COMPLETE
    backend._on_transfer_complete.assert_awaited_once()