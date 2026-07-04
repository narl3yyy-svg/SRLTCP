"""Protocol framing and crypto unit tests."""

from __future__ import annotations

from srltcp.core.identity import Identity
from srltcp.core.protocol.crypto import CryptoBox, KeyExchange, identity_hash
from srltcp.core.protocol.framing import FrameReader, pack_frame, unpack_frame
from srltcp.core.protocol.messages import (
    MessageType,
    build_header,
    decode_payload,
    encode_payload,
    pack_file_chunk,
    parse_header,
    unpack_file_chunk,
)


def test_frame_roundtrip() -> None:
    payload = b"hello srltcp"
    frame = pack_frame(payload)
    decoded, remainder = unpack_frame(frame)
    assert decoded == payload
    assert remainder == b""


def test_frame_reader_incremental() -> None:
    reader = FrameReader()
    payload = b"x" * 1000
    frame = pack_frame(payload)
    part1 = frame[:20]
    part2 = frame[20:]
    assert reader.feed(part1) == []
    frames = reader.feed(part2)
    assert len(frames) == 1
    assert frames[0] == payload


def test_message_header() -> None:
    body = encode_payload({"text": "hi"})
    packet = build_header(MessageType.TEXT, flags=0x0A, stream_id=1, seq=42, body=body)
    msg_type, flags, stream_id, seq, rest = parse_header(packet)
    assert msg_type == MessageType.TEXT
    assert flags == 0x0A
    assert stream_id == 1
    assert seq == 42
    assert decode_payload(rest)["text"] == "hi"


def test_file_chunk_pack_unpack() -> None:
    tid = "abcd1234ef567890"
    data = b"chunk data here"
    packed = pack_file_chunk(tid, 4096, data)
    out_tid, offset, out_data = unpack_file_chunk(packed)
    assert out_tid == tid
    assert offset == 4096
    assert out_data == data


def test_transfer_id_normalization() -> None:
    from srltcp.core.protocol.messages import normalize_transfer_id

    full = "a" * 32
    assert normalize_transfer_id(full) == "a" * 16
    assert normalize_transfer_id(f"  {full.upper()}  ") == "a" * 16


def test_identity_hash_deterministic() -> None:
    identity = Identity.generate("test", "tcp")
    pub = identity.public_bytes()
    assert identity.hash_id == identity_hash(pub)
    assert len(identity.hash_id) == 32


def test_key_exchange_e2ee() -> None:
    alice = Identity.generate("alice", "tcp")
    bob = Identity.generate("bob", "tcp")

    alice_kx = KeyExchange(alice.private_key)
    bob_kx = KeyExchange(bob.private_key)

    alice_keys = alice_kx.complete(
        bob_kx.ephemeral_public,
        bob_kx.sign_ephemeral(),
        bob.public_key,
        initiator=True,
    )
    bob_keys = bob_kx.complete(
        alice_kx.ephemeral_public,
        alice_kx.sign_ephemeral(),
        alice.public_key,
        initiator=False,
    )

    alice_box = CryptoBox(alice_keys)
    bob_box = CryptoBox(bob_keys)

    plaintext = b"secret message for relay opaque forwarding"
    ciphertext = alice_box.encrypt(plaintext)
    decrypted = bob_box.decrypt(ciphertext)
    assert decrypted == plaintext