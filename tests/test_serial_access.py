"""Serial permission helper tests."""

from __future__ import annotations

import srltcp.utils.serial_access as serial_access
from srltcp.utils.serial_access import (
    format_serial_permission_help,
    serial_group_status,
)


def test_permission_help_mentions_uucp_on_arch_hint() -> None:
    msg = format_serial_permission_help(
        "/dev/ttyUSB0",
        PermissionError(13, "Permission denied"),
    )
    assert "/dev/ttyUSB0" in msg
    assert "uucp" in msg or "dialout" in msg
    assert "Both peers" in msg


def test_serial_group_status_shape() -> None:
    status = serial_group_status()
    assert "group" in status
    assert "in_account" in status
    assert "in_session" in status
    assert "needs_relogin" in status


def test_serial_access_without_grp_module(monkeypatch) -> None:
    monkeypatch.setattr(serial_access, "_grp", None)
    assert serial_access.serial_access_group() is None
    assert serial_access.serial_group_status() == {
        "group": None,
        "in_account": False,
        "in_session": False,
        "needs_relogin": False,
    }

def test_windows_com_ports_are_never_shown_as_no_permission(monkeypatch) -> None:
    """COM30 isn't a file on Windows (os.path.exists is always False there), so
    every port showed "no permission" although the radio worked."""
    monkeypatch.setattr(serial_access.sys, "platform", "win32")
    assert serial_access.user_can_access_serial("COM30")
    assert not serial_access.user_can_access_serial("")


def test_linux_still_checks_the_device_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(serial_access.sys, "platform", "linux")
    assert not serial_access.user_can_access_serial(str(tmp_path / "ttyUSB9"))  # not there
    dev = tmp_path / "ttyUSB0"
    dev.write_bytes(b"")
    assert serial_access.user_can_access_serial(str(dev))


def test_windows_port_in_use_says_another_program_has_it(monkeypatch) -> None:
    monkeypatch.setattr(serial_access.platform, "system", lambda: "Windows")
    msg = format_serial_permission_help(
        "COM30",
        "could not open port 'COM30': PermissionError(13, 'Access is denied.', None, 5)",
    )
    assert "COM30" in msg and "another program" in msg
    assert "uucp" not in msg and "dialout" not in msg and "usermod" not in msg
