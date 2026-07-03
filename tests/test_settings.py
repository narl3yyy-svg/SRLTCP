"""Settings store tests."""

from __future__ import annotations

from pathlib import Path

from srltcp.core.messaging.models import ChatMessage
from srltcp.core.settings import AppSettings, SettingsStore, prune_messages_by_retention


def test_settings_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    settings = AppSettings(display_name="test-node", web_port=9876, setup_complete=True)
    store.save(settings)
    loaded = store.load()
    assert loaded.display_name == "test-node"
    assert loaded.web_port == 9876
    assert loaded.setup_complete is True


def test_message_prune() -> None:
    import time

    old = ChatMessage.create("a", "b", "old", "tcp")
    old.timestamp = time.time() - 999999
    new = ChatMessage.create("a", "b", "new", "tcp")
    result = prune_messages_by_retention([old, new], retention_hours=24)
    assert len(result) == 1
    assert result[0].text == "new"