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


def list_serial_ports() -> list[dict[str, str | bool | int]]:
    """Return USB/serial devices currently available on the system."""
    from srltcp.utils.platform import is_android
    from srltcp.utils.serial_access import user_can_access_serial

    if is_android():
        # Apps can't see /dev/ttyUSB* on Android: the USB host bridge lists them.
        from srltcp.transports.android_serial import list_ports as android_ports

        return android_ports()

    try:
        from serial.tools import list_ports
    except ImportError:
        return []

    ports: list[dict[str, str | bool | int]] = []
    for port in list_ports.comports():
        device = port.device or ""
        if not device:
            continue
        label = port.description or device
        if port.manufacturer:
            label = f"{label} ({port.manufacturer})"
        accessible = user_can_access_serial(device)
        ports.append(
            {
                "device": device,
                "description": label,
                "hwid": port.hwid or "",
                "accessible": accessible,
            }
        )
    return ports


def baud_rates() -> list[int]:
    return list(STANDARD_BAUD_RATES)