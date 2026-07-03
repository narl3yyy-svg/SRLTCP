"""Settings store tests."""

from __future__ import annotations

from pathlib import Path

from srltcp.core.settings import RETENTION_HOURS, AppSettings, SettingsStore


def test_settings_defaults(tmp_path: Path) -> None:
    store = SettingsStore(path=tmp_path / "settings.json")
    settings = store.load()
    assert settings.auto_announce is False
    assert settings.version == "0.1.14"
    assert settings.serial_baud == 57600
    assert settings.message_retention_hours == 168


def test_retention_presets() -> None:
    s = AppSettings(message_retention_preset="1d")
    s.apply_retention_preset()
    assert s.message_retention_hours == RETENTION_HOURS["1d"]


def test_settings_persist(tmp_path: Path) -> None:
    store = SettingsStore(path=tmp_path / "settings.json")
    settings = store.load()
    settings.display_name = "bob"
    settings.auto_announce = True
    settings.message_retention_preset = "1d"
    store.save(settings)
    loaded = store.load()
    assert loaded.display_name == "bob"
    assert loaded.auto_announce is True
    assert loaded.message_retention_hours == 24