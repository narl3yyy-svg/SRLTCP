"""SRLTCP node — ties messaging backend to web server and share sessions."""

from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from srltcp.core.messaging.backend import MessagingBackend, NodeConfig
from srltcp.utils.files import walk_directory
from srltcp.utils.logging import get_logger

log = get_logger(__name__)


@dataclass
class ShareSession:
    id: str
    path: Path
    token: str
    owner_hash: str
    created: float = field(default_factory=time.time)
    expires: float = field(default_factory=lambda: time.time() + 7200)

    def is_valid(self, token: str) -> bool:
        return secrets.compare_digest(self.token, token) and time.time() < self.expires

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": str(self.path),
            "owner_hash": self.owner_hash,
            "created": self.created,
            "expires": self.expires,
        }


class SRLTCPNode:
    """Top-level node combining messaging, sharing, and relay."""

    def __init__(self, config: NodeConfig) -> None:
        self.config = config
        self.backend = MessagingBackend(config)
        self._share_sessions: dict[str, ShareSession] = {}
        self._ws_clients: set[Any] = set()

    async def start(self) -> None:
        await self.backend.start()

    async def stop(self) -> None:
        await self.backend.stop()

    def create_share_session(self, folder: Path, owner_hash: str) -> ShareSession:
        session = ShareSession(
            id=uuid.uuid4().hex[:16],
            path=folder.resolve(),
            token=secrets.token_urlsafe(32),
            owner_hash=owner_hash,
        )
        self._share_sessions[session.id] = session
        return session

    def get_share_session(self, session_id: str) -> ShareSession | None:
        session = self._share_sessions.get(session_id)
        if session and time.time() >= session.expires:
            del self._share_sessions[session_id]
            return None
        return session

    def list_share(self, session_id: str, token: str) -> list[dict[str, object]] | None:
        session = self.get_share_session(session_id)
        if not session or not session.is_valid(token):
            return None
        return walk_directory(session.path)

    def resolve_share_path(self, session_id: str, token: str, rel_path: str) -> Path | None:
        session = self.get_share_session(session_id)
        if not session or not session.is_valid(token):
            return None
        target = (session.path / rel_path).resolve()
        if not str(target).startswith(str(session.path)):
            return None
        return target

    def status(self) -> dict[str, Any]:
        return {
            "name": self.config.name,
            "relay_mode": self.config.relay_mode,
            "identities": self.backend.get_identities(),
            "links": self.backend.list_links(),
            "peers": self.backend.get_discovered_peers(),
            "transfers": self.backend.list_transfers(),
            "routes": self.backend.routing.all_routes() if self.config.relay_mode else [],
        }