"""Persistent application settings."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from srltcp.utils.platform import data_dir

RetentionPreset = Literal["1d", "1w", "1m", "1y", "forever", "restart"]

RETENTION_HOURS: dict[RetentionPreset, int | None] = {
    "1d": 24,
    "1w": 168,
    "1m": 720,
    "1y": 8760,
    "forever": None,
    "restart": 0,
}


@dataclass
class AppSettings:
    setup_complete: bool = False
    display_name: str = "srltcp-node"
    web_port: int = 8743
    tcp_port: int = 7825
    message_retention_preset: RetentionPreset = "1w"
    incoming_files_dir: str = ""
    shared_folder: str = ""
    auto_announce: bool = False
    enable_tcp: bool = True
    enable_serial: bool = False
    serial_port: str = ""
    serial_baud: int = 115200
    lan_ip: str = ""
    bind_interface: str = "0.0.0.0"
    relay_mode: bool = False
    version: str = "0.1.2"

    def retention_hours(self) -> int | None:
        return RETENTION_HOURS.get(self.message_retention_preset, 168)

    def incoming_dir(self) -> Path:
        if self.incoming_files_dir:
            return Path(self.incoming_files_dir).expanduser()
        return data_dir() / "incoming"

    def shared_dir(self) -> Path:
        if self.shared_folder:
            return Path(self.shared_folder).expanduser()
        return data_dir() / "shared"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (data_dir() / "settings.json")
        self.settings = AppSettings()
        self.load()

    def load(self) -> AppSettings:
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for key, value in raw.items():
                if hasattr(self.settings, key):
                    setattr(self.settings, key, value)
        return self.settings

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.settings.to_dict(), indent=2),
            encoding="utf-8",
        )

    def update(self, **kwargs: Any) -> AppSettings:
        for key, value in kwargs.items():
            if hasattr(self.settings, key):
                setattr(self.settings, key, value)
        self.save()
        return self.settings