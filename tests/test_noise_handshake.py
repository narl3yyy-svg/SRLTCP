"""Noise XX handshake tests."""

from __future__ import annotations

from srltcp.core.identity import Identity
from srltcp.core.protocol.noise_handshake import NoiseHandshakeSession


def test_noise_xx_session_keys_match() -> None:
    alice_id = Identity.generate("alice", "tcp")
    bob_id = Identity.generate("bob", "tcp")

    alice = NoiseHandshakeSession.create(alice_id.private_key, initiator=True)
    bob = NoiseHandshakeSession.create(bob_id.private_key, initiator=False)

    bob.read_message(alice.write_message())
    alice.read_message(bob.write_message())
    bob.read_message(alice.write_message())

    assert alice.complete and bob.complete
    alice_keys = alice.session_keys(initiator=True)
    bob_keys = bob.session_keys(initiator=False)
    assert alice_keys.send_key == bob_keys.recv_key
    assert alice_keys.recv_key == bob_keys.send_key


def test_derive_noise_static_key_is_stable() -> None:
    from srltcp.core.protocol.noise_handshake import derive_noise_static_key

    identity = Identity.generate("stable", "tcp")
    assert derive_noise_static_key(identity.private_key) == derive_noise_static_key(
        identity.private_key
    )