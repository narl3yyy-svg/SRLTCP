"""Hybrid post-quantum handshake: X25519 + ML-KEM-768, Ed25519 + ML-DSA-65."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature

from srltcp.core.identity import Identity, IdentityStore
from srltcp.core.protocol import pq
from srltcp.core.protocol.crypto import CryptoBox, KeyExchange, SessionKeys


def handshake(
    a: Identity, b: Identity, *, tamper: str = ""
) -> tuple[SessionKeys, SessionKeys]:
    """a initiates, b responds; returns (a's keys, b's keys)."""
    a_kx = KeyExchange(a.private_key, a.pq)
    ek = a_kx.start_kem()
    b_kx = KeyExchange(b.private_key, b.pq)
    # Responder checks the initiator's hello.
    b_kx.respond_kem(ek)
    b_keys = b_kx.complete(
        a_kx.ephemeral_public, a_kx.sign_ephemeral(a.hash_id, b.hash_id), a.public_key,
        peer_timestamp=a_kx.timestamp, own_hash=b.hash_id, peer_hash=a.hash_id,
        peer_kem=ek, peer_pq_public=a.pq.public,
        peer_pq_signature=a_kx.sign_pq(a.hash_id, b.hash_id),
    )
    ct = b_kx.kem_sent
    pq_public = b.pq.public
    pq_sig = b_kx.sign_pq(b.hash_id, a.hash_id)
    if tamper == "ciphertext":
        ct = bytes([ct[0] ^ 1]) + ct[1:]
    elif tamper == "pq_key":  # attacker swaps in their own ML-DSA key and signature
        mallory = Identity.generate("mallory", "tcp")
        pq_public = mallory.pq.public
        pq_sig = mallory.pq.sign(b"anything")
    a_keys = a_kx.complete(
        b_kx.ephemeral_public, b_kx.sign_ephemeral(b.hash_id, a.hash_id), b.public_key,
        peer_timestamp=b_kx.timestamp, own_hash=a.hash_id, peer_hash=b.hash_id,
        peer_kem=ct, peer_pq_public=pq_public, peer_pq_signature=pq_sig,
    )
    return a_keys, b_keys


def test_hybrid_keys_agree_and_encrypt() -> None:
    alice, bob = Identity.generate("alice", "tcp"), Identity.generate("bob", "tcp")
    a_keys, b_keys = handshake(alice, bob)
    assert a_keys.send_key == b_keys.recv_key and a_keys.recv_key == b_keys.send_key
    assert CryptoBox(b_keys).decrypt(CryptoBox(a_keys).encrypt(b"hi")) == b"hi"


def test_tampered_kem_ciphertext_is_rejected() -> None:
    alice, bob = Identity.generate("alice", "tcp"), Identity.generate("bob", "tcp")
    with pytest.raises(InvalidSignature):  # the ciphertext is covered by the signature
        handshake(alice, bob, tamper="ciphertext")


def test_swapped_post_quantum_key_is_rejected() -> None:
    alice, bob = Identity.generate("alice", "tcp"), Identity.generate("bob", "tcp")
    with pytest.raises(InvalidSignature):
        handshake(alice, bob, tamper="pq_key")


def test_classic_only_handshake_is_refused() -> None:
    """No silent downgrade: without ML-KEM / ML-DSA the handshake fails."""
    alice, bob = Identity.generate("alice", "tcp"), Identity.generate("bob", "tcp")
    a_kx, b_kx = KeyExchange(alice.private_key), KeyExchange(bob.private_key)
    with pytest.raises(ValueError):
        b_kx.complete(
            a_kx.ephemeral_public, a_kx.sign_ephemeral(alice.hash_id, bob.hash_id),
            alice.public_key, peer_timestamp=a_kx.timestamp,
            own_hash=bob.hash_id, peer_hash=alice.hash_id,
        )


def test_handshake_is_bound_to_its_recipient() -> None:
    alice, bob, carol = (Identity.generate(n, "tcp") for n in ("alice", "bob", "carol"))
    a_kx = KeyExchange(alice.private_key, alice.pq)
    ek = a_kx.start_kem()
    c_kx = KeyExchange(carol.private_key, carol.pq)
    c_kx.respond_kem(ek)
    with pytest.raises(InvalidSignature):
        c_kx.complete(
            a_kx.ephemeral_public, a_kx.sign_ephemeral(alice.hash_id, bob.hash_id),
            alice.public_key, peer_timestamp=a_kx.timestamp,
            own_hash=carol.hash_id, peer_hash=alice.hash_id, peer_kem=ek,
            peer_pq_public=alice.pq.public,
            peer_pq_signature=a_kx.sign_pq(alice.hash_id, bob.hash_id),
        )


def test_pure_python_and_native_interoperate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Android (pure Python) and desktop (cryptography) produce the same keys."""
    if not pq._NATIVE:
        pytest.skip("native ML-KEM/ML-DSA not available")
    seed = bytes(range(32))
    native = pq.DsaKey(seed)
    monkeypatch.setattr(pq, "_NATIVE", False)
    pure = pq.DsaKey(seed)
    assert pure.public == native.public
    sig = pure.sign(b"m")
    monkeypatch.setattr(pq, "_NATIVE", True)
    assert pq.dsa_verify(native.public, b"m", sig)
    ek, dk = pq.kem_keygen()  # native key pair
    monkeypatch.setattr(pq, "_NATIVE", False)
    shared, ct = pq.kem_encapsulate(ek)  # pure-Python encapsulation
    monkeypatch.setattr(pq, "_NATIVE", True)
    assert pq.kem_decapsulate(dk, ct) == shared


def test_old_identity_gets_a_post_quantum_key(tmp_path: Path) -> None:
    store = IdentityStore(tmp_path)
    old = Identity.generate("me", "tcp")
    path = tmp_path / "identities" / "identity_tcp.json"
    data = old.to_dict()
    data.pop("pq_seed")  # as written by 0.8
    path.write_text(json.dumps(data))
    loaded = store.load_or_create("me", "tcp")
    assert loaded.hash_id == old.hash_id
    assert len(loaded.pq.public) == pq.DSA_PUBLIC_SIZE
    again = store.load_or_create("me", "tcp")
    assert again.pq.public == loaded.pq.public  # saved, stable
    assert (path.stat().st_mode & 0o077) == 0  # owner-only


async def test_changed_post_quantum_key_is_refused(tmp_path: Path) -> None:
    """The contact's ML-DSA key is pinned at the first handshake; someone who
    later shows up with the right Ed25519 key but another ML-DSA key (or a
    quantum attacker forging Ed25519) is refused."""
    import secrets

    from tests.test_e2e_nodes import make_node, trust

    a, b = make_node(tmp_path, "alice"), make_node(tmp_path, "bob")
    await a.start()
    await b.start()
    trust(a, b)
    trust(b, a)
    try:
        bh, ah = b.identities["tcp"].hash_id, a.identities["tcp"].hash_id
        await a.connect_to_peer(bh)
        assert await a.wait_for_handshake(bh, timeout=8)
        assert a.trusted.get(bh).pq_key == b.identities["tcp"].pq.public.hex()
        await a.disconnect_peer(bh)
        await b.disconnect_peer(ah)
        ident = b.identities["tcp"]
        ident.pq_seed, ident._pq = secrets.token_bytes(32), None  # new PQ key, same Ed25519
        await a.connect_to_peer(bh)
        assert not await a.wait_for_handshake(bh, timeout=3)
    finally:
        await a.stop()
        await b.stop()
