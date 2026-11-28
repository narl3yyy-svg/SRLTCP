"""Platform-specific paths and helpers."""

from __future__ import annotations

import os
import platform
from pathlib import Path

_android_data_dir: str | None = None


def is_android() -> bool:
    return os.environ.get("SRLTCP_ANDROID") == "1"


def set_android_data_dir(path: str) -> None:
    """Called from Android MainActivity before starting the Python server."""
    global _android_data_dir
    _android_data_dir = path
    # Mark the runtime as Android as early as possible so module-level
    # decisions made before start_android_server() see the right platform.
    os.environ["SRLTCP_ANDROID"] = "1"


def data_dir() -> Path:
    """Return persistent data directory for identities and transfers."""
    override = os.environ.get("SRLTCP_DATA_DIR", "").strip()
    if override:
        base = Path(override)
        base.mkdir(parents=True, exist_ok=True)
        return base
    if is_android():
        base = Path(
            _android_data_dir
            or os.environ.get("SRLTCP_DATA_DIR", "/data/data/com.srltcp.app/files")
        )
    elif platform.system() == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home())) / "SRLTCP"
    else:
        base = Path.home() / ".srltcp"
    base.mkdir(parents=True, exist_ok=True)
    return base


def resolve_serial_port(saved: str) -> str:
    """The adapter to open. Android: the saved one while it's plugged in;
    otherwise ("Automatic", or the saved one is gone) the adapter plugged in
    most recently, so a new radio is used as soon as it's plugged in. PC:
    the saved port while it's there; "Automatic" (or the saved one is gone
    and another USB adapter is plugged in) the USB adapter that's there."""
    if is_android():
        from srltcp.transports.android_serial import list_ports

        ports = list_ports()
        if saved and any(p["device"] == saved for p in ports):
            return saved
        newest = max(ports, key=lambda p: int(p.get("attached") or 0), default=None)
        return str(newest["device"]) if newest else ""
    if not saved:
        return default_serial_port()
    if not saved.startswith("/dev/") or os.path.exists(saved):
        return saved
    # The saved device file is gone (Linux renames a replugged radio, e.g.
    # /dev/ttyACM0 → /dev/ttyACM1): use the USB adapter that's plugged in, if
    # any; else the saved one, whose error then says why it can't open.
    usb = usb_serial_ports()
    return usb[0] if usb else saved


def usb_serial_ports() -> list[str]:
    """USB serial adapters plugged into this computer (not Android). Only USB
    ones: built-in UART slots (/dev/ttyS0…31 on Linux, listed even with
    nothing connected), COM1 and Bluetooth ports are never a radio."""
    try:
        from serial.tools import list_ports

        found = list(list_ports.comports())
    except Exception:  # (pyserial missing, or a broken udev / registry entry)
        return []
    return sorted(p.device for p in found if p.device and p.vid is not None)


def default_serial_port() -> str:
    """"Automatic": the USB serial adapter that's plugged in, "" if none.
    Linux: /dev/ttyUSB* (FTDI, CH340, CP210x) or /dev/ttyACM* (USB CDC:
    ESP32-S3/C3, RP2040, Arduino); macOS: /dev/cu.usb*; Windows: COM<n>."""
    if is_android():
        from srltcp.transports.android_serial import list_ports

        ports = list_ports()
        return str(ports[0]["device"]) if ports else ""
    usb = usb_serial_ports()
    return usb[0] if usb else ""