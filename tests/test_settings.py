"""Settings store tests."""

from __future__ import annotations

from pathlib import Path

from srltcp.core.settings import SettingsStore


def test_settings_defaults(tmp_path: Path) -> None:
    store = SettingsStore(path=tmp_path / "settings.json")
    assert store.settings.auto_announce is False
    assert store.settings.version == "0.1.2"
    assert store.settings.retention_hours() == 168


def test_settings_persist(tmp_path: Path) -> None:
    store = SettingsStore(path=tmp_path / "settings.json")
    store.update(display_name="bob", auto_announce=True, message_retention_preset="1d")
    store2 = SettingsStore(path=tmp_path / "settings.json")
    assert store2.settings.display_name == "bob"
    assert store2.settings.auto_announce is True
    assert store2.settings.retention_hours() == 24