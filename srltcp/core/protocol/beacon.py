"""Private radio beacons: "a contact of yours is in range", readable only by
those contacts.

A serial radio is a broadcast medium: anyone in range with a matching radio
hears everything. A plain announce (name, ID, public key) would tell them who
is there. A beacon carries no identity at all: only a timestamp, a random
nonce and one short tag per contact we'd like to reach, computed with a key
that only we and that contact can compute (static X25519 between our Ed25519
identity keys, converted the standard way, as libsodium's
``crypto_sign_ed25519_*_to_curve25519``). To anyone else the tags are random
bytes; random filler tags hide how many contacts we have. A contact who finds
its tag knows we're in range and want a session (the session itself is the
normal signed, quantum-safe handshake).

Wire format (binary, sent unacknowledged on the radio)::

    version (1) | flags (1) | timestamp (8, big endian) | nonce (12) | tags (8 each)
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import struct
import time
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

VERSION = 1
FLAG_REPLY = 0x01  # answers a beacon (receivers never answer a reply)
HEAD = struct.Struct(">BBQ12s")
TAG_SIZE = 8
TAG_BLOCK = 8  # tags are padded to a multiple of this with random filler
MAX_TAGS = 64
MAX_AGE = 300  # seconds: older (or replayed) beacons are ignored
_LABEL = b"SRLTCP radio beacon v1"
_P = 2**255 - 19


def ed25519_to_x25519_private(key: Ed25519PrivateKey) -> X25519PrivateKey:
    seed = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    return X25519PrivateKey.from_private_bytes(hashlib.sha512(seed).digest()[:32])


def ed25519_to_x25519_public(public: bytes) -> X25519PublicKey:
    """Edwards y → Montgomery u = (1 + y) / (1 - y) mod p."""
    if len(public) != 32:
        raise ValueError("not an Ed25519 public key")
    y = int.from_bytes(public, "little") & ((1 << 255) - 1)
    if y >= _P or y == 1:
        raise ValueError("not an Ed25519 public key")
    u = (1 + y) * pow(1 - y, _P - 2, _P) % _P
    return X25519PublicKey.from_public_bytes(u.to_bytes(32, "little"))


def pair_key(own: Ed25519PrivateKey, their_public: bytes) -> bytes:
    """The key only this pair of identities can compute (same on both sides)."""
    shared = ed25519_to_x25519_private(own).exchange(ed25519_to_x25519_public(their_public))
    if not any(shared):
        raise ValueError("weak public key")
    return hashlib.sha256(_LABEL + shared).digest()


def _tag(key: bytes, head: bytes) -> bytes:
    return hmac.new(key, head, hashlib.sha256).digest()[:TAG_SIZE]


@dataclass(frozen=True)
class Beacon:
    timestamp: int
    reply: bool
    head: bytes  # what the tags authenticate
    tags: frozenset[bytes]

    def names(self, key: bytes) -> bool:
        """True if this beacon has the tag for the pair ``key``."""
        return _tag(key, self.head) in self.tags


def build(keys: list[bytes], *, reply: bool = False, now: float | None = None) -> bytes:
    """A beacon for the contacts whose pair keys are ``keys``."""
    head = HEAD.pack(
        VERSION, FLAG_REPLY if reply else 0, int(now if now is not None else time.time()),
        secrets.token_bytes(12),
    )
    tags = [_tag(k, head) for k in keys[:MAX_TAGS]]
    while not tags or len(tags) % TAG_BLOCK:
        tags.append(secrets.token_bytes(TAG_SIZE))
    secrets.SystemRandom().shuffle(tags)
    return head + b"".join(tags)


def parse(data: bytes, *, now: float | None = None) -> Beacon | None:
    """A fresh, well-formed beacon, else None."""
    if len(data) < HEAD.size + TAG_SIZE or (len(data) - HEAD.size) % TAG_SIZE:
        return None
    version, flags, ts, _nonce = HEAD.unpack_from(data)
    count = (len(data) - HEAD.size) // TAG_SIZE
    if version != VERSION or count > MAX_TAGS + TAG_BLOCK:
        return None
    if abs((now if now is not None else time.time()) - ts) > MAX_AGE:
        return None
    body = data[HEAD.size :]
    tags = frozenset(body[i : i + TAG_SIZE] for i in range(0, len(body), TAG_SIZE))
    return Beacon(ts, bool(flags & FLAG_REPLY), data[: HEAD.size], tags)
