"""Internet relay protocol (v2): what travels between phones and a relay server.

Two phones on mobile data can't connect to each other (carrier NAT), so over
the internet they meet at a relay — a small server the user runs (e.g. on the
WireGuard box). The relay is treated as untrusted. Layers, outside in:

1. **TLS 1.3 to the relay, certificate pinned.** The relay code carries the
   SHA-256 of the relay's certificate, so nobody can impersonate the relay,
   and the network only sees TLS. The code also carries an access key, so
   strangers can't use the relay.
2. **Mailboxes, not identities.** A phone subscribes to mailbox tokens
   derived from a secret shared only with each contact
   (:func:`mailbox_preimage`), rotating daily. The relay never learns names,
   identity hashes or keys, and can't tell which contacts talk to each other
   beyond what IP addresses and timing reveal.
3. **Pair box.** Every packet is encrypted again with a key derived from the
   pair secret (:class:`PairBox`), padded to fixed size buckets, and checked
   against replays. The relay sees opaque, similar-sized blobs; it can't read
   message types, inject packets or replay them.
4. Inside: the normal SRLTCP session (signed handshake with forward secrecy,
   AES-GCM, replay window).
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import struct
import time
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

RELAY_SCHEME = "srltcp-relay://"
RELAY_VERSION = 2
DEFAULT_RELAY_PORT = 7827  # next to the peer (7825) and discovery (7826) ports

# Relay wire messages (first byte of each frame inside TLS).
R_HELLO = 0x01  # client -> relay: version(1) + access_key(16)
R_WELCOME = 0x02  # relay -> client: version(1)
R_SUB = 0x03  # client -> relay: n * preimage(32)  (replaces the whole set)
R_SEND = 0x04  # client -> relay: token(16) + payload
R_DELIVER = 0x05  # relay -> client: token(16) + payload
R_NACK = 0x06  # relay -> client: token(16)  — nobody listening on that mailbox
R_PING = 0x07
R_PONG = 0x08
R_ERROR = 0x09  # relay -> client: code(1) + utf-8 text

TOKEN_SIZE = 16
PREIMAGE_SIZE = 32
ACCESS_KEY_SIZE = 16
MAX_RELAY_FRAME = 256 * 1024  # largest frame the relay accepts
MAX_SUBSCRIPTIONS = 1024  # mailbox tokens per connection
DAY = 86400

_HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")


class RelayCodeError(ValueError):
    pass


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError) as exc:
        raise RelayCodeError("bad encoding") from exc


@dataclass(frozen=True)
class RelayCode:
    """Everything needed to use a relay: where it is, its certificate pin and
    the access key. Shared out of band (pasted once) or with contacts inside
    the encrypted session — never trusted from the network."""

    host: str
    port: int
    cert_sha256: bytes
    access_key: bytes

    def encode(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return (
            f"{RELAY_SCHEME}{host}:{self.port}#{_b64e(self.cert_sha256)}.{_b64e(self.access_key)}"
        )

    @property
    def relay_id(self) -> str:
        """Stable short id (doesn't reveal the access key)."""
        return hashlib.sha256(b"srltcp-relay-id" + self.cert_sha256).hexdigest()[:16]

    @property
    def display(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"

    @classmethod
    def parse(cls, text: str) -> RelayCode:
        text = (text or "").strip()
        if not text.startswith(RELAY_SCHEME) or len(text) > 512:
            raise RelayCodeError("not a relay code (it starts with srltcp-relay://)")
        rest = text[len(RELAY_SCHEME) :]
        if "#" not in rest:
            raise RelayCodeError("relay code is incomplete")
        addr, secret = rest.split("#", 1)
        addr = addr.rstrip("/")
        if addr.startswith("["):
            if "]:" not in addr:
                raise RelayCodeError("bad relay address")
            host, port_s = addr[1:].split("]:", 1)
            if not re.fullmatch(r"[0-9A-Fa-f:.]{2,45}", host):
                raise RelayCodeError("bad relay address")
        else:
            if addr.count(":") != 1:
                raise RelayCodeError("bad relay address")
            host, port_s = addr.split(":", 1)
            if not _HOST_RE.fullmatch(host) or host.startswith(("-", ".")):
                raise RelayCodeError("bad relay host")
        try:
            port = int(port_s)
        except ValueError as exc:
            raise RelayCodeError("bad relay port") from exc
        if not 1 <= port <= 65535:
            raise RelayCodeError("bad relay port")
        if secret.count(".") != 1:
            raise RelayCodeError("relay code is incomplete")
        pin_s, key_s = secret.split(".", 1)
        pin, key = _b64d(pin_s), _b64d(key_s)
        if len(pin) != 32 or len(key) != ACCESS_KEY_SIZE:
            raise RelayCodeError("relay code is damaged")
        return cls(host=host.lower(), port=port, cert_sha256=pin, access_key=key)


def relay_token(preimage: bytes) -> bytes:
    """What senders address and the relay routes on. Subscribing needs the
    preimage, which only the two contacts can compute."""
    return hashlib.sha256(b"srltcp-relay-token-v1" + preimage).digest()[:TOKEN_SIZE]


def _hkdf(secret: bytes, info: bytes, length: int = 32) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=b"srltcp-pair-v1", info=info).derive(
        secret
    )


def pair_secret(own_contribution: bytes, peer_contribution: bytes) -> bytes:
    """Both contacts contribute 32 random bytes inside the E2EE session; the
    secret is the same on both sides whatever order they arrive in."""
    low, high = sorted((own_contribution, peer_contribution))
    return hashlib.sha256(b"srltcp-pair-secret-v1" + low + high).digest()


def current_day(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // DAY)


def mailbox_preimage(secret: bytes, recipient_hash: str, day: int) -> bytes:
    """Mailbox for messages *to* ``recipient_hash`` from its contact, for one
    day. Rotates daily so the relay can't follow a mailbox over time."""
    return _hkdf(secret, b"mailbox|" + recipient_hash.encode() + b"|" + struct.pack(">q", day))


class PairBoxError(ValueError):
    pass


PAIRBOX_AAD = b"srltcp-pairbox-v1"
PAIRBOX_MAX_SKEW_MS = 10 * 60 * 1000  # sender/receiver clock difference allowed
PAIRBOX_WINDOW_MS = 30 * 1000  # how far behind the newest packet one may arrive
_PAD_SMALL = 256
_PAD_LARGE = 4096


def _padded_len(n: int) -> int:
    bucket = _PAD_SMALL if n <= 4096 else _PAD_LARGE
    return max(bucket, -(-n // bucket) * bucket)


class PairBox:
    """Second encryption layer for relayed packets between two contacts.

    ChaCha20-Poly1305 with one key per direction (a packet can't be reflected
    back to its sender), plaintext padded to 256-byte buckets (4 KiB above
    4 KiB) so sizes reveal little, and a timestamped nonce checked against a
    replay cache.
    """

    def __init__(self, secret: bytes, own_hash: str, peer_hash: str) -> None:
        self._send = ChaCha20Poly1305(_hkdf(secret, b"pairbox|" + own_hash.encode()))
        self._recv = ChaCha20Poly1305(_hkdf(secret, b"pairbox|" + peer_hash.encode()))
        self._seen: dict[bytes, int] = {}
        self._newest = 0

    def seal(self, plaintext: bytes) -> bytes:
        nonce = struct.pack(">Q", int(time.time() * 1000))[2:] + os.urandom(6)
        body = struct.pack(">I", len(plaintext)) + plaintext
        body += b"\x00" * (_padded_len(len(body)) - len(body))
        return nonce + self._send.encrypt(nonce, body, PAIRBOX_AAD)

    def open(self, packet: bytes, now_ms: int | None = None) -> bytes:
        if len(packet) < 12 + 16 + 4:
            raise PairBoxError("short packet")
        nonce, ct = packet[:12], packet[12:]
        ts = int.from_bytes(nonce[:6], "big")
        now = int(time.time() * 1000) if now_ms is None else now_ms
        if abs(now - ts) > PAIRBOX_MAX_SKEW_MS:
            raise PairBoxError("stale packet")
        # Packets come in order over one relay connection; allow a little
        # reordering (reconnects) and remember nonces only for that window.
        if ts < self._newest - PAIRBOX_WINDOW_MS:
            raise PairBoxError("stale packet")
        if nonce in self._seen:
            raise PairBoxError("replayed packet")
        try:
            body = self._recv.decrypt(nonce, ct, PAIRBOX_AAD)
        except Exception as exc:
            raise PairBoxError("not from this contact") from exc
        self._seen[nonce] = ts
        if ts > self._newest:
            self._newest = ts
        if len(self._seen) > 4096:
            floor = self._newest - PAIRBOX_WINDOW_MS
            self._seen = {k: v for k, v in self._seen.items() if v >= floor}
        (length,) = struct.unpack(">I", body[:4])
        if length > len(body) - 4:
            raise PairBoxError("bad length")
        return body[4 : 4 + length]
