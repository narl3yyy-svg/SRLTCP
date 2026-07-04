"""Serial permission helper tests."""

from __future__ import annotations

from srltcp.utils.serial_access import format_serial_permission_help


def test_permission_help_mentions_uucp_on_arch_hint() -> None:
    msg = format_serial_permission_help(
        "/dev/ttyUSB0",
        PermissionError(13, "Permission denied"),
    )
    assert "/dev/ttyUSB0" in msg
    assert "uucp" in msg or "dialout" in msg
    assert "Both peers" in msg