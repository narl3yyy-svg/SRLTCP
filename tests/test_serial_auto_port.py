""""Automatic" serial port on a PC: the USB adapter that's plugged in."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from srltcp.utils import platform as plat
from srltcp.utils import serial_ports


def _port(device: str, vid: int | None, description: str = "n/a") -> Any:
    return SimpleNamespace(device=device, vid=vid, description=description,
                           manufacturer=None, hwid="")


@pytest.fixture
def plugged(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    from serial.tools import list_ports

    ports: list[Any] = []
    monkeypatch.setattr(list_ports, "comports", lambda: list(ports))
    monkeypatch.setattr(plat, "is_android", lambda: False)
    return ports


def test_automatic_finds_a_usb_cdc_radio(plugged: list[Any]) -> None:
    # ESP32-S3 / RP2040 radios are /dev/ttyACM*, not /dev/ttyUSB0; the
    # /dev/ttyS* slots are listed even with nothing connected.
    plugged += [_port(f"/dev/ttyS{i}", None) for i in range(4)]
    plugged.append(_port("/dev/ttyACM0", 0x303A, "USB JTAG/serial debug unit"))
    assert plat.default_serial_port() == "/dev/ttyACM0"
    assert plat.resolve_serial_port("") == "/dev/ttyACM0"


def test_automatic_with_nothing_plugged_in(plugged: list[Any]) -> None:
    plugged += [_port("/dev/ttyS0", None)]
    assert plat.default_serial_port() == ""
    assert plat.resolve_serial_port("") == ""


def test_saved_port_kept_while_there_else_the_plugged_in_one(plugged: list[Any]) -> None:
    plugged.append(_port("/dev/ttyACM1", 0x303A))
    assert plat.resolve_serial_port("/dev/ttyACM1") == "/dev/ttyACM1"
    # Replugged and renamed by Linux: the radio that's there now.
    assert plat.resolve_serial_port("/dev/ttySRLTCPgone") == "/dev/ttyACM1"
    plugged.clear()
    # (nothing else plugged in: the saved one, whose error says why)
    assert plat.resolve_serial_port("/dev/ttySRLTCPgone") == "/dev/ttySRLTCPgone"


def test_settings_list_hides_empty_builtin_slots(plugged: list[Any], monkeypatch: Any) -> None:
    from srltcp.utils import serial_access

    monkeypatch.setattr(serial_access, "user_can_access_serial", lambda _d: True)
    plugged += [_port("/dev/ttyS0", None), _port("/dev/ttyACM0", 0x303A, "USB JTAG")]
    assert [p["device"] for p in serial_ports.list_serial_ports()] == ["/dev/ttyACM0"]
