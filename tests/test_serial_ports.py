"""Serial port utility tests."""

from __future__ import annotations

from srltcp.utils.serial_ports import STANDARD_BAUD_RATES, baud_rates, list_serial_ports


def test_baud_rates() -> None:
    rates = baud_rates()
    assert 57600 in rates
    assert 115200 in rates
    assert rates == list(STANDARD_BAUD_RATES)


def test_list_serial_ports_returns_list() -> None:
    ports = list_serial_ports()
    assert isinstance(ports, list)