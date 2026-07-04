"""File helper tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from srltcp.utils.files import fsync_file, write_file_chunk


@pytest.mark.asyncio
async def test_write_file_chunk_sparse(tmp_path: Path) -> None:
    dest = tmp_path / "partial.bin"
    await write_file_chunk(dest, 1024, b"hello")
    assert dest.stat().st_size == 1029
    assert dest.read_bytes()[1024:1029] == b"hello"


@pytest.mark.asyncio
async def test_write_file_chunk_no_fsync_by_default(tmp_path: Path) -> None:
    dest = tmp_path / "fast.bin"
    for i in range(10):
        await write_file_chunk(dest, i * 64, b"x" * 64)
    assert dest.stat().st_size == 640


@pytest.mark.asyncio
async def test_fsync_file_uses_os_fsync(tmp_path: Path) -> None:
    dest = tmp_path / "synced.bin"
    dest.write_bytes(b"sync-test")
    await fsync_file(dest)
    assert dest.read_bytes() == b"sync-test"