"""Serial radio privacy and session recovery: private beacons only contacts
can read, nobody discovered over the air, both sides always agree."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import srltcp.core  # noqa: F401  (import order: core before transports)
from srltcp.core.messaging import radio
from srltcp.core.messaging.backend import MessagingBackend
from srltcp.core.protocol import beacon
from srltcp.core.trusted import TrustedPeer
from srltcp.transports import android_serial
from tests.test_android_serial import AirBridge
from tests.test_e2e_nodes import make_node, wait_for

PORTS = ["usb:0403:6015#0", "usb:303a:1001#0"]


def _pub(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


# ── Beacon format ──


def test_pair_key_is_the_same_on_both_sides_and_private() -> None:
    a, b, c = (Ed25519PrivateKey.generate() for _ in range(3))
    assert beacon.pair_key(a, _pub(b)) == beacon.pair_key(b, _pub(a))
    assert beacon.pair_key(a, _pub(b)) != beacon.pair_key(c, _pub(b))


def test_beacon_names_only_the_contacts_it_was_built_for() -> None:
    a, b, c = (Ed25519PrivateKey.generate() for _ in range(3))
    ab, ac = beacon.pair_key(a, _pub(b)), beacon.pair_key(a, _pub(c))
    heard = beacon.parse(beacon.build([ab]))
    assert heard is not None
    assert heard.names(beacon.pair_key(b, _pub(a)))
    assert not heard.names(ac)


def test_beacon_hides_identity_and_contact_count() -> None:
    a, b = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    one = beacon.build([beacon.pair_key(a, _pub(b))])
    none = beacon.build([], reply=True)
    assert len(one) == len(none)  # padded with random filler tags
    assert _pub(a) not in one and _pub(a).hex().encode() not in one
    assert beacon.build([beacon.pair_key(a, _pub(b))]) != one  # fresh nonce every time


def test_stale_or_malformed_beacons_are_ignored() -> None:
    old = beacon.build([], now=time.time() - beacon.MAX_AGE - 5)
    assert beacon.parse(old) is None
    assert beacon.parse(b"\x01\x00" + b"\x00" * 10) is None
    assert beacon.parse(beacon.build([])[:-3]) is None


# ── Two phones over a lossy radio link ──


@pytest.fixture
async def radios(tmp_path: Any, monkeypatch: Any) -> AsyncIterator[tuple[MessagingBackend, ...]]:
    air = AirBridge(PORTS, loss=0.03)
    monkeypatch.setattr(android_serial, "_bridge", lambda: air)
    nodes = tuple(
        make_node(tmp_path, name, enable_serial=True, serial_port=port, serial_baud=57600)
        for name, port in zip(("alice", "bob"), PORTS, strict=True)
    )
    for node in nodes:
        await node.start()
    try:
        yield nodes
    finally:
        for node in nodes:
            await node.stop()


def _befriend(x: MessagingBackend, y: MessagingBackend) -> None:
    ident = y.identities["tcp"]
    x.trusted.add(TrustedPeer(hash_id=ident.hash_id, name=ident.name,
                              public_key=ident.public_bytes().hex()))


def _on_radio(x: MessagingBackend, y: MessagingBackend) -> bool:
    link = x.get_link(y.identities["tcp"].hash_id)
    return bool(link and link.handshake_complete and link.transport == "serial")


async def test_strangers_are_not_discovered_and_get_no_answer(
    radios: tuple[MessagingBackend, MessagingBackend],
) -> None:
    a, b = radios
    _befriend(a, b)  # a has b as a contact; b doesn't know a
    assert await a.send_radio_beacon()
    assert await wait_for(lambda: b.serial_transport is not None
                          and b.serial_transport.link_stats()["messages_in"] > 0, timeout=10)
    await wait_for(lambda: False, timeout=1.0)
    assert b.discovery.list_peers() == []
    assert b._radio_heard == {} and not b.get_link(a.identities["tcp"].hash_id)
    assert b.serial_transport is not None
    assert b.serial_transport.link_stats()["messages_out"] == 0  # not even an answer
    assert not _on_radio(a, b)


async def test_a_side_that_lost_its_session_gets_it_back(
    radios: tuple[MessagingBackend, MessagingBackend], monkeypatch: Any
) -> None:
    """One phone dropped the session (restart, a lost handshake answer) while
    the other still shows it: the next beacon brings both back."""
    monkeypatch.setattr(radio, "RADIO_SESSION_SETTLE", 0.0)
    a, b = radios
    _befriend(a, b)
    _befriend(b, a)
    assert await a.send_radio_beacon()
    assert await wait_for(lambda: _on_radio(a, b) and _on_radio(b, a), timeout=60)
    assert await wait_for(lambda: not a._radio_dialing and not b._radio_dialing)
    b.remove_link(a.identities["tcp"].hash_id)
    assert _on_radio(a, b) and not _on_radio(b, a)
    await b.radio_tick(now_open=True)
    assert await wait_for(lambda: _on_radio(a, b) and _on_radio(b, a), timeout=60)
    await b.send_message(a.identities["tcp"].hash_id, "back on the air")
    assert await wait_for(
        lambda: any(m["text"] == "back on the air" for m in a.get_messages()), timeout=30)


async def test_the_other_side_dials_when_the_first_one_does_not(
    radios: tuple[MessagingBackend, MessagingBackend], monkeypatch: Any
) -> None:
    monkeypatch.setattr(radio, "RADIO_DIAL_GRACE", 0.5)
    a, b = radios
    _befriend(a, b)
    _befriend(b, a)
    low, high = sorted(radios, key=lambda n: n.identities["tcp"].hash_id)
    monkeypatch.setattr(low, "_maybe_dial_radio", lambda hash_id: None)  # never dials
    assert await low.send_radio_beacon()
    assert await wait_for(lambda: bool(high._radio_waiting), timeout=10)
    await wait_for(lambda: False, timeout=0.6)
    await high.radio_tick()
    assert await wait_for(lambda: _on_radio(a, b) and _on_radio(b, a), timeout=60)


async def test_a_busy_radio_session_is_not_reaped(
    radios: tuple[MessagingBackend, MessagingBackend],
) -> None:
    a, b = radios
    _befriend(a, b)
    _befriend(b, a)
    assert await a.send_radio_beacon()
    assert await wait_for(lambda: _on_radio(a, b) and _on_radio(b, a), timeout=60)
    link = a.get_link(b.identities["tcp"].hash_id)
    assert link is not None
    link.last_seen -= 120  # no pong for two minutes, but packets keep arriving
    assert a.radio_alive()
    assert await a.reap_dead_links(30.0) == []
    assert _on_radio(a, b)


async def test_tor_never_replaces_a_working_radio_session(
    radios: tuple[MessagingBackend, MessagingBackend],
) -> None:
    a, b = radios
    _befriend(a, b)
    _befriend(b, a)
    assert await a.send_radio_beacon()
    assert await wait_for(lambda: _on_radio(a, b) and _on_radio(b, a), timeout=60)
    assert a._direct_alive(b.identities["tcp"].hash_id)
