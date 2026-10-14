"""On-disk chat history (``messages.json`` in the data dir, encrypted at rest)."""

from __future__ import annotations

import contextlib
from pathlib import Path

from srltcp.core.messaging.models import ChatMessage
from srltcp.utils.jsonstore import load_json, save_json
from srltcp.utils.logging import get_logger
from srltcp.utils.platform import data_dir

log = get_logger(__name__)

MAX_STORED_MESSAGES = 20_000


class MessageStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (data_dir() / "messages.json")

    def load(self) -> list[ChatMessage]:
        raw = load_json(self.path, {})
        messages: list[ChatMessage] = []
        for item in raw.get("messages", []) if isinstance(raw, dict) else []:
            msg = ChatMessage.from_dict(item)
            if msg:
                messages.append(msg)
        return messages[-MAX_STORED_MESSAGES:]

    def save(self, messages: list[ChatMessage]) -> None:
        data = {"version": 1, "messages": [m.to_dict() for m in messages[-MAX_STORED_MESSAGES:]]}
        save_json(self.path, data)

    def clear(self) -> None:
        with contextlib.suppress(OSError):
            self.path.unlink(missing_ok=True)
