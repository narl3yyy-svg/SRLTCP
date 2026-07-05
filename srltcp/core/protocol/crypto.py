"""Identity-based encryption using Ed25519 + X25519 + AES-GCM."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

NONCE_SIZE = 12
KEY_SIZE = 32


def identity_hash(public_key_bytes: bytes) -> str:
    """Reticulum-style truncated SHA-256 hash identifier (hex, 32 chars)."""
    digest = hashlib.sha256(public_key_bytes).hexdigest()
    return digest[:32]


def derive_session_key(shared_secret: bytes, salt: bytes, info: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_SIZE,
        salt=salt,
        info=info,
    ).derive(shared_secret)


@dataclass
class SessionKeys:
    send_key: bytes
    recv_key: bytes
    salt: bytes


class KeyExchange:
    """Perform ephemeral X25519 key exchange signed by Ed25519 identity."""

    def __init__(self, ed_private: Ed25519PrivateKey) -> None:
        self._ed_private = ed_private
        self._x_private = X25519PrivateKey.generate()
        self._x_public = self._x_private.public_key()

    @property
    def ephemeral_public(self) -> bytes:
        return self._x_public.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    def sign_ephemeral(self) -> bytes:
        return self._ed_private.sign(self.ephemeral_public)

    def complete(
        self,
        peer_ephemeral: bytes,
        peer_signature: bytes,
        peer_ed_public: Ed25519PublicKey,
        *,
        initiator: bool,
    ) -> SessionKeys:
        peer_ed_public.verify(peer_signature, peer_ephemeral)
        peer_x = X25519PublicKey.from_public_bytes(peer_ephemeral)
        shared = self._x_private.exchange(peer_x)
        # Deterministic salt so both peers derive identical session keys
        salt = hashlib.sha256(shared + b"srltcp-session-salt-v2").digest()[:16]
        send_info = b"srltcp-v2-send"
        recv_info = b"srltcp-v2-recv"
        if initiator:
            send_key = derive_session_key(shared, salt, send_info)
            recv_key = derive_session_key(shared, salt, recv_info)
        else:
            recv_key = derive_session_key(shared, salt, send_info)
            send_key = derive_session_key(shared, salt, recv_info)
        return SessionKeys(send_key=send_key, recv_key=recv_key, salt=salt)


class CryptoBox:
    """AES-GCM encrypt/decrypt with separate send/recv keys."""

    def __init__(self, keys: SessionKeys | None = None) -> None:
        self._keys = keys

    @property
    def ready(self) -> bool:
        return self._keys is not None

    def set_keys(self, keys: SessionKeys) -> None:
        self._keys = keys

    def encrypt(self, plaintext: bytes, aad: bytes = b"") -> bytes:
        if not self._keys:
            raise RuntimeError("session keys not established")
        nonce = os.urandom(NONCE_SIZE)
        aes = AESGCM(self._keys.send_key)
        ct = aes.encrypt(nonce, plaintext, aad)
        return nonce + ct

    def decrypt(self, ciphertext: bytes, aad: bytes = b"") -> bytes:
        if not self._keys:
            raise RuntimeError("session keys not established")
        nonce = ciphertext[:NONCE_SIZE]
        ct = ciphertext[NONCE_SIZE:]
        aes = AESGCM(self._keys.recv_key)
        return aes.decrypt(nonce, ct, aad)


def _hash_to_token(hash_id: str) -> bytes:
    cleaned = hash_id.strip().lower()
    if len(cleaned) != 32 or any(c not in "0123456789abcdef" for c in cleaned):
        raise ValueError("hash_id must be 32 lowercase hex chars")
    return bytes.fromhex(cleaned)


def relay_wrap(
    inner_payload: bytes,
    dest_hash: str,
    *,
    src_hash: str | None = None,
) -> bytes:
    """
    Wrap an E2EE payload for hub forwarding.
    Hub sees only routing tokens (16-byte hash prefixes) + opaque blob.
    """
    dest = _hash_to_token(dest_hash)
    if src_hash:
        src = _hash_to_token(src_hash)
        return dest + src + inner_payload
    return dest + inner_payload


def relay_unwrap(wrapped: bytes) -> tuple[str, str | None, bytes]:
    if len(wrapped) < 16:
        raise ValueError("relay envelope too short")
    dest_hash = wrapped[:16].hex()
    if len(wrapped) >= 32:
        src_hash = wrapped[16:32].hex()
        return dest_hash, src_hash, wrapped[32:]
    return dest_hash, None, wrapped[16:]


def sign_bytes(private_key: Ed25519PrivateKey, data: bytes) -> bytes:
    return private_key.sign(data)


def verify_bytes(public_key: Ed25519PublicKey, signature: bytes, data: bytes) -> bool:
    try:
        public_key.verify(signature, data)
        return True
    except Exception:
        return False


def public_key_bytes(public_key: Ed25519PublicKey) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def load_public_key(raw: bytes) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(raw)


def keys_to_dict(keys: SessionKeys) -> dict[str, Any]:
    return {
        "send_key": keys.send_key.hex(),
        "recv_key": keys.recv_key.hex(),
        "salt": keys.salt.hex(),
    }