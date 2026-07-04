"""Serial port permission helpers (Arch uucp vs Debian dialout)."""

from __future__ import annotations

import grp
import os
import platform


def serial_access_group() -> str | None:
    """Return the OS group that owns serial devices, if known."""
    for name in ("dialout", "uucp"):
        try:
            grp.getgrnam(name)
            return name
        except KeyError:
            continue
    return None


def format_serial_permission_help(port: str, error: Exception | str | None = None) -> str:
    """Human-readable steps to open a serial device."""
    err = str(error or "")
    group = serial_access_group()
    system = platform.system()
    lines = [f"Cannot open {port}"]
    if "Permission denied" in err or "Errno 13" in err:
        if group:
            lines.append(
                f"Add your user to the '{group}' group, log out/in, then restart SRLTCP:"
            )
            lines.append(f"  sudo usermod -aG {group} $USER")
        if system == "Linux":
            lines.append(f"Or temporarily: sudo chmod a+rw {port}")
        if system == "Linux" and group == "uucp":
            lines.append("(Arch Linux uses group 'uucp', not 'dialout')")
    elif error:
        lines.append(err)
    lines.append("Both peers need serial enabled and the port open to send/receive RF announces.")
    return " — ".join(lines)


def user_can_access_serial(port: str) -> bool:
    """Best-effort check whether the current user can open the device."""
    if not port or not os.path.exists(port):
        return False
    return os.access(port, os.R_OK | os.W_OK)