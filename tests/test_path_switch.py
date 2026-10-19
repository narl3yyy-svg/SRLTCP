"""Moving between the serial radio and Wi-Fi / the internet: sessions follow at
once, and a file that was moving finishes on the new path."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest

import srltcp.core  # noqa: F401  (import order: core before transports)
from srltcp.core.messaging.backend import MessagingBackend
from srltcp.core.protocol.crypto import CryptoBox, SessionKeys
from srltcp.transports import android_serial
from tests.test_e2e_nodes import make_node, wait_for
from tests.test_radio import PORTS, _befriend, _on_radio


@pytest.fixture
async def radio_pair(tmp_path: Any, monkeypatch: Any) -> AsyncIterator[Any]:
    """Two phones on a realistic 57600-baud radio pair (3 % of frames lost)."""
    from scripts.bench_serial_file import RadioBridge

    bridge = RadioBridge(57600, 0.03)
    monkeypatch.setattr(android_serial, "_bridge", lambda: bridge)
    nodes = [
        make_node(tmp_path, name, enable_serial=True, serial_port=port, serial_baud=57600)
        for name, port in zip(("alice", "bob"), PORTS, strict=True)
    ]
    for node in nodes:
        await node.start()
    a, b = nodes
    try:
        _befriend(a, b)
        _befriend(b, a)
        assert await a.send_radio_beacon()
        assert await wait_for(lambda: _on_radio(a, b) and _on_radio(b, a), timeout=60)
        yield a, b, bridge
    finally:
        for node in nodes:
            await node.stop()


def _learn_addresses(a: MessagingBackend, b: MessagingBackend) -> None:
    for x, y in ((a, b), (b, a)):
        assert y.tcp_transport is not None
        x.trusted.update(y.identities["tcp"].hash_id, tcp_host="127.0.0.1",
                         tcp_port=y.tcp_transport.port)


def _on_tcp(x: MessagingBackend, y: MessagingBackend) -> bool:
    link = x.get_link(y.identities["tcp"].hash_id)
    return bool(link and link.up and link.transport == "tcp")


@pytest.mark.parametrize("how", ["wifi_takes_over", "radio_off", "radio_silent"])
async def test_a_file_moving_on_the_radio_finishes_on_wifi(
    radio_pair: Any, tmp_path: Any, how: str
) -> None:
    """0.13.7: the receiver had every byte but the file never finished (the
    sender's hash had skipped the part resent on the new path)."""
    a, b, bridge = radio_pair
    src = tmp_path / "shot.png"
    payload = os.urandom(132 * 1024)
    src.write_bytes(payload)
    tid = (await b.send_file(a.identities["tcp"].hash_id, src))["id"]
    assert await wait_for(lambda: tid in a._transfers and a._transfers[tid].offset > 16 * 1024,
                          timeout=120)
    _learn_addresses(a, b)
    started = time.monotonic()
    if how == "radio_off":
        for node in (a, b):
            node.config.enable_serial = False
            await node.apply_serial_transport()
    elif how == "radio_silent":
        bridge.loss = 1.0  # out of range
    for node in (a, b):
        await node.addresses_changed()  # (each phone: its network changed)
    assert await wait_for(lambda: a._transfers[tid].state.value == "complete", timeout=30)
    assert time.monotonic() - started < 10
    assert a._transfers[tid].path.read_bytes() == payload
    assert await wait_for(lambda: b._transfers[tid].state.value == "complete", timeout=10)
    assert _on_tcp(a, b) and _on_tcp(b, a)


async def test_one_phone_leaving_the_radio_reconnects_over_the_internet(
    radio_pair: Any, monkeypatch: Any
) -> None:
    """One phone switches its radio off; the other still has the radio
    session. They're on different networks: both are back on the internet in
    seconds (the radio session is checked as soon as the contact dials)."""
    a, b, _bridge = radio_pair
    for node in (a, b):
        monkeypatch.setattr(node, "_tcp_peer_on_lan", lambda peer_id: False)
    _learn_addresses(a, b)
    started = time.monotonic()
    a.config.enable_serial = False
    await a.apply_serial_transport()
    assert await wait_for(lambda: _on_tcp(a, b) and _on_tcp(b, a), timeout=30)
    assert time.monotonic() - started < 15
    ah, bh = a.identities["tcp"].hash_id, b.identities["tcp"].hash_id
    await a.send_message(bh, "over the internet")
    await b.send_message(ah, "and back")
    assert await wait_for(
        lambda: any(m["text"] == "over the internet" for m in b.get_messages())
        and any(m["text"] == "and back" for m in a.get_messages()), timeout=20)


async def test_a_working_radio_session_still_wins_over_the_internet(
    radio_pair: Any, monkeypatch: Any
) -> None:
    """While the radio carries traffic, an internet session is refused, and
    the check that follows finds the radio answering: nothing changes."""
    a, b, _bridge = radio_pair
    for node in (a, b):
        monkeypatch.setattr(node, "_tcp_peer_on_lan", lambda peer_id: False)
    bh = b.identities["tcp"].hash_id
    assert a.tcp_transport is not None and b.tcp_transport is not None
    await a.ping_peer(bh)
    await asyncio.sleep(1)
    # a keeps its radio session and still offers an internet path
    peer_id = await a.tcp_transport.connect("127.0.0.1", b.tcp_transport.port)
    assert await a._send_upgrade_handshake(bh, peer_id) is not None
    await asyncio.sleep(8)  # (longer than the radio check)
    assert _on_radio(b, a) and _on_radio(a, b)


def test_installing_the_same_keys_again_never_reuses_a_nonce() -> None:
    keys = SessionKeys(send_key=os.urandom(32), recv_key=os.urandom(32), salt=b"")
    box = CryptoBox(keys)
    first, _ = box.reserve()
    box.set_keys(keys)  # (a repeated handshake answer)
    second, _ = box.reserve()
    assert first != second and box.epoch == 1
    box.set_keys(SessionKeys(send_key=os.urandom(32), recv_key=os.urandom(32), salt=b""))
    assert box.epoch == 2  # new keys: the counter may start over


@pytest.mark.parametrize("who", ["lower", "higher"])
async def test_wifi_off_on_one_phone_moves_both_to_the_radio(
    radio_pair: Any, monkeypatch: Any, who: str
) -> None:
    """Both on the same Wi-Fi (that beats the radio), radios plugged in. One
    phone loses its Wi-Fi; the other's session looks fine to it (no goodbye
    arrives). The first phone's beacon tells the other: both are on the radio
    within seconds, whichever of them would normally dial."""
    a, b, _bridge = radio_pair
    _learn_addresses(a, b)
    for node in (a, b):
        await node.addresses_changed()
    assert await wait_for(lambda: _on_tcp(a, b) and _on_tcp(b, a), timeout=30)
    low, high = sorted((a, b), key=lambda n: n.identities["tcp"].hash_id)
    off, other = (low, high) if who == "lower" else (high, low)
    oh = other.identities["tcp"].hash_id
    real_send = off._send_raw

    async def no_wifi(peer_id: str, transport: str, packet: bytes) -> None:
        if transport == "serial":
            await real_send(peer_id, transport, packet)

    async def no_dial(*_a: Any, **_k: Any) -> str:
        raise OSError("network unreachable")

    monkeypatch.setattr(off, "_send_raw", no_wifi)
    assert off.tcp_transport is not None
    monkeypatch.setattr(off.tcp_transport, "connect", no_dial)
    if off.tcp_transport._server is not None:
        off.tcp_transport._server.close()  # (nothing reaches it over the Wi-Fi either)
    started = time.monotonic()
    off.remove_link(oh)  # (its Wi-Fi is gone: no goodbye reaches the other phone)
    assert await wait_for(lambda: _on_radio(off, other) and _on_radio(other, off), timeout=60)
    assert time.monotonic() - started < 20
