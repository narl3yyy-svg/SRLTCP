"""Post-quantum primitives: ML-KEM-768 (FIPS 203) and ML-DSA-65 (FIPS 204).

Used alongside X25519 and Ed25519, never instead of them ("hybrid"): a
session key needs both the X25519 and the ML-KEM secret, and a handshake
needs both the Ed25519 and the ML-DSA signature. Breaking one algorithm,
with a quantum computer or a new attack, is not enough.

``cryptography`` provides both when it is new enough (desktop). The Android
build ships an older ``cryptography``, so there the pure-Python
``kyber-py`` / ``dilithium-py`` implementations are used. They produce
byte-identical keys and accept each other's ciphertexts and signatures (see
tests/test_pq.py). They are not constant-time, which matters little here:
every ML-KEM key pair is used for exactly one handshake, and ML-DSA signing
uses fresh randomness.
"""

from __future__ import annotations

from typing import Any

KEM_PUBLIC_SIZE = 1184
KEM_CIPHERTEXT_SIZE = 1088
DSA_PUBLIC_SIZE = 1952
DSA_SIGNATURE_SIZE = 3309
DSA_SEED_SIZE = 32
DSA_CONTEXT = b"srltcp-identity-v1"

try:  # cryptography >= 47
    from cryptography.hazmat.primitives.asymmetric import mldsa as _mldsa
    from cryptography.hazmat.primitives.asymmetric import mlkem as _mlkem

    _NATIVE = True
except ImportError:  # pragma: no cover - exercised on Android
    _NATIVE = False


def backend_name() -> str:
    return "cryptography" if _NATIVE else "pure-python"


# ── ML-KEM-768 ──


def kem_keygen() -> tuple[bytes, Any]:
    """Fresh key pair for one handshake: (public key, private handle)."""
    if _NATIVE:
        private = _mlkem.MLKEM768PrivateKey.generate()
        return private.public_key().public_bytes_raw(), private
    from kyber_py.ml_kem import ML_KEM_768

    ek, dk = ML_KEM_768.keygen()
    return ek, dk


def kem_encapsulate(public: bytes) -> tuple[bytes, bytes]:
    """(shared secret, ciphertext) for the holder of ``public``."""
    if len(public) != KEM_PUBLIC_SIZE:
        raise ValueError("bad ML-KEM public key")
    if _NATIVE:
        shared, ciphertext = _mlkem.MLKEM768PublicKey.from_public_bytes(public).encapsulate()
        return shared, ciphertext
    from kyber_py.ml_kem import ML_KEM_768

    shared, ciphertext = ML_KEM_768.encaps(public)
    return shared, ciphertext


def kem_decapsulate(private: Any, ciphertext: bytes) -> bytes:
    if len(ciphertext) != KEM_CIPHERTEXT_SIZE:
        raise ValueError("bad ML-KEM ciphertext")
    if _NATIVE:
        return bytes(private.decapsulate(ciphertext))
    from kyber_py.ml_kem import ML_KEM_768

    return bytes(ML_KEM_768.decaps(private, ciphertext))


# ── ML-DSA-65 ──


class DsaKey:
    """An ML-DSA-65 signing key, derived from a stored 32-byte seed."""

    def __init__(self, seed: bytes) -> None:
        if len(seed) != DSA_SEED_SIZE:
            raise ValueError("ML-DSA seed must be 32 bytes")
        self.seed = seed
        if _NATIVE:
            self._native = _mldsa.MLDSA65PrivateKey.from_seed_bytes(seed)
            self.public = self._native.public_key().public_bytes_raw()
        else:
            from dilithium_py.ml_dsa import ML_DSA_65

            self.public, self._sk = ML_DSA_65.key_derive(seed)

    def sign(self, message: bytes) -> bytes:
        if _NATIVE:
            return bytes(self._native.sign(message, DSA_CONTEXT))
        from dilithium_py.ml_dsa import ML_DSA_65

        return bytes(ML_DSA_65.sign(self._sk, message, ctx=DSA_CONTEXT))


def dsa_verify(public: bytes, message: bytes, signature: bytes) -> bool:
    if len(public) != DSA_PUBLIC_SIZE or len(signature) != DSA_SIGNATURE_SIZE:
        return False
    if _NATIVE:
        try:
            _mldsa.MLDSA65PublicKey.from_public_bytes(public).verify(
                signature, message, DSA_CONTEXT
            )
        except Exception:  # noqa: BLE001 - InvalidSignature or malformed key
            return False
        return True
    from dilithium_py.ml_dsa import ML_DSA_65

    try:
        return bool(ML_DSA_65.verify(public, message, signature, ctx=DSA_CONTEXT))
    except Exception:  # noqa: BLE001 - malformed input
        return False
