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
    HANDSHAKE_FINISH = 0x05  # retired (Noise XX); never sent, ignored
    PING = 0x03
    PONG = 0x04
    ANNOUNCE = 0x10
    ANNOUNCE_ACK = 0x11
    RADIO_BEACON = 0x12  # private "a contact is in range" on a serial radio (beacon.py)
    TEXT = 0x20
    FILE_OFFER = 0x30
    FILE_ACCEPT = 0x31
    FILE_REJECT = 0x32
    FILE_CHUNK = 0x33
    FILE_COMPLETE = 0x34
    FILE_RESUME = 0x35
    FILE_ACK = 0x36  # receiver -> sender: bytes on disk (window), then verified
    LANE_JOIN = 0x37  # retired (Tor file lanes, until 0.11); ignored
    SHARE_LIST = 0x40
    SHARE_REQUEST = 0x41
    SYNC = 0x42  # folder sync (sync.py)
    RECEIPT = 0x21
    PAIR = 0x22
    RENDEZVOUS = 0x23  # our current addresses (LAN/VPN, public, onion)
    PUNCH = 0x24  # hole punching offer/answer (punch.py)
    CALL_SIGNAL = 0x60
    CALL_MEDIA = 0x61
    GROUP_UPDATE = 0x70
    GROUP_TEXT = 0x71
    GROUP_LEAVE = 0x72
    ERROR = 0xFF


class Flags:
    COMPRESSED = 0x01
    ENCRYPTED = 0x02
    RELAY = 0x04  # (retired: was the internet relay)
    E2EE = 0x08


# Messages that are only valid inside an established E2EE session. They must
# carry Flags.ENCRYPTED and are authenticated with the header as AAD.
SESSION_MESSAGE_TYPES = frozenset(
    {
        MessageType.TEXT,
        MessageType.RECEIPT,
        MessageType.PAIR,
        MessageType.RENDEZVOUS,
        MessageType.PUNCH,
        MessageType.FILE_OFFER,
        MessageType.FILE_ACCEPT,
        MessageType.FILE_REJECT,
        MessageType.FILE_CHUNK,
        MessageType.FILE_COMPLETE,
        MessageType.FILE_RESUME,
        MessageType.FILE_ACK,
        MessageType.SHARE_LIST,
        MessageType.SHARE_REQUEST,
        MessageType.SYNC,
        MessageType.CALL_SIGNAL,
        MessageType.CALL_MEDIA,
        MessageType.GROUP_UPDATE,
        MessageType.GROUP_TEXT,
        MessageType.GROUP_LEAVE,
    }
)

# Group traffic is authorised by group membership (see groups.py), not by
# the sender being one of our contacts.
GROUP_MESSAGE_TYPES = frozenset(
    {MessageType.GROUP_UPDATE, MessageType.GROUP_TEXT, MessageType.GROUP_LEAVE}
)


def header_aad(msg_type: MessageType, flags: int) -> bytes:
    """Associated data binding a ciphertext to its message type and flags."""
    return bytes([int(msg_type) & 0xFF, flags & 0xFF])


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
# transfer_id (16 bytes lowercase hex as ascii) + offset (8) + length (4) + data
FILE_CHUNK_META_FMT = ">16sQI"
FILE_CHUNK_META_SIZE = struct.calcsize(FILE_CHUNK_META_FMT)
TRANSFER_ID_HEX_LEN = 16


def normalize_transfer_id(transfer_id: str) -> str:
    """Normalize a transfer ID to exactly 16 lowercase hex chars (wire format)."""
    cleaned = transfer_id.strip().lower()
    if not cleaned:
        raise ValueError("transfer_id required")
    hex_only = "".join(c for c in cleaned if c in "0123456789abcdef")
    if len(hex_only) < TRANSFER_ID_HEX_LEN:
        raise ValueError(f"transfer_id must be at least {TRANSFER_ID_HEX_LEN} hex chars")
    return hex_only[:TRANSFER_ID_HEX_LEN]


def is_valid_transfer_id(transfer_id: str) -> bool:
    try:
        normalize_transfer_id(transfer_id)
    except ValueError:
        return False
    return True


def pack_file_chunk(transfer_id: str, offset: int, data: bytes) -> bytes:
    tid_str = normalize_transfer_id(transfer_id)
    tid = tid_str.encode("ascii")
    if len(tid) != TRANSFER_ID_HEX_LEN:
        raise ValueError("transfer_id encoding length mismatch")
    meta = struct.pack(FILE_CHUNK_META_FMT, tid, offset, len(data))
    return meta + data


def unpack_file_chunk(body: bytes) -> tuple[str, int, bytes]:
    if len(body) < FILE_CHUNK_META_SIZE:
        raise ValueError("file chunk meta too short")
    tid_raw, offset, length = struct.unpack(FILE_CHUNK_META_FMT, body[:FILE_CHUNK_META_SIZE])
    transfer_id = tid_raw.decode("ascii", errors="ignore").strip("\x00").lower()
    if len(transfer_id) != TRANSFER_ID_HEX_LEN or not is_valid_transfer_id(transfer_id):
        raise ValueError("invalid transfer_id in file chunk")
    data = body[FILE_CHUNK_META_SIZE : FILE_CHUNK_META_SIZE + length]
    if len(data) != length:
        raise ValueError("file chunk length mismatch")
    return transfer_id, offset, data