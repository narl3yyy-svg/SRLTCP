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


def default_serial_port() -> str:
    system = platform.system()
    if system == "Windows":
        return "COM3"
    if system == "Darwin":
        return "/dev/tty.usbserial"
    return "/dev/ttyUSB0"