"""Tests for file transfer media types and transfer guards."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from srltcp.core.messaging.backend import MessagingBackend, NodeConfig
from srltcp.core.messaging.models import TransferState


@pytest.fixture
def backend() -> MessagingBackend:
    b = MessagingBackend(NodeConfig(name="test-node"))
    b._init_transfer()
    b._init_ping()
    return b


def test_file_msg_type_image(backend: MessagingBackend) -> None:
    assert backend._file_msg_type("photo.PNG") == "image"
    assert backend._file_msg_type("x.webp") == "image"


def test_file_msg_type_video(backend: MessagingBackend) -> None:
    assert backend._file_msg_type("clip.mp4") == "video"
    assert backend._file_msg_type("movie.MKV") == "video"


def test_file_msg_type_other(backend: MessagingBackend) -> None:
    assert backend._file_msg_type("doc.pdf") == "file"
    assert backend._file_msg_type("archive.zip") == "file"


def test_has_active_transfer_for(backend: MessagingBackend) -> None:
    from srltcp.core.messaging.models import FileTransfer

    backend._transfers["t1"] = FileTransfer(
        id="t1",
        sender_hash="aaa",
        recipient_hash="bbb",
        filename="f.bin",
        path=Path("/tmp/f.bin"),
        size=100,
        sha256="",
        transport="tcp",
        state=TransferState.TRANSFERRING,
    )
    assert backend.has_active_transfer_for("bbb") is True
    assert backend.has_active_transfer_for("ccc") is False


def test_maybe_compress_skips_serial(backend: MessagingBackend) -> None:
    data = b"x" * (128 * 1024)
    payload, compressed = backend._maybe_compress(data, transport="serial")
    assert payload == data
    assert compressed is False


@pytest.mark.asyncio
async def test_connect_skips_force_during_transfer(backend: MessagingBackend) -> None:
    from srltcp.core.messaging.links import PeerLink
    from srltcp.core.messaging.models import FileTransfer
    from srltcp.core.trusted import TrustedPeer

    hash_id = "deadbeef" * 4
    backend.trusted.add(
        TrustedPeer(hash_id=hash_id, name="peer", transport="tcp", tcp_host="127.0.0.1")
    )
    link = PeerLink(
        hash_id=hash_id,
        transport_peer_id="peer1",
        transport="tcp",
        address="127.0.0.1:7825",
        public_key=b"\x00" * 32,
        peer_name="peer",
        handshake_complete=True,
    )
    backend.register_link(link)
    backend.tcp_transport = MagicMock()
    backend.tcp_transport.has_peer = MagicMock(return_value=True)
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
    backend._teardown_link = AsyncMock()
    result = await backend.connect_to_peer(hash_id, force=True)
    assert result is True
    backend._teardown_link.assert_not_called()