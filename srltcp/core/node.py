"""SRLTCP node — ties messaging backend to web server and share sessions."""

from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from srltcp import __version__
from srltcp.core.messaging.backend import MessagingBackend, NodeConfig
from srltcp.core.settings import SettingsStore
from srltcp.core.trusted import TrustedPeer
from srltcp.utils.files import ensure_dir, walk_directory
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

    def __init__(self, config: NodeConfig, settings: SettingsStore | None = None) -> None:
        self.config = config
        self.settings = settings or SettingsStore()
        self.backend = MessagingBackend(config)
        self._share_sessions: dict[str, ShareSession] = {}
        self._ws_clients: set[Any] = set()
        self._apply_directories()

    def _apply_directories(self) -> None:
        incoming = self.settings.settings.incoming_dir()
        shared = self.settings.settings.shared_dir()
        ensure_dir(incoming)
        ensure_dir(shared)
        self.backend._incoming_dir = incoming
        self.backend._transfer_dir = incoming
        ensure_dir(self.backend._transfer_dir)

    async def start(self) -> None:
        hours = self.settings.settings.retention_hours()
        if hours == 0:
            self.backend.prune_messages(0)
        elif hours is not None:
            self.backend.prune_messages(hours)
        await self.backend.start()

    async def stop(self) -> None:
        if self.settings.settings.retention_hours() == 0:
            self.backend.prune_messages(0)
        await self.backend.stop()

    async def apply_settings(self, **kwargs: Any) -> dict[str, Any]:
        old_announce = self.settings.settings.auto_announce
        self.settings.update(**kwargs)
        s = self.settings.settings
        self.config.name = s.display_name
        self.config.tcp_port = s.tcp_port
        self.config.announce = s.auto_announce
        self.config.enable_serial = s.enable_serial
        self.config.serial_port = s.serial_port
        self.config.serial_baud = s.serial_baud
        self.config.relay_mode = s.relay_mode
        self._apply_directories()
        if s.auto_announce != old_announce:
            await self.backend.set_auto_announce(s.auto_announce)
        hours = s.retention_hours()
        if hours == 0:
            self.backend.prune_messages(0)
        elif hours is not None:
            self.backend.prune_messages(hours)
        return s.to_dict()

    def add_trusted_from_discovered(self, hash_id: str) -> dict[str, Any] | None:
        peer = self.backend.discovery.get(hash_id)
        if not peer:
            return None
        trusted = TrustedPeer(
            hash_id=peer.hash_id,
            name=peer.name,
            transport=peer.transport,
            public_key=peer.public_key,
            tcp_host=peer.tcp_host,
            tcp_port=peer.tcp_port,
        )
        return self.backend.trusted.add(trusted).to_dict()

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
        s = self.settings.settings
        return {
            "name": self.config.name,
            "version": __version__,
            "relay_mode": self.config.relay_mode,
            "auto_announce": self.config.announce,
            "identities": self.backend.get_identities(),
            "links": self.backend.list_links(),
            "peers": self.backend.get_discovered_peers(),
            "trusted": self.backend.get_trusted_peers(),
            "transfers": self.backend.list_transfers(),
            "settings": s.to_dict(),
            "routes": self.backend.routing.all_routes() if self.config.relay_mode else [],
        }