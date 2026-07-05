"""Length-prefixed frames with CRC32 integrity check."""

from __future__ import annotations

import struct
import zlib
from collections.abc import AsyncIterator, Awaitable, Callable

# Magic: 'SRL\x01' + version byte
FRAME_MAGIC = b"SRL\x01"
HEADER_FMT = ">4sI"  # magic + payload length (uint32 BE)
HEADER_SIZE = struct.calcsize(HEADER_FMT)
CRC_SIZE = 4
MAX_FRAME_SIZE = 64 * 1024 * 1024  # 64 MiB per frame


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def pack_frame(payload: bytes) -> bytes:
    """Wrap payload in magic + length + CRC + payload."""
    if len(payload) > MAX_FRAME_SIZE:
        raise ValueError(f"Frame too large: {len(payload)} > {MAX_FRAME_SIZE}")
    length = len(payload)
    checksum = crc32(payload)
    return FRAME_MAGIC + struct.pack(">I", length) + struct.pack(">I", checksum) + payload


def unpack_frame(buffer: bytes) -> tuple[bytes, bytes]:
    """
    Parse one frame from buffer.
    Returns (payload, remainder).
    Raises ValueError if incomplete or corrupt.
    """
    min_size = HEADER_SIZE + CRC_SIZE
    if len(buffer) < min_size:
        raise ValueError("incomplete")

    if not buffer.startswith(FRAME_MAGIC):
        raise ValueError("bad magic")

    length = struct.unpack(">I", buffer[4:8])[0]
    if length > MAX_FRAME_SIZE:
        raise ValueError("frame size exceeds limit")

    total = HEADER_SIZE + CRC_SIZE + length
    if len(buffer) < total:
        raise ValueError("incomplete")

    expected_crc = struct.unpack(">I", buffer[8:12])[0]
    payload = buffer[12 : 12 + length]
    actual_crc = crc32(payload)
    if actual_crc != expected_crc:
        raise ValueError(f"CRC mismatch: {actual_crc:#x} != {expected_crc:#x}")

    return payload, buffer[total:]


class FrameReader:
    """Incremental frame reader for stream transports."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self._buffer.extend(data)
        frames: list[bytes] = []
        while True:
            try:
                payload, remainder = unpack_frame(bytes(self._buffer))
            except ValueError as exc:
                if str(exc) == "incomplete":
                    break
                # Resync: drop one byte and retry
                if self._buffer:
                    self._buffer.pop(0)
                    continue
                raise
            frames.append(payload)
            self._buffer = bytearray(remainder)
        return frames


class FrameWriter:
    """Frame encoder."""

    @staticmethod
    def write(payload: bytes) -> bytes:
        return pack_frame(payload)


async def read_frames_from_stream(
    read_coro: Callable[[int], Awaitable[bytes]],
    chunk_size: int = 65536,
) -> AsyncIterator[bytes]:
    """Read frames from an async read callable."""
    reader = FrameReader()
    while True:
        data = await read_coro(chunk_size)
        if not data:
            break
        for frame in reader.feed(data):
            yield frame