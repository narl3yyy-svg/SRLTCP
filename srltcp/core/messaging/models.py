"""Domain models for messages and transfers."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class TransferState(StrEnum):
    PENDING = "pending"
    OFFERED = "offered"
    ACCEPTED = "accepted"
    TRANSFERRING = "transferring"
    COMPLETE = "complete"
    REJECTED = "rejected"
    FAILED = "failed"
    PAUSED = "paused"


@dataclass
class ChatMessage:
    id: str
    sender_hash: str
    recipient_hash: str
    text: str
    transport: str
    timestamp: float = field(default_factory=time.time)
    status: str = "sent"  # sent | delivered | read | pending
    msg_type: str = "text"
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        sender_hash: str,
        recipient_hash: str,
        text: str,
        transport: str,
        *,
        msg_type: str = "text",
        metadata: dict[str, Any] | None = None,
    ) -> ChatMessage:
        return cls(
            id=str(uuid.uuid4()),
            sender_hash=sender_hash,
            recipient_hash=recipient_hash,
            text=text,
            transport=transport,
            msg_type=msg_type,
            metadata=metadata or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sender_hash": self.sender_hash,
            "recipient_hash": self.recipient_hash,
            "text": self.text,
            "transport": self.transport,
            "timestamp": self.timestamp,
            "status": self.status,
            "msg_type": self.msg_type,
            "metadata": self.metadata,
        }


@dataclass
class FileTransfer:
    id: str
    sender_hash: str
    recipient_hash: str
    filename: str
    path: Path
    size: int
    sha256: str
    transport: str
    state: TransferState = TransferState.PENDING
    offset: int = 0
    compressed: bool = False
    created: float = field(default_factory=time.time)
    speed_mbps: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        sender_hash: str,
        recipient_hash: str,
        path: Path,
        transport: str,
        *,
        sha256: str = "",
        compressed: bool = False,
    ) -> FileTransfer:
        return cls(
            id=uuid.uuid4().hex[:16],
            sender_hash=sender_hash,
            recipient_hash=recipient_hash,
            filename=path.name,
            path=path,
            size=path.stat().st_size if path.exists() else 0,
            sha256=sha256,
            transport=transport,
            compressed=compressed,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sender_hash": self.sender_hash,
            "recipient_hash": self.recipient_hash,
            "filename": self.filename,
            "size": self.size,
            "sha256": self.sha256,
            "transport": self.transport,
            "state": self.state.value,
            "offset": self.offset,
            "compressed": self.compressed,
            "created": self.created,
            "speed_mbps": round(self.speed_mbps, 2),
            "metadata": self.metadata,
        }