"""Transfer stability and connection guard tests."""

from __future__ import annotations

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
    hash_id = "a" * 64
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
    hash_id = "b" * 64
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
    hash_id = "c" * 64
    assert backend.in_transfer_cooldown(hash_id) is False
    backend._mark_transfer_cooldown(hash_id)
    assert backend.in_transfer_cooldown(hash_id) is True