"""Async-friendly file helpers."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import AsyncIterator
from pathlib import Path

import aiofiles
import aiofiles.os

CHUNK_SIZE = 1024 * 1024  # 1 MiB default read chunk


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_filename(name: str) -> str:
    """Sanitize a filename for cross-platform use."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name.strip())
    return cleaned or "unnamed"


def human_size(num: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    size = float(num)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{num} B"


async def sha256_file(path: Path, chunk_size: int = CHUNK_SIZE) -> str:
    digest = hashlib.sha256()
    async with aiofiles.open(path, "rb") as f:
        while True:
            block = await f.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


async def read_file_chunk(path: Path, offset: int, length: int) -> bytes:
    async with aiofiles.open(path, "rb") as f:
        await f.seek(offset)
        data: bytes = await f.read(length)
        return data


async def write_file_chunk(path: Path, offset: int, data: bytes, *, fsync: bool = False) -> None:
    """Write a chunk at offset. Fsync is optional — callers fsync on transfer complete."""
    ensure_dir(path.parent)
    existed = path.exists()
    async with aiofiles.open(path, "r+b" if existed else "w+b") as f:
        end = offset + len(data)
        current = path.stat().st_size if existed else 0
        if end > current:
            await f.truncate(end)
        await f.seek(offset)
        await f.write(data)
        await f.flush()
        if fsync:
            await aiofiles.os.fsync(f.fileno())


async def fsync_file(path: Path) -> None:
    async with aiofiles.open(path, "rb") as f:
        await aiofiles.os.fsync(f.fileno())


def walk_directory(root: Path) -> list[dict[str, object]]:
    """Return a flat listing of files under root (relative paths)."""
    entries: list[dict[str, object]] = []
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        for d in sorted(dirnames):
            rel = rel_dir / d if str(rel_dir) != "." else Path(d)
            entries.append({"name": str(rel), "type": "dir", "size": 0})
        for fn in sorted(filenames):
            rel = rel_dir / fn if str(rel_dir) != "." else Path(fn)
            full = root / rel
            entries.append(
                {
                    "name": str(rel).replace("\\", "/"),
                    "type": "file",
                    "size": full.stat().st_size,
                }
            )
    return entries


async def stream_file(
    path: Path, offset: int = 0, chunk_size: int = CHUNK_SIZE
) -> AsyncIterator[bytes]:
    async with aiofiles.open(path, "rb") as f:
        await f.seek(offset)
        while True:
            block = await f.read(chunk_size)
            if not block:
                break
            yield block