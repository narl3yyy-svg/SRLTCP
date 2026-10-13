"""System CPU usage and temperature (Linux-first, graceful fallback)."""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_prev_cpu: tuple[int, int] | None = None
_prev_time: float = 0.0


def _read_windows_times() -> tuple[int, int] | None:
    """(total, idle) CPU time on Windows via GetSystemTimes."""
    try:
        import ctypes
        from ctypes import wintypes

        idle, kernel, user = (wintypes.FILETIME() for _ in range(3))
        if not ctypes.windll.kernel32.GetSystemTimes(  # type: ignore[attr-defined]
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None

        def val(ft: wintypes.FILETIME) -> int:
            return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

        # Kernel time already includes idle time.
        return val(kernel) + val(user), val(idle)
    except (OSError, AttributeError, ValueError):
        return None


def _read_proc_stat() -> tuple[int, int] | None:
    if sys.platform == "win32":
        return _read_windows_times()
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


_win_temp_cache: tuple[float, float | None] = (0.0, None)


def _windows_temperature_c() -> float | None:
    """ACPI thermal zone via WMI (PowerShell). Many PCs don't expose it, and
    it may need administrator rights; checked at most once a minute."""
    global _win_temp_cache
    now = time.monotonic()
    if now - _win_temp_cache[0] < 60:
        return _win_temp_cache[1]
    value: float | None = None
    try:
        out = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                "(Get-CimInstance -Namespace root/wmi -ClassName "
                "MSAcpi_ThermalZoneTemperature -ErrorAction Stop | "
                "Measure-Object -Property CurrentTemperature -Maximum).Maximum",
            ],
            capture_output=True, text=True, timeout=6,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip()
        if out:
            kelvin_tenths = float(out.splitlines()[-1])
            celsius = kelvin_tenths / 10.0 - 273.15
            if 0 < celsius < 130:
                value = round(celsius, 1)
    except (OSError, ValueError, subprocess.SubprocessError):
        value = None
    _win_temp_cache = (now, value)
    return value


def _android_battery_temperature_c() -> float | None:
    """Battery temperature from Android (works where CPU sensors are locked
    down, e.g. many smaller phones). Tenths of a degree in the intent."""
    try:
        from java import jclass

        python = jclass("com.chaquo.python.Python")
        ctx = python.getPlatform().getApplication()
        intent_filter = jclass("android.content.IntentFilter")(
            "android.intent.action.BATTERY_CHANGED"
        )
        intent = ctx.registerReceiver(None, intent_filter)
        if intent is None:
            return None
        tenths = intent.getIntExtra("temperature", -10000)
        if tenths <= -1000:
            return None
        return round(float(tenths) / 10.0, 1)
    except Exception:  # noqa: BLE001 - not on Android, or no permission
        return None


def cpu_temperature_c() -> float | None:
    """CPU temperature in °C where the OS exposes it; on Windows the ACPI
    thermal zone; on Android phones that hide CPU sensors, the battery."""
    if sys.platform == "win32":
        return _windows_temperature_c()
    value = _cpu_thermal_zone_c()
    if value is None:
        from srltcp.utils.platform import is_android

        if is_android():
            value = _android_battery_temperature_c()
    return value


def _cpu_thermal_zone_c() -> float | None:
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
    temps = [t for t in temps if 5.0 <= t <= 125.0]  # drop unplugged/bogus sensors
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
        return datetime.fromtimestamp(ntp, tz=UTC)
    except Exception:
        return None


def local_time_info(
    timezone: str = "",
    *,
    clock_source: str = "system",
    ntp_server: str = "pool.ntp.org",
) -> dict[str, Any]:
    """Return local clock info for the status bar and settings."""
    # Windows has no system time zone database: ZoneInfo needs the optional
    # "tzdata" package there. Without it, fall back to the OS's own local time
    # (which already knows the right offset), never crash.
    tz_name = timezone.strip()
    local_tz = datetime.now().astimezone().tzinfo or UTC
    tz: tzinfo
    if not tz_name:
        tz, tz_name = local_tz, str(local_tz)
    else:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz, tz_name = local_tz, str(local_tz)

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