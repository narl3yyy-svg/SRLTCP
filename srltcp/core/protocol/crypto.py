"""Identity-based encryption using Ed25519 + X25519 + AES-GCM."""

from __future__ import annotations

import hashlib
import struct
import time
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


HANDSHAKE_SIG_CONTEXT = b"srltcp-hs-v4"
HANDSHAKE_MAX_SKEW = 600.0  # seconds; rejects replayed handshakes older than this
_SEND_INFO_LOW = b"srltcp-v3-low-to-high"
_SEND_INFO_HIGH = b"srltcp-v3-high-to-low"


def handshake_signed_bytes(
    ephemeral: bytes, timestamp: int, sender_hash: str, recipient_hash: str
) -> bytes:
    """Bytes covered by the identity signature in the default handshake.

    Naming both the sender and the intended recipient means a handshake seen
    by one node (or on the network) can't be replayed to a different node.
    """
    return (
        HANDSHAKE_SIG_CONTEXT
        + ephemeral
        + struct.pack(">Q", max(0, int(timestamp)))
        + sender_hash.encode("ascii")
        + b"|"
        + recipient_hash.encode("ascii")
    )


class KeyExchange:
    """Perform ephemeral X25519 key exchange signed by Ed25519 identity.

    Session keys are derived from the two ephemeral public keys in a fixed
    (sorted) order, so both peers agree on the keys regardless of which side
    initiated — this makes simultaneous dials safe.
    """

    def __init__(self, ed_private: Ed25519PrivateKey) -> None:
        self._ed_private = ed_private
        self._x_private = X25519PrivateKey.generate()
        self._x_public = self._x_private.public_key()
        self.timestamp = int(time.time())

    @property
    def ephemeral_public(self) -> bytes:
        return self._x_public.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    def sign_ephemeral(self, own_hash: str, peer_hash: str) -> bytes:
        return self._ed_private.sign(
            handshake_signed_bytes(self.ephemeral_public, self.timestamp, own_hash, peer_hash)
        )

    def complete(
        self,
        peer_ephemeral: bytes,
        peer_signature: bytes,
        peer_ed_public: Ed25519PublicKey,
        *,
        peer_timestamp: int,
        own_hash: str,
        peer_hash: str,
        initiator: bool | None = None,
        now: float | None = None,
    ) -> SessionKeys:
        """Verify the peer's signed ephemeral key and derive session keys.

        ``initiator`` is accepted for API compatibility but no longer affects
        key direction. Raises ``ValueError`` / ``InvalidSignature`` on failure.
        """
        del initiator
        current = time.time() if now is None else now
        if abs(current - float(peer_timestamp)) > HANDSHAKE_MAX_SKEW:
            raise ValueError("handshake timestamp outside allowed window")
        if len(peer_ephemeral) != 32:
            raise ValueError("invalid ephemeral key length")
        own = self.ephemeral_public
        if peer_ephemeral == own:
            raise ValueError("reflected handshake")
        peer_ed_public.verify(
            peer_signature,
            handshake_signed_bytes(peer_ephemeral, peer_timestamp, peer_hash, own_hash),
        )
        peer_x = X25519PublicKey.from_public_bytes(peer_ephemeral)
        shared = self._x_private.exchange(peer_x)
        if shared == b"\x00" * 32:
            raise ValueError("degenerate shared secret")
        low, high = sorted((own, peer_ephemeral))
        salt = hashlib.sha256(b"srltcp-session-salt-v3" + low + high).digest()[:16]
        low_to_high = derive_session_key(shared, salt, _SEND_INFO_LOW)
        high_to_low = derive_session_key(shared, salt, _SEND_INFO_HIGH)
        if own == low:
            return SessionKeys(send_key=low_to_high, recv_key=high_to_low, salt=salt)
        return SessionKeys(send_key=high_to_low, recv_key=low_to_high, salt=salt)


class ReplayError(ValueError):
    """Raised when a ciphertext's counter was already seen or is too old."""


REPLAY_WINDOW = 4096


class CryptoBox:
    """AES-GCM with separate send/recv keys, counter nonces and replay protection.

    Nonce layout: 4 zero bytes + 64-bit big-endian message counter. The
    receiver keeps a sliding window so out-of-order delivery (concurrent file
    chunk processing) is accepted while duplicates and stale packets are not.
    """

    def __init__(self, keys: SessionKeys | None = None) -> None:
        self._keys: SessionKeys | None = None
        self._send_counter = 0
        self._recv_highest = -1
        self._recv_seen: set[int] = set()
        if keys:
            self.set_keys(keys)

    @property
    def ready(self) -> bool:
        return self._keys is not None

    def set_keys(self, keys: SessionKeys) -> None:
        self._keys = keys
        self._send_aes = AESGCM(keys.send_key)
        self._recv_aes = AESGCM(keys.recv_key)
        self._send_counter = 0
        self._recv_highest = -1
        self._recv_seen = set()

    def encrypt(self, plaintext: bytes, aad: bytes = b"") -> bytes:
        if not self._keys:
            raise RuntimeError("session keys not established")
        counter = self._send_counter
        if counter >= 2**63:
            raise RuntimeError("nonce space exhausted — rekey required")
        self._send_counter += 1
        nonce = b"\x00\x00\x00\x00" + struct.pack(">Q", counter)
        return nonce + self._send_aes.encrypt(nonce, plaintext, aad)

    def decrypt(self, ciphertext: bytes, aad: bytes = b"") -> bytes:
        if not self._keys:
            raise RuntimeError("session keys not established")
        if len(ciphertext) < NONCE_SIZE + 16:
            raise ValueError("ciphertext too short")
        nonce = ciphertext[:NONCE_SIZE]
        if nonce[:4] != b"\x00\x00\x00\x00":
            raise ValueError("invalid nonce")
        counter = struct.unpack(">Q", nonce[4:])[0]
        if counter <= self._recv_highest - REPLAY_WINDOW or counter in self._recv_seen:
            raise ReplayError("replayed or stale message")
        plaintext = self._recv_aes.decrypt(nonce, ciphertext[NONCE_SIZE:], aad)
        # Only record the counter after authentication succeeds.
        self._recv_seen.add(counter)
        if counter > self._recv_highest:
            self._recv_highest = counter
            floor = counter - REPLAY_WINDOW
            if len(self._recv_seen) > REPLAY_WINDOW * 2:
                self._recv_seen = {c for c in self._recv_seen if c > floor}
        return plaintext


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