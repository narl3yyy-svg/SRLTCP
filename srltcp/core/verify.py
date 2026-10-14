"""Safety numbers: check once that a contact's keys really are theirs.

Each side's *fingerprint* covers both of its identity keys: Ed25519 and the
hash of its ML-DSA-65 (post-quantum) key. A pair's safety number is computed
from the two fingerprints in a fixed order, so both phones show the same 60
digits. If they match (read out, or one phone scans the other's code), nobody
swapped keys between the two: to fake a match an attacker would have to
break SHA-512 or forge both signature schemes.

Pairing in person (scanning the other phone's QR code, or an NFC tap) does
this check by itself: the code carries both key fingerprints.
"""

from __future__ import annotations

import hashlib
import hmac

CONTEXT = b"srltcp-safety-v1"
CODE_PREFIX = "srltcp://verify#"
GROUPS = 12  # of 5 digits


def fingerprint(ed_public: bytes, pq_hash: bytes) -> bytes:
    """One side's keys: Ed25519 public key + SHA-256 of its ML-DSA public key."""
    if len(ed_public) != 32 or len(pq_hash) != 32:
        raise ValueError("bad key fingerprint input")
    return hashlib.sha256(CONTEXT + ed_public + pq_hash).digest()


def pq_hash(pq_public: bytes) -> bytes:
    return hashlib.sha256(pq_public).digest()


def safety_digest(a: bytes, b: bytes) -> bytes:
    lo, hi = sorted((a, b))
    return hashlib.sha512(CONTEXT + lo + hi).digest()


def safety_digits(digest: bytes) -> str:
    """60 digits in groups of five (each group from 40 bits of the digest)."""
    return " ".join(
        f"{int.from_bytes(digest[i * 5:(i + 1) * 5], 'big') % 100000:05d}" for i in range(GROUPS)
    )


def verify_code(owner_hash: str, contact_hash: str, digest: bytes) -> str:
    """What the verify QR code shows: whose code it is, which contact it is
    for, and the number."""
    return f"{CODE_PREFIX}{owner_hash}.{contact_hash}.{safety_digits(digest).replace(' ', '')}"


def parse_verify_code(text: str) -> tuple[str, str, str] | None:
    """(owner hash, contact hash, digits) from a scanned verify code, or None."""
    text = (text or "").strip()
    if not text.startswith(CODE_PREFIX):
        return None
    parts = text[len(CODE_PREFIX):].split(".")
    if len(parts) != 3:
        return None
    owner, contact, digits = parts
    if (
        len(owner) != 32 or len(contact) != 32 or len(digits) != GROUPS * 5
        or not digits.isdigit()
    ):
        return None
    return owner.lower(), contact.lower(), digits


def digits_match(digest: bytes, digits: str) -> bool:
    expected = safety_digits(digest).replace(" ", "")
    return hmac.compare_digest(expected, "".join(ch for ch in digits if ch.isdigit()))
