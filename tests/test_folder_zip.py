"""Folder zip and active transfer listing tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from srltcp.core.messaging.backend import MessagingBackend, NodeConfig
from srltcp.core.messaging.models import FileTransfer, TransferState
from srltcp.utils.files import zip_path_to_temp


@pytest.fixture
def backend() -> MessagingBackend:
    b = MessagingBackend(NodeConfig(name="test-node"))
    b._init_transfer()
    return b


def test_zip_path_to_temp_directory(tmp_path: Path) -> None:
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "a.txt").write_text("hello")
    sub = folder / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("world")
    zip_path = zip_path_to_temp(folder)
    try:
        assert zip_path.is_file()
        assert zip_path.stat().st_size > 0
    finally:
        zip_path.unlink(missing_ok=True)


def test_list_transfers_active_only(backend: MessagingBackend) -> None:
    backend._transfers["done"] = FileTransfer(
        id="done",
        sender_hash="a" * 32,
        recipient_hash="b" * 32,
        filename="x.bin",
        path=Path("/tmp/x.bin"),
        size=10,
        sha256="",
        transport="tcp",
        state=TransferState.COMPLETE,
    )
    backend._transfers["active"] = FileTransfer(
        id="active",
        sender_hash="a" * 32,
        recipient_hash="b" * 32,
        filename="y.bin",
        path=Path("/tmp/y.bin"),
        size=10,
        sha256="",
        transport="tcp",
        state=TransferState.TRANSFERRING,
    )
    assert len(backend.list_transfers(active_only=True)) == 1
    assert len(backend.list_transfers(active_only=False)) == 2