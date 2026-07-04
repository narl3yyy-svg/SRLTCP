"""Async-friendly file helpers."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path

import aiofiles
import aiofiles.os

from srltcp.utils.platform import data_dir

CHUNK_SIZE = 1024 * 1024  # 1 MiB default read chunk
MAX_FOLDER_ZIP_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB


class FolderZipError(Exception):
    """Raised when a folder cannot be zipped for transfer."""


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_filename(name: str) -> str:
    """Sanitize a filename for cross-platform use."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name.strip())
    return cleaned or "unnamed"


def unique_dest_path(directory: Path, filename: str) -> Path:
    """Pick a destination path, adding 1, 2, … before the extension when taken."""
    directory = ensure_dir(directory)
    safe = safe_filename(filename)
    dest = directory / safe
    if not dest.exists():
        return dest
    stem = Path(safe).stem
    suffix = Path(safe).suffix
    counter = 1
    while True:
        candidate = directory / f"{stem}{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def human_size(num: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    size = float(num)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{num} B"


def folder_zip_temp_dir() -> Path:
    """Persistent temp directory for folder zips (avoids small /tmp quotas)."""
    return ensure_dir(data_dir() / "transfers" / ".zip-temp")


def folder_payload_size(path: Path) -> int:
    """Total byte size of a file or directory tree."""
    resolved = path.resolve()
    if not resolved.exists():
        raise FileNotFoundError(resolved)
    if resolved.is_file():
        return resolved.stat().st_size
    total = 0
    for dirpath, _dirnames, filenames in os.walk(resolved):
        for name in filenames:
            try:
                total += Path(dirpath, name).stat().st_size
            except OSError:
                continue
    return total


def zip_path_to_temp(path: Path, *, temp_dir: Path | None = None) -> Path:
    """Zip a file or directory to a temporary zip file and return its path."""
    resolved = path.resolve()
    if not resolved.exists():
        raise FileNotFoundError(resolved)

    payload_size = folder_payload_size(resolved)
    if payload_size > MAX_FOLDER_ZIP_BYTES:
        raise FolderZipError(
            f"Folder is too large to zip ({human_size(payload_size)}; "
            f"limit {human_size(MAX_FOLDER_ZIP_BYTES)})"
        )

    work_dir = ensure_dir(temp_dir or folder_zip_temp_dir())
    fd, tmp_name = tempfile.mkstemp(suffix=".zip", prefix="srltcp-folder-", dir=str(work_dir))
    os.close(fd)
    zip_path = Path(tmp_name)
    base_name = resolved.name

    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            if resolved.is_file():
                zf.write(resolved, resolved.name)
            else:
                for dirpath, _dirnames, filenames in os.walk(resolved):
                    for filename in filenames:
                        full = Path(dirpath) / filename
                        rel = full.relative_to(resolved).as_posix()
                        arcname = f"{base_name}/{rel}"
                        zf.write(full, arcname)
        return zip_path
    except OSError as exc:
        zip_path.unlink(missing_ok=True)
        if exc.errno == 122:  # EDQUOT — disk quota exceeded
            raise FolderZipError(
                "Not enough disk space to create folder zip. "
                "Free space in your SRLTCP data directory or choose a smaller folder."
            ) from exc
        raise FolderZipError(f"Could not zip folder: {exc}") from exc
    except Exception:
        zip_path.unlink(missing_ok=True)
        raise


async def zip_path_to_temp_async(path: Path, *, temp_dir: Path | None = None) -> Path:
    """Run zip_path_to_temp in a worker thread so the event loop stays responsive."""
    return await asyncio.to_thread(zip_path_to_temp, path, temp_dir=temp_dir)


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