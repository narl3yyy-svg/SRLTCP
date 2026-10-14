"""Verified contacts: safety numbers, in-person pairing, scanning a safety code."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from srltcp.core import verify
from srltcp.core.messaging.backend import MessagingBackend
from srltcp.core.messaging.pairing import PairingError, encode_card
from tests.test_e2e_nodes import make_node, trust, wait_for


def test_safety_number_is_the_same_on_both_sides_and_covers_both_keys() -> None:
    ed_a, ed_b, pq_a, pq_b = (os.urandom(32) for _ in range(4))
    fa, fb = verify.fingerprint(ed_a, pq_a), verify.fingerprint(ed_b, pq_b)
    number = verify.safety_digits(verify.safety_digest(fa, fb))
    assert number == verify.safety_digits(verify.safety_digest(fb, fa))
    groups = number.split(" ")
    assert len(groups) == 12 and all(len(g) == 5 and g.isdigit() for g in groups)
    # Another post-quantum key (or Ed25519 key) gives another number.
    other_pq = verify.safety_digest(fa, verify.fingerprint(ed_b, os.urandom(32)))
    other_ed = verify.safety_digest(fa, verify.fingerprint(os.urandom(32), pq_b))
    assert verify.safety_digits(other_pq) != number != verify.safety_digits(other_ed)


def test_verify_codes_parse_strictly() -> None:
    digest = verify.safety_digest(b"a" * 32, b"b" * 32)
    code = verify.verify_code("ab" * 16, "cd" * 16, digest)
    owner, contact, digits = verify.parse_verify_code(code) or ("", "", "")
    assert (owner, contact) == ("ab" * 16, "cd" * 16) and verify.digits_match(digest, digits)
    assert not verify.digits_match(verify.safety_digest(b"a" * 32, b"c" * 32), digits)
    for bad in ("", "srltcp://pair#x", code[:-1], code + "1", code.replace(".", ""),
                verify.CODE_PREFIX + "zz." + "cd" * 16 + "." + digits, code + ".1"):
        assert verify.parse_verify_code(bad) is None or bad == code


async def _pair(tmp_path: Path, *, shared: bool, in_person: bool):  # type: ignore[no-untyped-def]
    a = make_node(tmp_path, "alice")
    b = make_node(tmp_path, "bob")
    await a.start()
    await b.start()
    a._card_endpoints = lambda: [("127.0.0.1", a.config.tcp_port)]  # type: ignore[method-assign]
    uri = a.create_pairing_card()["uri"]
    if shared:
        a.mark_pairing_shared(uri)  # "Copy link" / "Share…"
    await b.import_pairing_card(uri, in_person=in_person)
    return a, b


async def test_scanning_a_qr_in_person_verifies_both_sides(tmp_path: Path) -> None:
    a, b = await _pair(tmp_path, shared=False, in_person=True)
    try:
        ah, bh = a.identities["tcp"].hash_id, b.identities["tcp"].hash_id
        # The scanner knows both of Alice's keys from her screen at once.
        assert b.contact_verified(ah)
        assert await wait_for(lambda: a.is_trusted(bh), timeout=10)
        # Alice: whoever redeemed her one-time token saw her screen.
        assert await wait_for(lambda: a.contact_verified(bh), timeout=10)
        assert a.safety_info(bh)["number"] == b.safety_info(ah)["number"]
        assert b.trusted_view(ah)["verified"] is True  # type: ignore[index]
        assert next(p for p in a.get_trusted_peers() if p["hash_id"] == bh)["verified"]
    finally:
        await a.stop()
        await b.stop()


async def test_shared_link_pairs_but_does_not_verify(tmp_path: Path) -> None:
    a, b = await _pair(tmp_path, shared=True, in_person=False)
    try:
        ah, bh = a.identities["tcp"].hash_id, b.identities["tcp"].hash_id
        assert await wait_for(lambda: a.is_trusted(bh), timeout=10)
        assert await wait_for(lambda: bool(b.trusted.get(ah).pq_key), timeout=10)  # type: ignore[union-attr]
        assert not a.contact_verified(bh) and not b.contact_verified(ah)
        # Bob scans the safety code on Alice's screen later.
        b.verify_contact(ah, code=a.safety_info(bh)["code"])
        assert b.contact_verified(ah)
        # Alice compares the numbers and confirms; then takes it back.
        a.verify_contact(bh, confirm=True)
        assert a.contact_verified(bh)
        a.unverify_contact(bh)
        assert not a.contact_verified(bh)
    finally:
        await a.stop()
        await b.stop()


async def test_wrong_safety_codes_are_refused(tmp_path: Path) -> None:
    a, b = make_node(tmp_path, "alice"), make_node(tmp_path, "bob")
    c = make_node(tmp_path, "carol")
    for node in (a, b, c):
        await node.start()
    try:
        trust(a, b)
        trust(b, a)
        trust(a, c)
        trust(c, a)
        ah, bh, ch = (n.identities["tcp"].hash_id for n in (a, b, c))
        with pytest.raises(PairingError, match="connect"):
            b.verify_contact(ah, confirm=True)  # no post-quantum key known yet
        await b.connect_to_peer(ah)
        await c.connect_to_peer(ah)
        assert await b.wait_for_handshake(ah, timeout=8)
        assert await c.wait_for_handshake(ah, timeout=8)
        with pytest.raises(PairingError, match="another contact"):
            b.verify_contact(ah, code=a.safety_info(ch)["code"])  # Alice's code for Carol
        with pytest.raises(PairingError, match="different contact"):
            b.verify_contact(ah, code=c.safety_info(ah)["code"])  # Carol's code
        owner, me, digits = verify.parse_verify_code(a.safety_info(bh)["code"]) or ("", "", "")
        tampered = f"{verify.CODE_PREFIX}{owner}.{me}.{digits[:-1]}{(int(digits[-1]) + 1) % 10}"
        with pytest.raises(PairingError, match="don't match"):
            b.verify_contact(ah, code=tampered)
        with pytest.raises(PairingError):
            b.verify_contact(ah)
        assert not b.contact_verified(ah)
    finally:
        for node in (a, b, c):
            await node.stop()


async def test_post_quantum_key_must_match_the_scanned_code(tmp_path: Path) -> None:
    """A code shows the fingerprint of Alice's ML-DSA key: a session with
    another ML-DSA key (someone holding Alice's Ed25519 key, say) is refused."""
    a, b = make_node(tmp_path, "alice"), make_node(tmp_path, "bob")
    await a.start()
    await b.start()
    try:
        ident = a.identities["tcp"]
        uri = encode_card(
            private_key=ident.private_key, public_key=ident.public_bytes(), name="alice",
            endpoints=[("127.0.0.1", a.config.tcp_port)], pq_hash=os.urandom(32),
        )
        await b.import_pairing_card(uri, connect=False, in_person=True)
        trust(a, b)
        await b.connect_to_peer(ident.hash_id)
        assert not await b.wait_for_handshake(ident.hash_id, timeout=3)
        assert not b.trusted.get(ident.hash_id).pq_key  # type: ignore[union-attr]
    finally:
        await a.stop()
        await b.stop()


def test_card_from_older_app_has_no_post_quantum_hash(tmp_path: Path) -> None:
    node: MessagingBackend = make_node(tmp_path, "alice")
    node.identities["tcp"] = node.identity_store.load_or_create("alice", "tcp")
    from srltcp.core.messaging.pairing import decode_card

    ident = node.identities["tcp"]
    old = encode_card(private_key=ident.private_key, public_key=ident.public_bytes(),
                      name="alice", endpoints=[])
    assert decode_card(old).pq_hash == b""
    new = decode_card(node.create_pairing_card()["uri"])
    assert new.pq_hash == verify.pq_hash(ident.pq.public)
