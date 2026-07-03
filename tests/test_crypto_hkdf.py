"""HKDF session key derivation tests."""

from __future__ import annotations

import hashlib

from srltcp.core.identity import Identity
from srltcp.core.protocol.crypto import KeyExchange, derive_session_key


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


def test_key_exchange_initiator_responder_swap() -> None:
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

    assert alice_keys.send_key == bob_keys.recv_key
    assert alice_keys.recv_key == bob_keys.send_key