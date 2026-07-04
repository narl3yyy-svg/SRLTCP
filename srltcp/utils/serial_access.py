"""Serial port permission helpers (Arch uucp vs Debian dialout)."""

from __future__ import annotations

import os
import platform
import types

_grp: types.ModuleType | None
try:
    import grp as _grp_mod

    _grp = _grp_mod
except ModuleNotFoundError:
    _grp = None  # Android builds omit grp (no setgrent in NDK)


def serial_access_group() -> str | None:
    """Return the OS group that owns serial devices, if known."""
    if _grp is None:
        return None
    for name in ("dialout", "uucp"):
        try:
            _grp.getgrnam(name)
            return name
        except KeyError:
            continue
    return None


def serial_group_status() -> dict[str, object]:
    """Whether the serial device group is available in this process session."""
    group = serial_access_group()
    if not group:
        return {"group": None, "in_account": False, "in_session": False, "needs_relogin": False}
    assert _grp is not None
    group_gid = _grp.getgrnam(group).gr_gid
    in_account = os.getuid() in _grp.getgrnam(group).gr_mem
    in_session = group_gid in os.getgroups()
    return {
        "group": group,
        "in_account": in_account,
        "in_session": in_session,
        "needs_relogin": in_account and not in_session,
    }


def format_serial_permission_help(port: str, error: Exception | str | None = None) -> str:
    """Human-readable steps to open a serial device."""
    err = str(error or "")
    group = serial_access_group()
    status = serial_group_status()
    system = platform.system()
    lines = [f"Cannot open {port}"]
    if "Permission denied" in err or "Errno 13" in err:
        if status.get("needs_relogin") and group:
            lines.append(
                f"You are in '{group}' but this session has not picked it up — "
                "log out and back in, or restart via ./run.sh web (auto-refreshes the group)"
            )
        elif group:
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