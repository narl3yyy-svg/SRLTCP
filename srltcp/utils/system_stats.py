"""System CPU usage and temperature (Linux-first, graceful fallback)."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_prev_cpu: tuple[int, int] | None = None
_prev_time: float = 0.0


def _read_proc_stat() -> tuple[int, int] | None:
    try:
        line = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0]
        parts = [int(x) for x in line.split()[1:]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        total = sum(parts)
        return total, idle
    except (OSError, ValueError, IndexError):
        return None


def cpu_usage_percent() -> float | None:
    """Return system CPU usage % since last call (call twice ~1s apart for accuracy)."""
    global _prev_cpu, _prev_time
    sample = _read_proc_stat()
    if not sample:
        return None
    now = time.monotonic()
    if _prev_cpu is None or (now - _prev_time) < 0.4:
        _prev_cpu = sample
        _prev_time = now
        time.sleep(0.35)
        sample2 = _read_proc_stat()
        if not sample2:
            return None
        total_delta = sample2[0] - sample[0]
        idle_delta = sample2[1] - sample[1]
        _prev_cpu = sample2
        _prev_time = time.monotonic()
        if total_delta <= 0:
            return 0.0
        return round(100.0 * (1.0 - idle_delta / total_delta), 1)
    total_delta = sample[0] - _prev_cpu[0]
    idle_delta = sample[1] - _prev_cpu[1]
    _prev_cpu = sample
    _prev_time = now
    if total_delta <= 0:
        return 0.0
    return round(100.0 * (1.0 - idle_delta / total_delta), 1)


def _thermal_zones() -> list[Path]:
    base = Path("/sys/class/thermal")
    if not base.is_dir():
        return []
    return sorted(base.glob("thermal_zone*"))


def cpu_temperature_c() -> float | None:
    """Average CPU-relevant thermal zone temp in °C."""
    temps: list[float] = []
    for zone in _thermal_zones():
        type_path = zone / "type"
        temp_path = zone / "temp"
        try:
            ztype = type_path.read_text(encoding="utf-8").strip().lower()
            if (
                "cpu" not in ztype
                and "core" not in ztype
                and "pkg" not in ztype
                and ztype not in ("x86_pkg_temp", "acpitz")
            ):
                continue
            millideg = int(temp_path.read_text(encoding="utf-8").strip())
            temps.append(millideg / 1000.0)
        except (OSError, ValueError):
            continue
    if not temps and _thermal_zones():
        try:
            millideg = int((_thermal_zones()[0] / "temp").read_text(encoding="utf-8").strip())
            temps.append(millideg / 1000.0)
        except (OSError, ValueError):
            pass
    if not temps:
        return None
    # Use hottest CPU-relevant zone (package temp is most representative).
    return round(max(temps), 1)


def _query_ntp(server: str, *, timeout: float = 2.0) -> datetime | None:
    """Fetch UTC time from an NTP server (UDP port 123)."""
    import socket
    import struct

    host = (server or "pool.ntp.org").strip()
    if not host:
        return None
    try:
        packet = b"\x1b" + 47 * b"\0"
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(packet, (host, 123))
            data, _ = sock.recvfrom(96)
        if len(data) < 48:
            return None
        seconds = struct.unpack("!I", data[40:44])[0]
        fraction = struct.unpack("!I", data[44:48])[0]
        ntp = seconds + fraction / 2**32 - 2208988800
        return datetime.fromtimestamp(ntp, tz=ZoneInfo("UTC"))
    except Exception:
        return None


def local_time_info(
    timezone: str = "",
    *,
    clock_source: str = "system",
    ntp_server: str = "pool.ntp.org",
) -> dict[str, Any]:
    """Return local clock info for the status bar and settings."""
    tz_name = timezone.strip()
    if not tz_name:
        tz_name = str(datetime.now().astimezone().tzinfo or "UTC")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
        tz_name = "UTC"

    source = (clock_source or "system").lower()
    now_utc: datetime | None = None
    if source == "ntp":
        now_utc = _query_ntp(ntp_server)
    if now_utc is None:
        now = datetime.now(tz)
        source = "system"
    else:
        now = now_utc.astimezone(tz)
    return {
        "timezone": tz_name,
        "clock_source": source,
        "ntp_server": ntp_server if source == "ntp" else "",
        "local_time": now.strftime("%H:%M:%S"),
        "local_date": now.strftime("%Y-%m-%d"),
        "utc_offset": now.strftime("%z"),
    }


def list_timezones() -> list[str]:
    try:
        from zoneinfo import available_timezones

        return sorted(available_timezones())
    except Exception:
        return ["UTC"]


def system_stats(
    *,
    timezone: str = "",
    clock_source: str = "system",
    ntp_server: str = "pool.ntp.org",
) -> dict[str, Any]:
    return {
        "cpu_percent": cpu_usage_percent(),
        "cpu_temp_c": cpu_temperature_c(),
        **local_time_info(
            timezone, clock_source=clock_source, ntp_server=ntp_server
        ),
    }