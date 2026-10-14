"""HKDF session key derivation tests."""

from __future__ import annotations

import hashlib

from srltcp.core.protocol.crypto import derive_session_key


def test_hkdf_directional_keys_differ() -> None:
    shared = b"\xab" * 32
    salt = hashlib.sha256(shared + b"srltcp-session-salt-v2").digest()[:16]
    send_key = derive_session_key(shared, salt, b"srltcp-v2-send")
    recv_key = derive_session_key(shared, salt, b"srltcp-v2-recv")
    assert send_key != recv_key
    assert len(send_key) == 32
    assert len(recv_key) == 32


def test_hkdf_requires_explicit_info() -> None:
    shared = b"\xcd" * 32
    salt = b"\x01" * 16
    a = derive_session_key(shared, salt, b"srltcp-v2-send")
    b = derive_session_key(shared, salt, b"srltcp-v2-recv")
    assert a != b
