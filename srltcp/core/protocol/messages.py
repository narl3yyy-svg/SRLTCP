"""SRLTCP message types and serialization."""

from __future__ import annotations

import json
import struct
from enum import IntEnum
from typing import Any

# Wire header: msg_type (1) + flags (1) + stream_id (4) + seq (4) = 10 bytes
HEADER_SIZE = 10


class MessageType(IntEnum):
    HANDSHAKE = 0x01
    HANDSHAKE_ACK = 0x02
    PING = 0x03
    PONG = 0x04
    ANNOUNCE = 0x10
    ANNOUNCE_ACK = 0x11
    TEXT = 0x20
    FILE_OFFER = 0x30
    FILE_ACCEPT = 0x31
    FILE_REJECT = 0x32
    FILE_CHUNK = 0x33
    FILE_COMPLETE = 0x34
    FILE_RESUME = 0x35
    SHARE_LIST = 0x40
    SHARE_REQUEST = 0x41
    RELAY_ENVELOPE = 0x50
    ROUTE_UPDATE = 0x51
    ERROR = 0xFF


class Flags:
    COMPRESSED = 0x01
    ENCRYPTED = 0x02
    RELAY = 0x04
    E2EE = 0x08


def parse_header(data: bytes) -> tuple[MessageType, int, int, int, bytes]:
    if len(data) < HEADER_SIZE:
        raise ValueError("header too short")
    msg_type, flags, stream_id, seq = struct.unpack(">BBII", data[:HEADER_SIZE])
    return MessageType(msg_type), flags, stream_id, seq, data[HEADER_SIZE:]


def build_header(
    msg_type: MessageType,
    flags: int = 0,
    stream_id: int = 0,
    seq: int = 0,
    body: bytes = b"",
) -> bytes:
    return struct.pack(">BBII", int(msg_type), flags & 0xFF, stream_id & 0xFFFFFFFF, seq) + body


def encode_payload(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def decode_payload(data: bytes) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(data.decode("utf-8"))
    return result


# File chunk binary layout after header:
# transfer_id (16 bytes uuid hex as ascii) + offset (8) + length (4) + data
FILE_CHUNK_META_FMT = ">16sQI"
FILE_CHUNK_META_SIZE = struct.calcsize(FILE_CHUNK_META_FMT)


def pack_file_chunk(transfer_id: str, offset: int, data: bytes) -> bytes:
    tid = transfer_id.encode("ascii")[:16].ljust(16, b"\x00")
    meta = struct.pack(FILE_CHUNK_META_FMT, tid, offset, len(data))
    return meta + data


def unpack_file_chunk(body: bytes) -> tuple[str, int, bytes]:
    if len(body) < FILE_CHUNK_META_SIZE:
        raise ValueError("file chunk meta too short")
    tid_raw, offset, length = struct.unpack(FILE_CHUNK_META_FMT, body[:FILE_CHUNK_META_SIZE])
    transfer_id = tid_raw.rstrip(b"\x00").decode("ascii")
    data = body[FILE_CHUNK_META_SIZE : FILE_CHUNK_META_SIZE + length]
    if len(data) != length:
        raise ValueError("file chunk length mismatch")
    return transfer_id, offset, data