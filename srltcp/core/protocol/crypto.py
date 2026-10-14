"""Identity-based encryption using Ed25519 + X25519 + AES-GCM."""

from __future__ import annotations

import hashlib
import hmac
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


HANDSHAKE_SIG_CONTEXT = b"srltcp-hs-v6"
HANDSHAKE_MAX_SKEW = 600.0  # seconds; rejects replayed handshakes older than this
_SEND_INFO_LOW = b"srltcp-v3-low-to-high"
_SEND_INFO_HIGH = b"srltcp-v3-high-to-low"


def handshake_signed_bytes(
    ephemeral: bytes,
    timestamp: int,
    sender_hash: str,
    recipient_hash: str,
    kem: bytes = b"",
    pq_public: bytes = b"",
) -> bytes:
    """Bytes covered by both identity signatures (Ed25519 and ML-DSA).

    Naming both the sender and the intended recipient means a handshake seen
    by one node (or on the network) can't be replayed to a different node.
    ``kem`` is the ML-KEM material this side sent (the initiator's public
    key, or the responder's ciphertext) and ``pq_public`` the sender's ML-DSA
    key: signing them stops anyone swapping either in transit.
    """
    return (
        HANDSHAKE_SIG_CONTEXT
        + ephemeral
        + struct.pack(">Q", max(0, int(timestamp)))
        + sender_hash.encode("ascii")
        + b"|"
        + recipient_hash.encode("ascii")
        + hashlib.sha256(kem).digest()
        + hashlib.sha256(pq_public).digest()
    )


class KeyExchange:
    """Hybrid key exchange: ephemeral X25519 + ML-KEM-768, signed by the
    identity's Ed25519 and ML-DSA-65 keys.

    The initiator sends an ML-KEM public key (``start_kem``); the responder
    answers with a ciphertext (``respond_kem``). The session keys come from
    both secrets, so an attacker has to break X25519 *and* ML-KEM — a
    quantum computer that breaks X25519 (even years later, on recorded
    traffic) still learns nothing. Directions come from the two X25519 keys
    in a fixed (sorted) order, so both peers agree on them.
    """

    def __init__(self, ed_private: Ed25519PrivateKey, pq_key: Any = None) -> None:
        self._ed_private = ed_private
        self._pq_key = pq_key  # pq.DsaKey, or None (tests of the classic part)
        self._x_private = X25519PrivateKey.generate()
        self._x_public = self._x_private.public_key()
        self.timestamp = int(time.time())
        self.kem_sent = b""  # our public key (initiator) or ciphertext (responder)
        self._kem_private: Any = None
        self._kem_shared = b""
        self._kem_public = b""  # the initiator's public key, as both sides saw it

    @property
    def ephemeral_public(self) -> bytes:
        return self._x_public.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    @property
    def pq_public(self) -> bytes:
        return bytes(self._pq_key.public) if self._pq_key is not None else b""

    def start_kem(self) -> bytes:
        """Initiator: a fresh ML-KEM key pair for this handshake only."""
        from srltcp.core.protocol import pq

        self._kem_public, self._kem_private = pq.kem_keygen()
        self.kem_sent = self._kem_public
        return self.kem_sent

    def respond_kem(self, peer_public: bytes) -> bytes:
        """Responder: encapsulate a secret to the initiator's key."""
        from srltcp.core.protocol import pq

        self._kem_shared, self.kem_sent = pq.kem_encapsulate(peer_public)
        self._kem_public = peer_public
        return self.kem_sent

    def _signed(self, own_hash: str, peer_hash: str) -> bytes:
        return handshake_signed_bytes(
            self.ephemeral_public, self.timestamp, own_hash, peer_hash,
            self.kem_sent, self.pq_public,
        )

    def sign_ephemeral(self, own_hash: str, peer_hash: str) -> bytes:
        return self._ed_private.sign(self._signed(own_hash, peer_hash))

    def sign_pq(self, own_hash: str, peer_hash: str) -> bytes:
        if self._pq_key is None:
            return b""
        return bytes(self._pq_key.sign(self._signed(own_hash, peer_hash)))

    def complete(
        self,
        peer_ephemeral: bytes,
        peer_signature: bytes,
        peer_ed_public: Ed25519PublicKey,
        *,
        peer_timestamp: int,
        own_hash: str,
        peer_hash: str,
        peer_kem: bytes = b"",
        peer_pq_public: bytes = b"",
        peer_pq_signature: bytes = b"",
        require_pq: bool = True,
        now: float | None = None,
    ) -> SessionKeys:
        """Verify the peer's signatures and derive the session keys.

        With ``require_pq`` (the default) the ML-KEM exchange and the ML-DSA
        signature are mandatory: no silent fallback to classic-only keys.
        Raises ``ValueError`` / ``InvalidSignature`` on failure.
        """
        current = time.time() if now is None else now
        if abs(current - float(peer_timestamp)) > HANDSHAKE_MAX_SKEW:
            raise ValueError("handshake timestamp outside allowed window")
        if len(peer_ephemeral) != 32:
            raise ValueError("invalid ephemeral key length")
        own = self.ephemeral_public
        if peer_ephemeral == own:
            raise ValueError("reflected handshake")
        signed = handshake_signed_bytes(
            peer_ephemeral, peer_timestamp, peer_hash, own_hash, peer_kem, peer_pq_public
        )
        peer_ed_public.verify(peer_signature, signed)  # cheap check first
        if require_pq or peer_pq_public or peer_kem:
            from srltcp.core.protocol import pq

            if not pq.dsa_verify(peer_pq_public, signed, peer_pq_signature):
                raise ValueError("post-quantum signature invalid")
            if self._kem_private is not None:  # initiator: peer_kem is the ciphertext
                kem_shared = pq.kem_decapsulate(self._kem_private, peer_kem)
                kem_ciphertext = peer_kem
            elif self._kem_shared and peer_kem == self._kem_public:  # responder
                kem_shared, kem_ciphertext = self._kem_shared, self.kem_sent
            else:
                raise ValueError("post-quantum key exchange missing")
        else:
            kem_shared = kem_ciphertext = b""
        peer_x = X25519PublicKey.from_public_bytes(peer_ephemeral)
        shared = self._x_private.exchange(peer_x)
        if shared == b"\x00" * 32:
            raise ValueError("degenerate shared secret")
        low, high = sorted((own, peer_ephemeral))
        if kem_shared:
            salt = hashlib.sha256(
                b"srltcp-session-salt-v6" + low + high
                + hashlib.sha256(self._kem_public).digest()
                + hashlib.sha256(kem_ciphertext).digest()
            ).digest()[:16]
            secret = shared + kem_shared  # hybrid: both are needed
        else:
            salt = hashlib.sha256(b"srltcp-session-salt-v3" + low + high).digest()[:16]
            secret = shared
        low_to_high = derive_session_key(secret, salt, _SEND_INFO_LOW)
        high_to_low = derive_session_key(secret, salt, _SEND_INFO_HIGH)
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
        self.epoch = 0  # bumped on every re-key: data sealed before it is lost
        if keys:
            self.set_keys(keys)

    @property
    def ready(self) -> bool:
        return self._keys is not None

    def set_keys(self, keys: SessionKeys) -> None:
        self._keys = keys
        self._send_aes = AESGCM(keys.send_key)
        self._recv_aes = AESGCM(keys.recv_key)
        self.epoch += 1
        self._send_counter = 0
        self._recv_highest = -1
        self._recv_seen = set()

    def reserve(self) -> tuple[bytes, AESGCM]:
        """Take the next nonce together with the key it belongs to.

        Lets bulk data be encrypted in a worker thread: the pair is fixed
        here, on the event loop, so a re-key in between can never make two
        messages share a nonce under one key."""
        if not self._keys:
            raise RuntimeError("session keys not established")
        counter = self._send_counter
        if counter >= 2**63:
            raise RuntimeError("nonce space exhausted — rekey required")
        self._send_counter += 1
        return b"\x00\x00\x00\x00" + struct.pack(">Q", counter), self._send_aes

    def encrypt(self, plaintext: bytes, aad: bytes = b"") -> bytes:
        nonce, aes = self.reserve()
        return nonce + aes.encrypt(nonce, plaintext, aad)

    def lane_hint(self, *, outgoing: bool) -> bytes:
        """16-byte tag naming this session to the peer without revealing it
        to anyone else (derived from a session key). Extra Tor connections
        that carry file data for the session start with it."""
        if not self._keys:
            raise RuntimeError("session keys not established")
        key = self._keys.send_key if outgoing else self._keys.recv_key
        return hmac.new(key, b"srltcp-lane-hint-v1", hashlib.sha256).digest()[:16]

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

