"""Length-prefixed frames with CRC32 integrity check."""

from __future__ import annotations

import struct
import zlib

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

