"""USB serial on Android: the transport over the app's USB host bridge (faked
here), device listing, unplug and reopen."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import pytest

from srltcp.core.protocol.framing import FrameReader
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


async def test_frames_go_both_ways_and_unplug_stops_the_transport(bridge: FakeBridge) -> None:
    transport = SerialTransport(port="usb:0403:6001#0", baudrate=57600, timeout=0.02)
    with pytest.raises(PermissionError):  # Android asks the user first
        await transport.start()
    bridge.allowed = True
    got: list[bytes] = []

    async def on_frame(_peer: Any, frame: bytes) -> None:
        got.append(frame)

    transport.on_frame(on_frame)
    await transport.start()
    assert transport.alive
    peer = transport.peers()[0]
    await transport.send(peer.peer_id, b"hello radio")
    assert FrameReader().feed(bytes(bridge.tx)) == [b"hello radio"]
    with bridge.lock:
        bridge.rx.append(bytes(bridge.tx))  # (loopback)
    for _ in range(100):
        if got:
            break
        await asyncio.sleep(0.02)
    assert got == [b"hello radio"]
    bridge.plugged = False  # unplugged
    for _ in range(100):
        if not transport.alive:
            break
        await asyncio.sleep(0.02)
    assert not transport.alive
    await transport.stop()


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
