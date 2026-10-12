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
# Largest frame anyone may send us. File chunks are 1 MiB, so 4 MiB is plenty;
# kept small because unauthenticated connections can also make us buffer it.
MAX_FRAME_SIZE = 4 * 1024 * 1024


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
        """Return every complete frame in the buffer.

        Parses in place (no per-frame copy of the whole buffer) and, on junk,
        jumps straight to the next frame magic — a peer sending garbage must
        not be able to pin the event loop.
        """
        self._buffer.extend(data)
        buf = self._buffer
        view = memoryview(buf)
        frames: list[bytes] = []
        pos = 0
        min_size = HEADER_SIZE + CRC_SIZE
        try:
            while len(buf) - pos >= min_size:
                if view[pos : pos + 4] != FRAME_MAGIC:
                    nxt = buf.find(FRAME_MAGIC, pos + 1)
                    if nxt < 0:
                        # Keep a possible partial magic at the very end.
                        pos = max(pos, len(buf) - (len(FRAME_MAGIC) - 1))
                        break
                    pos = nxt
                    continue
                length = struct.unpack_from(">I", buf, pos + 4)[0]
                if length > MAX_FRAME_SIZE:
                    pos += 1
                    continue
                total = min_size + length
                if len(buf) - pos < total:
                    break
                expected_crc = struct.unpack_from(">I", buf, pos + 8)[0]
                payload = bytes(view[pos + 12 : pos + total])
                if crc32(payload) != expected_crc:
                    pos += 1
                    continue
                frames.append(payload)
                pos += total
        finally:
            view.release()
        if pos:
            del buf[:pos]
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