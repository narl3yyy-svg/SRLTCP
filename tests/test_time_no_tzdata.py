"""Windows without the tzdata package: no time zone database at all."""

from __future__ import annotations

import pytest

from srltcp.utils import system_stats


def test_local_time_survives_missing_tz_database(monkeypatch: pytest.MonkeyPatch) -> None:
    class Missing(Exception):
        pass

    def raise_missing(key: str) -> None:
        raise Missing(f"No time zone found with key {key}")

    monkeypatch.setattr(system_stats, "ZoneInfo", raise_missing)
    for tz in ("America/Los_Angeles", "UTC", ""):
        info = system_stats.local_time_info(tz)
        assert info["local_time"] and info["utc_offset"] is not None
    stats = system_stats.system_stats(timezone="America/Los_Angeles")
    assert "local_time" in stats
