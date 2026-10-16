"""USB serial on Android: the transport over the app's USB host bridge (faked
here), device listing, unplug and reopen."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import pytest

import srltcp.core  # noqa: F401  (import order: core before transports)
from srltcp.transports import android_serial
from srltcp.transports.serial import SerialTransport


class FakeBridge:
    """Stands in for com.srltcp.app.UsbSerialBridge (the Kotlin side)."""

    def __init__(self) -> None:
        self.allowed = False
        self.plugged = True
        self.opened = False
        self.gen = 0
        self.asks = 0  # permission questions shown
        self.rx: list[bytes] = []
        self.tx = bytearray()
        self.lock = threading.Lock()

    def listPorts(self) -> str:  # noqa: N802 - Java names
        if not self.plugged:
            return "[]"
        return json.dumps([{
            "device": "usb:0403:6001#0", "description": "FTDI FT232R (USB 0403:6001)",
            "accessible": self.allowed,
        }])

    def open(self, port: str, baud: int, ask: bool) -> int:
        self.opened = False
        if not self.plugged or port != "usb:0403:6001#0":
            return -2
        if not self.allowed:
            self.asks += 1 if ask else 0
            return -1
        self.opened = True
        self.gen += 1
        return self.gen

    def getLastError(self) -> str:  # noqa: N802
        return ""

    def isOpen(self, gen: int) -> bool:  # noqa: N802
        return self.opened and gen == self.gen

    def read(self, gen: int, size: int, timeout_ms: int) -> bytes:
        if not self.plugged or gen != self.gen:
            raise RuntimeError("java.io.IOException: USB get_status request failed")
        with self.lock:
            if self.rx:
                return self.rx.pop(0)
        threading.Event().wait(timeout_ms / 1000)
        return b""

    def write(self, gen: int, data: bytes, timeout_ms: int) -> None:
        self.tx += data

    def close(self, gen: int) -> None:
        if gen == self.gen:
            self.opened = False


@pytest.fixture
def bridge(monkeypatch: Any) -> FakeBridge:
    fake = FakeBridge()
    monkeypatch.setattr(android_serial, "_bridge", lambda: fake)
    return fake


def test_lists_adapters_from_the_bridge(bridge: FakeBridge, monkeypatch: Any) -> None:
    from srltcp.utils import platform as plat
    from srltcp.utils import serial_ports

    monkeypatch.setattr(plat, "is_android", lambda: True)
    ports = serial_ports.list_serial_ports()
    assert ports == [{"device": "usb:0403:6001#0", "description": "FTDI FT232R (USB 0403:6001)",
                      "hwid": "", "accessible": False}]
    assert plat.default_serial_port() == "usb:0403:6001#0"
    bridge.plugged = False
    assert serial_ports.list_serial_ports() == [] and plat.default_serial_port() == ""


async def test_frames_go_both_ways_and_unplug_stops_the_transport(
    bridge: FakeBridge, monkeypatch: Any
) -> None:
    """Two phones, each with its USB radio (two fake bridges wired together)."""
    other = FakeBridge()
    other.allowed = True
    bridges = {"a": bridge, "b": other}
    which = {"next": "a"}

    def pick() -> FakeBridge:
        return bridges[which["next"]]

    monkeypatch.setattr(android_serial, "_bridge", pick)

    def wire(src: FakeBridge, dst: FakeBridge) -> None:
        def write(gen: int, data: bytes, timeout_ms: int) -> None:
            with dst.lock:
                dst.rx.append(bytes(data))
        src.write = write  # type: ignore[method-assign]

    wire(bridge, other)
    wire(other, bridge)
    a = SerialTransport(port="usb:0403:6001#0", baudrate=57600, timeout=0.02)
    with pytest.raises(PermissionError):  # Android asks the user first
        await a.start()
    bridge.allowed = True
    await a.start()
    which["next"] = "b"
    b = SerialTransport(port="usb:0403:6001#0", baudrate=57600, timeout=0.02)
    await b.start()
    got: list[bytes] = []

    async def on_frame(_peer: Any, frame: bytes) -> None:
        got.append(frame)

    b.on_frame(on_frame)
    assert a.alive and b.alive
    big = bytes(range(256)) * 40  # 10 KiB: many packets, acknowledged
    await a.send(a.peers()[0].peer_id, b"hello radio")
    await a.send(a.peers()[0].peer_id, big)
    for _ in range(300):
        if len(got) == 2:
            break
        await asyncio.sleep(0.02)
    assert got == [b"hello radio", big]
    for _ in range(100):  # (the last acknowledgement is on its way back)
        if a.link_stats()["messages_out"] == 2:
            break
        await asyncio.sleep(0.02)
    assert a.link_stats()["messages_out"] == 2 and b.link_stats()["messages_in"] == 2
    bridge.plugged = False  # unplugged
    for _ in range(100):
        if not a.alive:
            break
        await asyncio.sleep(0.02)
    assert not a.alive
    await a.stop()
    await b.stop()


async def test_watchdog_reopens_after_permission_and_replug(
    bridge: FakeBridge, tmp_path: Any
) -> None:
    from srltcp.core.messaging.backend import MessagingBackend, NodeConfig

    backend = MessagingBackend(NodeConfig(name="n", incoming_dir=str(tmp_path / "in"),
                                          enable_serial=True, serial_port="usb:0403:6001#0"))
    try:
        await backend.serial_watchdog()  # waiting for the user to allow it
        await backend.serial_watchdog()
        assert backend.serial_transport is None and "Allow" in (backend._serial_error or "")
        assert bridge.asks == 0  # the background retry never shows the question again
        await backend.apply_serial_transport()  # Save in Settings: asks
        assert bridge.asks == 1
        bridge.allowed = True
        await backend.serial_watchdog()
        assert backend.serial_transport is not None and backend.serial_transport.alive
        bridge.plugged = False
        for _ in range(100):
            if not backend.serial_transport.alive:
                break
            await asyncio.sleep(0.02)
        await backend.serial_watchdog()  # gone: closed, not reopened yet
        assert backend.serial_transport is None
        bridge.plugged = True
        await backend.serial_watchdog()
        assert backend.serial_transport is not None and backend.serial_transport.alive
    finally:
        if backend.serial_transport:
            await backend.serial_transport.stop()


def test_closing_an_old_port_leaves_the_new_one_open(bridge: FakeBridge) -> None:
    bridge.allowed = True
    old = android_serial.AndroidSerial("usb:0403:6001#0", 57600)
    new = android_serial.AndroidSerial("usb:0403:6001#0", 115200)  # baud changed
    assert not old.is_open and new.is_open
    with pytest.raises(OSError):
        old.read(64)  # a late read from the old reader thread
    old.close()
    assert new.is_open


async def test_no_adapter_gives_a_plain_message(monkeypatch: Any) -> None:
    transport = SerialTransport(port="", baudrate=57600)
    transport.port = ""
    with pytest.raises(FileNotFoundError, match="No serial adapter"):
        await transport.start()


class AirBridge:
    """Both phones' USB radios: port ids pick the phone; what one writes
    reaches the other over a lossy air link."""

    def __init__(self, ports: list[str], loss: float, seed: int = 3) -> None:
        import random

        self.rnd = random.Random(seed)
        self.loss = loss
        self.ports = ports
        self.rx: dict[str, list[bytes]] = {p: [] for p in ports}
        self.gens: dict[int, str] = {}
        self.lock = threading.Lock()

    def listPorts(self) -> str:  # noqa: N802
        return json.dumps([{"device": p, "description": p, "accessible": True} for p in self.ports])

    def open(self, port: str, baud: int, ask: bool) -> int:
        gen = len(self.gens) + 1
        self.gens[gen] = port
        return gen

    def getLastError(self) -> str:  # noqa: N802
        return ""

    def isOpen(self, gen: int) -> bool:  # noqa: N802
        return gen in self.gens

    def read(self, gen: int, size: int, timeout_ms: int) -> bytes:
        port = self.gens[gen]
        with self.lock:
            if self.rx[port]:
                return self.rx[port].pop(0)
        threading.Event().wait(timeout_ms / 1000)
        return b""

    def write(self, gen: int, data: bytes, timeout_ms: int) -> None:
        other = next(p for p in self.ports if p != self.gens[gen])
        for i in range(0, len(data), 64):
            if self.rnd.random() < self.loss:
                continue  # lost on the air
            with self.lock:
                self.rx[other].append(bytes(data[i : i + 64]))

    def close(self, gen: int) -> None:
        self.gens.pop(gen, None)


async def test_contacts_find_each_other_and_chat_over_the_radio(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Two phones, each with a USB radio, no network between them: they hear
    each other's private beacon, connect (same identity as the contact they
    paired over Wi-Fi) and chat; ~9 KiB handshakes cross a lossy air link."""
    from srltcp.core.trusted import TrustedPeer
    from tests.test_e2e_nodes import make_node, wait_for

    air = AirBridge(["usb:0403:6015#0", "usb:303a:1001#0"], loss=0.03)
    monkeypatch.setattr(android_serial, "_bridge", lambda: air)
    a = make_node(tmp_path, "alice", enable_serial=True, serial_port="usb:0403:6015#0",
                  serial_baud=57600)
    b = make_node(tmp_path, "bob", enable_serial=True, serial_port="usb:303a:1001#0",
                  serial_baud=57600)
    await a.start()
    await b.start()
    try:
        for x, y in ((a, b), (b, a)):  # contacts, but no address to reach over a network
            ident = y.identities["tcp"]
            x.trusted.add(TrustedPeer(hash_id=ident.hash_id, name=ident.name,
                                      public_key=ident.public_bytes().hex()))
        ah, bh = a.identities["tcp"].hash_id, b.identities["tcp"].hash_id
        assert a.serial_transport and a.serial_transport.alive
        assert await a.send_radio_beacon()
        assert await wait_for(lambda: (link := a.get_link(bh)) is not None
                              and link.handshake_complete and link.transport == "serial"
                              and (other := b.get_link(ah)) is not None
                              and other.handshake_complete, timeout=60)
        await a.send_message(bh, "over the radio")
        assert await wait_for(
            lambda: any(m["text"] == "over the radio" for m in b.get_messages()), timeout=30)
        await b.send_message(ah, "loud and clear")
        assert await wait_for(
            lambda: any(m["text"] == "loud and clear" for m in a.get_messages()), timeout=30)
    finally:
        await a.stop()
        await b.stop()
