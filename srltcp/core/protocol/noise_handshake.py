"""Noise Protocol Framework handshake (XX pattern) for optional peer links."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Literal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from noise.connection import Keypair, NoiseConnection

from srltcp.core.protocol.crypto import SessionKeys, derive_session_key

NOISE_PATTERN = b"Noise_XX_25519_ChaChaPoly_SHA256"
HANDSHAKE_PROTOCOL = "noise_xx"


def derive_noise_static_key(ed_private: Ed25519PrivateKey) -> bytes:
    """Derive a deterministic X25519 static key from an Ed25519 identity."""
    seed = ed_private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    raw = bytearray(hashlib.sha256(seed + b"srltcp-noise-static-v1").digest())
    raw[0] &= 248
    raw[31] &= 127
    raw[31] |= 64
    return bytes(raw)


@dataclass
class NoiseHandshakeSession:
    noise: NoiseConnection
    role: Literal["initiator", "responder"]
    step: int = 0
    created: float = field(default_factory=time.time)

    @classmethod
    def create(
        cls,
        ed_private: Ed25519PrivateKey,
        *,
        initiator: bool,
    ) -> NoiseHandshakeSession:
        noise = NoiseConnection.from_name(NOISE_PATTERN)
        role: Literal["initiator", "responder"]
        if initiator:
            noise.set_as_initiator()
            role = "initiator"
        else:
            noise.set_as_responder()
            role = "responder"
        static = derive_noise_static_key(ed_private)
        noise.set_keypair_from_private_bytes(Keypair.STATIC, static)
        noise.start_handshake()
        return cls(noise=noise, role=role)

    def write_message(self) -> bytes:
        payload = bytes(self.noise.write_message())
        self.step += 1
        return payload

    def read_message(self, payload: bytes) -> None:
        self.noise.read_message(payload)
        self.step += 1

    @property
    def complete(self) -> bool:
        return bool(self.noise.handshake_finished)

    def handshake_hash(self) -> bytes:
        if not self.complete:
            raise RuntimeError("noise handshake not complete")
        return bytes(self.noise.get_handshake_hash())

    def session_keys(self, *, initiator: bool | None = None) -> SessionKeys:
        """Derive AES-GCM session keys from the Noise split() cipher keys.

        The handshake hash alone is *not* secret (it is computable from the
        public transcript), so keys must come from the post-handshake cipher
        states, which are derived from the DH chaining key.
        """
        del initiator
        if not self.complete:
            raise RuntimeError("noise handshake not complete")
        proto = self.noise.noise_protocol
        enc_k = bytes(proto.cipher_state_encrypt.k)
        dec_k = bytes(proto.cipher_state_decrypt.k)
        handshake_hash = self.handshake_hash()
        salt = hashlib.sha256(handshake_hash + b"srltcp-noise-session-v2").digest()[:16]
        send_key = derive_session_key(enc_k, salt, b"srltcp-noise-v2-dir")
        recv_key = derive_session_key(dec_k, salt, b"srltcp-noise-v2-dir")
        return SessionKeys(send_key=send_key, recv_key=recv_key, salt=salt)


NOISE_BIND_CONTEXT = b"srltcp-noise-bind-v1"


def sign_noise_binding(ed_private: Ed25519PrivateKey, handshake_hash: bytes, role: str) -> bytes:
    """Bind an Ed25519 identity to a completed Noise session (channel binding)."""
    return ed_private.sign(NOISE_BIND_CONTEXT + role.encode() + handshake_hash)


def verify_noise_binding(
    ed_public: Ed25519PublicKey, signature: bytes, handshake_hash: bytes, role: str
) -> bool:
    try:
        ed_public.verify(signature, NOISE_BIND_CONTEXT + role.encode() + handshake_hash)
        return True
    except Exception:
        return False
