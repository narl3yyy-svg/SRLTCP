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
from srltcp.core.settings import AppSettings
from srltcp.core.trusted import TrustedPeer
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

    def __init__(self, config: NodeConfig, settings: AppSettings) -> None:
        self.config = config
        self.settings = settings
        self.backend = MessagingBackend(config)
        self._share_sessions: dict[str, ShareSession] = {}
        self._ws_clients: set[Any] = set()

    async def start(self) -> None:
        if self.settings.message_retention_hours == 0:
            self.backend.clear_messages()
        await self.backend.start()

    async def close_websockets(self) -> None:
        """Close browser WebSocket connections so the web server can exit."""
        dead: list[Any] = []
        for ws in list(self._ws_clients):
            try:
                await ws.close()
            except Exception:
                dead.append(ws)
        self._ws_clients -= set(dead)
        self._ws_clients.clear()

    async def stop(self) -> None:
        if self.settings.message_retention_hours == 0:
            self.backend.clear_messages()
        await self.backend.stop()

    async def apply_settings(self, settings: AppSettings) -> None:
        """Hot-apply settings that don't require transport restart."""
        old_announce = self.config.announce
        self.settings = settings
        self.config.name = settings.display_name
        self.config.announce = settings.auto_announce
        self.config.lan_ip = settings.lan_ip
        self.config.message_retention_hours = settings.message_retention_hours
        self.config.enable_serial = settings.enable_serial
        self.config.serial_port = settings.serial_port
        self.config.serial_baud = settings.serial_baud
        incoming = str(settings.resolved_incoming_dir())
        self.config.incoming_dir = incoming
        if hasattr(self.backend, "_transfer_dir"):
            self.backend._transfer_dir = Path(incoming)
        for identity in self.backend.identities.values():
            if identity.name != settings.display_name:
                identity.name = settings.display_name
                self.backend.identity_store.save(identity)
        if settings.auto_announce != old_announce:
            await self.backend.set_auto_announce(settings.auto_announce)
        await self.backend.apply_serial_transport()
        if settings.message_retention_hours == 0:
            self.backend.clear_messages()

    def add_trusted_from_discovered(
        self, hash_id: str, transport: str | None = None
    ) -> dict[str, Any] | None:
        peer = self.backend.discovery.get(hash_id, transport) or self.backend.discovery.get(
            hash_id
        )
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
        result = self.backend.trusted.add(trusted).to_dict()
        self.backend.discovery.remove(hash_id)
        return result

    def create_share_session(self, folder: Path | None, owner_hash: str) -> ShareSession:
        root = folder or self.settings.resolved_shared_folder()
        session = ShareSession(
            id=uuid.uuid4().hex[:16],
            path=root.resolve(),
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
            "trusted": self.backend.get_trusted_peers(),
            "transfers": self.backend.list_transfers(),
            "routes": self.backend.routing.all_routes() if self.config.relay_mode else [],
            "settings": self.settings.to_dict(),
            "web_port": self.settings.web_port,
            "version": __version__,
            "auto_announce": self.config.announce,
            "transports": self.backend.get_transport_status(),
        }