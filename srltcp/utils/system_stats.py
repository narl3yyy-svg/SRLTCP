"""System CPU usage and temperature (Linux-first, graceful fallback)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

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
    if _prev_cpu is None:
        _prev_cpu = sample
        _prev_time = now
        return 0.0
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
    return round(sum(temps) / len(temps), 1)


def system_stats() -> dict[str, Any]:
    return {
        "cpu_percent": cpu_usage_percent(),
        "cpu_temp_c": cpu_temperature_c(),
    }