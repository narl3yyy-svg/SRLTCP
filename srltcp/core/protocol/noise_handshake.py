"""Noise Protocol Framework handshake (XX pattern) for optional peer links."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
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

    @classmethod
    def create(
        cls,
        ed_private: Ed25519PrivateKey,
        *,
        initiator: bool,
    ) -> NoiseHandshakeSession:
        noise = NoiseConnection.from_name(NOISE_PATTERN)
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

    def session_keys(self, *, initiator: bool) -> SessionKeys:
        if not self.complete:
            raise RuntimeError("noise handshake not complete")
        handshake_hash = self.noise.get_handshake_hash()
        salt = hashlib.sha256(handshake_hash + b"srltcp-noise-session-v1").digest()[:16]
        send_info = b"srltcp-noise-send"
        recv_info = b"srltcp-noise-recv"
        if initiator:
            send_key = derive_session_key(handshake_hash, salt, send_info)
            recv_key = derive_session_key(handshake_hash, salt, recv_info)
        else:
            recv_key = derive_session_key(handshake_hash, salt, send_info)
            send_key = derive_session_key(handshake_hash, salt, recv_info)
        return SessionKeys(send_key=send_key, recv_key=recv_key, salt=salt)