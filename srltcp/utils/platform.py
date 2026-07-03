"""Platform-specific paths and helpers."""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path


def is_android() -> bool:
    return "chaquopy" in sys.modules or os.environ.get("SRLTCP_ANDROID") == "1"


def data_dir() -> Path:
    """Return persistent data directory for identities and transfers."""
    if is_android():
        base = Path("/data/data/com.srltcp.app/files")
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