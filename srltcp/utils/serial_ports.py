"""Serial port discovery for settings UI."""

from __future__ import annotations

STANDARD_BAUD_RATES = (
    9600,
    19200,
    38400,
    57600,
    115200,
    230400,
    460800,
    921600,
)


def list_serial_ports() -> list[dict[str, str]]:
    """Return USB/serial devices currently available on the system."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []

    ports: list[dict[str, str]] = []
    for port in list_ports.comports():
        device = port.device or ""
        if not device:
            continue
        label = port.description or device
        if port.manufacturer:
            label = f"{label} ({port.manufacturer})"
        ports.append(
            {
                "device": device,
                "description": label,
                "hwid": port.hwid or "",
            }
        )
    return ports


def baud_rates() -> list[int]:
    return list(STANDARD_BAUD_RATES)