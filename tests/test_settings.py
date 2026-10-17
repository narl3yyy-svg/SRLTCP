"""Settings store tests."""

from __future__ import annotations

from pathlib import Path

from srltcp.core.settings import RETENTION_HOURS, AppSettings, SettingsStore


def test_settings_defaults(tmp_path: Path) -> None:
    store = SettingsStore(path=tmp_path / "settings.json")
    settings = store.load()
    assert settings.auto_announce is True
    assert settings.version == "0.13.6"
    assert settings.tcp_port == 7825
    assert settings.discovery_port == 7826
    assert settings.strict_ports is True
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


def test_old_relay_and_hub_keys_are_ignored(tmp_path: Path) -> None:
    """Relays are gone (0.13): settings files from older versions still load."""
    import json

    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "hub_enabled": True, "hub_host": "old.example.com", "relay_enabled": True,
        "relay_code": "srltcp-relay://relay.example.com:443#x.y", "call_stun_server": "turn:x:1",
    }))
    settings = SettingsStore(path=path).load()
    for key in ("hub_host", "relay_code", "relay_enabled", "call_stun_server"):
        assert not hasattr(settings, key)


def test_handshake_protocol_persist(tmp_path: Path) -> None:
    store = SettingsStore(path=tmp_path / "settings.json")
    settings = store.load()
    settings.handshake_protocol = "noise_xx"
    store.save(settings)
    loaded = store.load()
    assert loaded.handshake_protocol == "noise_xx"


def test_wan_expose_port_persist(tmp_path: Path) -> None:
    store = SettingsStore(path=tmp_path / "settings.json")
    settings = store.load()
    settings.wan_expose_port = True
    store.save(settings)
    loaded = store.load()
    assert loaded.wan_expose_port is True


def test_android_upgrade_migration_unpins_ip_and_enables_discovery() -> None:
    from srltcp.app import migrate_settings
    from srltcp.core.settings import AppSettings

    def make(version: str, ip: str) -> AppSettings:
        return AppSettings(setup_complete=True, version=version, lan_ip=ip, auto_announce=False)

    old = make("0.2.0", "192.168.1.20")
    assert migrate_settings(old, android=True)
    assert old.lan_ip == "" and old.auto_announce is True

    desktop = make("0.2.0", "192.168.1.20")
    assert not migrate_settings(desktop, android=False)
    assert desktop.lan_ip == "192.168.1.20"

    current = make("0.3.0", "10.0.100.3")
    assert not migrate_settings(current, android=True)
