"""USB serial adapters on Android, through the app's Kotlin bridge.

Android doesn't let apps open ``/dev/ttyUSB*``: USB devices are reached
through the USB host API after the user allows it, which pyserial can't do.
``UsbSerialBridge`` (android/.../UsbSerialBridge.kt, built on
usb-serial-for-android) does, and this module gives it pyserial's shape so
:class:`srltcp.transports.serial.SerialTransport` works unchanged.

Device ids look like ``usb:0403:6001#0`` (vendor id, product id, port index):
they stay the same when the adapter is plugged in again.
"""

from __future__ import annotations

import json
from typing import Any

PREFIX = "usb:"


def _bridge() -> Any:
    from java import jclass  # Chaquopy (Android only)

    return jclass("com.srltcp.app.UsbSerialBridge")


def list_ports() -> list[dict[str, str | bool | int]]:
    try:
        raw = json.loads(str(_bridge().listPorts()))
    except Exception:  # noqa: BLE001 - not on Android, or no USB host support
        return []
    return [
        {
            "device": str(p.get("device", "")),
            "description": str(p.get("description", "")),
            "hwid": "",
            "accessible": bool(p.get("accessible")),
            "attached": int(p.get("attached") or 0),  # higher = plugged in later
        }
        for p in raw
        if isinstance(p, dict) and str(p.get("device", "")).startswith(PREFIX)
    ]


class UsbSerialPermissionError(PermissionError):
    """Android is asking the user to allow the USB device."""


class AndroidSerial:
    """The subset of ``serial.Serial`` the serial transport uses.

    Each open has its own generation number in the bridge: closing (or a late
    read error on) an old port never touches a newer one."""

    def __init__(
        self, port: str, baudrate: int, timeout: float = 0.1, *, ask: bool = True
    ) -> None:
        self.port = port
        self.timeout_ms = max(1, int(timeout * 1000))
        self._bridge = _bridge()
        self._closed = False
        gen = int(self._bridge.open(port, int(baudrate), bool(ask)))
        if gen == -1:
            raise UsbSerialPermissionError(
                "Allow SRLTCP to use this USB device (tap OK on Android's question, or "
                "press Save to be asked again); it opens by itself then"
            )
        if gen == -2:
            raise FileNotFoundError(f"USB serial adapter not connected ({port})")
        if gen <= 0:
            raise OSError(f"Cannot open the USB serial adapter: {self._bridge.getLastError()}")
        self._gen = gen

    @property
    def is_open(self) -> bool:
        return not self._closed and bool(self._bridge.isOpen(self._gen))

    def read(self, size: int = 1) -> bytes:
        try:
            data = self._bridge.read(self._gen, int(size), self.timeout_ms)
        except Exception as exc:  # noqa: BLE001 - Java exception: unplugged or replaced
            self.close()  # is_open turns False: the transport stops and is reopened later
            raise OSError(f"USB serial adapter gone: {exc}") from exc
        return bytes(data) if data is not None else b""

    def write(self, data: bytes) -> int:
        try:
            self._bridge.write(self._gen, bytes(data), 2000)
        except Exception as exc:  # noqa: BLE001
            raise OSError(f"USB serial write failed: {exc}") from exc
        return len(data)

    def flush(self) -> None:
        return

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._bridge.close(self._gen)
