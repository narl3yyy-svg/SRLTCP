"""Transfer cancel guards."""

from __future__ import annotations

from pathlib import Path

import pytest

from srltcp.core.messaging.backend import MessagingBackend, NodeConfig
from srltcp.core.messaging.models import FileTransfer, TransferState


@pytest.fixture
def backend() -> MessagingBackend:
    b = MessagingBackend(NodeConfig(name="test-node"))
    b._init_transfer()
    return b


@pytest.mark.asyncio
async def test_cancel_complete_transfer_rejected(backend: MessagingBackend) -> None:
    backend._transfers["done"] = FileTransfer(
        id="done",
        sender_hash="a" * 32,
        recipient_hash="b" * 32,
        filename="photo.png",
        path=Path("/tmp/photo.png"),
        size=100,
        sha256="",
        transport="tcp",
        state=TransferState.COMPLETE,
    )
    assert await backend.cancel_transfer("done") is False


@pytest.mark.asyncio
async def test_cancel_active_transfer_allowed(backend: MessagingBackend) -> None:
    backend._transfers["active"] = FileTransfer(
        id="active",
        sender_hash="a" * 32,
        recipient_hash="b" * 32,
        filename="doc.txt",
        path=Path("/tmp/doc.txt"),
        size=100,
        sha256="",
        transport="tcp",
        state=TransferState.TRANSFERRING,
    )
    assert await backend.cancel_transfer("active") is True
    assert backend._transfers["active"].state == TransferState.CANCELLED