"""Persistent application settings (~/.srltcp/settings.json)."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from srltcp.core.messaging.constants import (
    DEFAULT_TCP_PORT,
    DISCOVERY_PORT,
    WEB_PORT,
)
from srltcp.utils.files import ensure_dir
from srltcp.utils.platform import data_dir

DEFAULT_INCOMING = "incoming"
DEFAULT_SHARED = "shared"

RetentionPreset = Literal["1d", "1w", "1m", "1y", "forever", "restart"]

RETENTION_HOURS: dict[str, int] = {
    "1d": 24,
    "1w": 168,
    "1m": 720,
    "1y": 8760,
    "forever": 999_999,
    "restart": 0,
}


@dataclass
class AppSettings:
    setup_complete: bool = False
    display_name: str = "srltcp-node"
    web_port: int = WEB_PORT
    tcp_port: int = DEFAULT_TCP_PORT
    discovery_port: int = DISCOVERY_PORT
    strict_ports: bool = True
    message_retention_hours: int = 168
    message_retention_preset: str = "1w"
    incoming_files_dir: str = ""
    shared_folder: str = ""
    auto_announce: bool = False
    enable_serial: bool = False
    serial_port: str = ""
    serial_baud: int = 57600
    lan_ip: str = ""
    bind_interface: str = ""
    timezone: str = ""
    show_clock: bool = True
    clock_source: str = "system"  # system | ntp
    ntp_server: str = "pool.ntp.org"
    wan_expose_port: bool = False
    version: str = "0.1.39"

    def resolved_incoming_dir(self) -> Path:
        if self.incoming_files_dir:
            return Path(self.incoming_files_dir).expanduser().resolve()
        return data_dir() / "transfers" / DEFAULT_INCOMING

    def resolved_shared_folder(self) -> Path:
        if self.shared_folder:
            return Path(self.shared_folder).expanduser().resolve()
        return data_dir() / DEFAULT_SHARED

    def apply_retention_preset(self) -> None:
        hours = RETENTION_HOURS.get(self.message_retention_preset)
        if hours is not None:
            self.message_retention_hours = hours

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppSettings:
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        settings = cls(**filtered)
        if "message_retention_preset" in filtered:
            settings.apply_retention_preset()
        return settings


class SettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (data_dir() / "settings.json")

    def load(self) -> AppSettings:
        if not self.path.exists():
            settings = AppSettings()
            self._ensure_dirs(settings)
            return settings
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            settings = AppSettings.from_dict(data)
            self._ensure_dirs(settings)
            return settings
        except (json.JSONDecodeError, TypeError, ValueError):
            settings = AppSettings()
            self._ensure_dirs(settings)
            return settings

    def save(self, settings: AppSettings) -> None:
        settings.apply_retention_preset()
        ensure_dir(self.path.parent)
        self.path.write_text(
            json.dumps(settings.to_dict(), indent=2),
            encoding="utf-8",
        )
        self._ensure_dirs(settings)

    def _ensure_dirs(self, settings: AppSettings) -> None:
        ensure_dir(settings.resolved_incoming_dir())
        ensure_dir(settings.resolved_shared_folder())


def prune_messages_by_retention(
    messages: list[Any],
    retention_hours: int,
) -> list[Any]:
    """Drop messages older than retention window."""
    if retention_hours <= 0:
        return messages
    if retention_hours >= 999_999:
        return messages
    cutoff = time.time() - (retention_hours * 3600)
    return [m for m in messages if getattr(m, "timestamp", 0) >= cutoff]
