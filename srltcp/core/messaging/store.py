"""On-disk chat history (``messages.json`` in the data dir, owner-only permissions)."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

from srltcp.core.messaging.models import ChatMessage
from srltcp.utils.logging import get_logger
from srltcp.utils.platform import data_dir

log = get_logger(__name__)

MAX_STORED_MESSAGES = 20_000


class MessageStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (data_dir() / "messages.json")

    def load(self) -> list[ChatMessage]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Could not read message history (%s) — starting empty", exc)
            return []
        messages: list[ChatMessage] = []
        for item in raw.get("messages", []) if isinstance(raw, dict) else []:
            msg = ChatMessage.from_dict(item)
            if msg:
                messages.append(msg)
        return messages[-MAX_STORED_MESSAGES:]

    def save(self, messages: list[ChatMessage]) -> None:
        data = {"version": 1, "messages": [m.to_dict() for m in messages[-MAX_STORED_MESSAGES:]]}
        tmp = self.path.with_suffix(".json.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, separators=(",", ":"))
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("Could not save message history: %s", exc)
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)

    def clear(self) -> None:
        with contextlib.suppress(OSError):
            self.path.unlink(missing_ok=True)
