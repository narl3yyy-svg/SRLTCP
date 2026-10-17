"""Serial radio privacy and session recovery: private beacons only contacts
can read, nobody discovered over the air, both sides always agree."""

from __future__ import annotations

import asyncio
import os
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
from srltcp.core.protocol.messages import MessageType, parse_header
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
def air(monkeypatch: Any) -> AirBridge:
    bridge = AirBridge(PORTS, loss=0.03)
    monkeypatch.setattr(android_serial, "_bridge", lambda: bridge)
    return bridge


@pytest.fixture
async def radios(tmp_path: Any, air: AirBridge) -> AsyncIterator[tuple[MessagingBackend, ...]]:
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
    return bool(link and link.up and link.transport == "serial")


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
    monkeypatch.setattr(radio, "RADIO_SESSION_SETTLE", 3.0)  # (120 s on a real radio)
    a, b = radios
    _befriend(a, b)
    _befriend(b, a)
    assert await a.send_radio_beacon()
    assert await wait_for(lambda: _on_radio(a, b) and _on_radio(b, a), timeout=60)
    assert await wait_for(lambda: not a._radio_dialing and not b._radio_dialing)
    await asyncio.sleep(3.5)  # the session is settled: a beacon naming us now is news
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


async def test_a_lost_handshake_answer_leaves_nobody_claiming_a_session(
    radios: tuple[MessagingBackend, MessagingBackend], monkeypatch: Any
) -> None:
    """The answering phone's reply is lost on the air: it must not show the
    contact as connected (that was one phone saying SERIAL, the other not);
    once the air clears, both connect."""
    from srltcp.core.messaging import connect

    monkeypatch.setattr(connect, "SERIAL_HANDSHAKE_TTL", 8.0)
    a, b = radios
    _befriend(a, b)
    _befriend(b, a)
    low, high = sorted(radios, key=lambda n: n.identities["tcp"].hash_id)
    real_send = high._send_raw
    lose_answers = True

    async def send(peer_id: str, transport: str, packet: bytes) -> None:
        if lose_answers and parse_header(packet)[0] == MessageType.HANDSHAKE_ACK:
            return  # lost on the air (given up after too many tries)
        await real_send(peer_id, transport, packet)

    monkeypatch.setattr(high, "_send_raw", send)
    assert await low.send_radio_beacon()
    lh = low.identities["tcp"].hash_id
    assert await wait_for(lambda: (link := high.get_link(lh)) is not None
                          and link.handshake_complete, timeout=30)
    assert not _on_radio(high, low) and not _on_radio(low, high)
    assert high.get_transport_status()["serial"]["peers"] == []
    lose_answers = False
    await wait_for(lambda: not low._radio_dialing, timeout=15)
    await low.radio_tick(now_open=True)
    assert await wait_for(lambda: _on_radio(a, b) and _on_radio(b, a), timeout=60)


async def test_the_radio_takes_over_from_an_internet_session(
    radios: tuple[MessagingBackend, MessagingBackend], monkeypatch: Any
) -> None:
    a, b = radios
    _befriend(a, b)
    _befriend(b, a)
    for node in radios:  # (loopback stands in for the internet here)
        monkeypatch.setattr(node, "_tcp_peer_on_lan", lambda peer_id: False)
    assert b.tcp_transport is not None
    bh = b.identities["tcp"].hash_id
    assert await a.connect_to_peer(bh, host="127.0.0.1", port=b.tcp_transport.port)
    assert await wait_for(lambda: (l1 := a.get_link(bh)) is not None and l1.up
                          and l1.transport == "tcp", timeout=15)
    assert await a.send_radio_beacon()  # internet isn't the radio: still wanted
    assert await wait_for(lambda: _on_radio(a, b) and _on_radio(b, a), timeout=60)
    await a.send_message(bh, "radio first")
    assert await wait_for(
        lambda: any(m["text"] == "radio first" for m in b.get_messages()), timeout=30)


async def test_the_same_wifi_takes_over_from_the_radio(
    radios: tuple[MessagingBackend, MessagingBackend], monkeypatch: Any
) -> None:
    a, b = radios
    _befriend(a, b)
    _befriend(b, a)
    assert await a.send_radio_beacon()
    assert await wait_for(lambda: _on_radio(a, b) and _on_radio(b, a), timeout=60)
    low, high = sorted(radios, key=lambda n: n.identities["tcp"].hash_id)
    assert high.tcp_transport is not None
    hh = high.identities["tcp"].hash_id
    low.trusted.update(hh, tcp_host="127.0.0.1", tcp_port=high.tcp_transport.port)
    await low.maintain_paths()
    assert await wait_for(lambda: (link := low.get_link(hh)) is not None and link.up
                          and link.transport == "tcp", timeout=30)
    lh = low.identities["tcp"].hash_id
    assert await wait_for(lambda: (link := high.get_link(lh)) is not None and link.up
                          and link.transport == "tcp", timeout=30)


async def test_both_sides_dialing_at_once_still_connect(
    radios: tuple[MessagingBackend, MessagingBackend], monkeypatch: Any
) -> None:
    """Both phones dial over the radio at the same moment (each thinks the
    other is late): the handshakes cross and still end in one session."""
    monkeypatch.setattr(radio, "RADIO_DIAL_GRACE", 0.0)
    a, b = radios
    _befriend(a, b)
    _befriend(b, a)
    ah, bh = a.identities["tcp"].hash_id, b.identities["tcp"].hash_id
    a._radio_heard[bh] = b._radio_heard[ah] = time.monotonic()
    a._radio_waiting[bh] = b._radio_waiting[ah] = time.monotonic()
    a._maybe_dial_radio(bh)
    b._maybe_dial_radio(ah)
    assert await wait_for(lambda: _on_radio(a, b) and _on_radio(b, a), timeout=90)
    await a.send_message(bh, "crossed")
    assert await wait_for(
        lambda: any(m["text"] == "crossed" for m in b.get_messages()), timeout=30)


async def test_call_audio_goes_as_radio_datagrams(
    radios: tuple[MessagingBackend, MessagingBackend], monkeypatch: Any
) -> None:
    """Voice packets go as single unacknowledged radio packets, sealed with
    the session keys, and reach the other phone's call relay."""
    a, b = radios
    _befriend(a, b)
    _befriend(b, a)
    assert await a.send_radio_beacon()
    assert await wait_for(lambda: _on_radio(a, b) and _on_radio(b, a), timeout=60)
    heard: list[tuple[str, int, int, bytes]] = []
    assert b.call_relay is not None
    monkeypatch.setattr(b.call_relay, "deliver",
                        lambda h, src, dst, data: heard.append((h, src, dst, data)))
    assert a.serial_transport is not None
    before = a.serial_transport.link_stats()["datagrams_out"]
    voice = os.urandom(90)
    await a._send_call_media(b.identities["tcp"].hash_id, 49152, 49153, voice)
    assert await wait_for(lambda: bool(heard), timeout=10)
    assert heard[0] == (a.identities["tcp"].hash_id, 49152, 49153, voice)
    assert a.serial_transport.link_stats()["datagrams_out"] == before + 1
    config = await a.call_ice_config(b.identities["tcp"].hash_id)
    assert config is not None and config["narrow"] is True


async def test_file_over_the_radio_shows_speed_and_time_left(
    radios: tuple[MessagingBackend, MessagingBackend], tmp_path: Any
) -> None:
    a, b = radios
    _befriend(a, b)
    _befriend(b, a)
    assert await a.send_radio_beacon()
    assert await wait_for(lambda: _on_radio(a, b) and _on_radio(b, a), timeout=60)
    updates: list[dict[str, Any]] = []

    async def progress(payload: dict[str, Any]) -> None:
        updates.append(payload)

    a._on_transfer_progress = progress
    src = tmp_path / "notes.bin"
    payload = os.urandom(24 * 1024)
    src.write_bytes(payload)
    result = await a.send_file(b.identities["tcp"].hash_id, src)
    assert result is not None
    tid = result["id"]
    assert await wait_for(lambda: b._transfers.get(tid) is not None
                          and b._transfers[tid].state.value == "complete", timeout=120)
    assert b._incoming_paths[tid].read_bytes() == payload
    moving = [u for u in updates if u["state"] == "transferring" and u["speed_bps"] > 0]
    assert moving, "no speed was ever shown"
    assert all(isinstance(u["eta_s"], int) for u in moving if u["offset"] < u["size"])
