"""Serial permission helper tests."""

from __future__ import annotations

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