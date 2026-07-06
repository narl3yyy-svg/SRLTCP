"""Ed25519 signing helpers for signed JSON payloads."""

from __future__ import annotations

from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from srltcp.core.protocol.crypto import (
    identity_hash,
    load_public_key,
    sign_bytes,
    verify_bytes,
)
from srltcp.core.protocol.messages import encode_payload


def unsigned_payload(data: dict[str, Any], signature_field: str) -> dict[str, Any]:
    return {k: v for k, v in data.items() if k != signature_field}


def sign_payload_dict(
    payload: dict[str, Any],
    private_key: Ed25519PrivateKey,
    *,
    signature_field: str = "signature",
) -> dict[str, Any]:
    body = encode_payload(payload)
    signature = sign_bytes(private_key, body).hex()
    return {**payload, signature_field: signature}


def sign_payload(
    payload: dict[str, Any],
    private_key: Ed25519PrivateKey,
    *,
    signature_field: str = "signature",
) -> bytes:
    return encode_payload(sign_payload_dict(payload, private_key, signature_field=signature_field))


def verify_identity_binding(hash_id: str, public_key_hex: str) -> Ed25519PublicKey | None:
    if len(hash_id) != 32 or not public_key_hex:
        return None
    try:
        pub = bytes.fromhex(public_key_hex)
        if identity_hash(pub) != hash_id:
            return None
        return load_public_key(pub)
    except (ValueError, TypeError):
        return None


def verify_signed_payload(
    data: dict[str, Any],
    *,
    signature_field: str = "signature",
    require_identity_match: bool = True,
) -> bool:
    hash_id = str(data.get("hash_id", ""))
    pub_hex = str(data.get("public_key", ""))
    sig_hex = str(data.get(signature_field, ""))
    if not sig_hex:
        return False
    public_key: Ed25519PublicKey | None
    if require_identity_match:
        public_key = verify_identity_binding(hash_id, pub_hex)
        if not public_key:
            return False
    else:
        try:
            public_key = load_public_key(bytes.fromhex(pub_hex))
        except (ValueError, TypeError):
            return False
    try:
        payload = encode_payload(unsigned_payload(data, signature_field))
        return verify_bytes(public_key, bytes.fromhex(sig_hex), payload)
    except (ValueError, TypeError):
        return False