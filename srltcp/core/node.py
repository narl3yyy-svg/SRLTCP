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

CALL_RING_SECONDS = 45.0  # same as the web UI's ring timeout


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
    """Top-level node combining messaging, sharing, and optional hub connectivity."""

    def __init__(self, config: NodeConfig, settings: AppSettings) -> None:
        self.config = config
        self.settings = settings
        self.backend = MessagingBackend(config)
        self._share_sessions: dict[str, ShareSession] = {}
        self._ws_clients: set[Any] = set()
        # The incoming call ringing right now: its signals are kept so a page
        # opened from the ringing notification still gets the call.
        self.pending_call: dict[str, Any] | None = None

    async def start(self) -> None:
        from srltcp.core.backup import APP_FILES
        from srltcp.utils import vault

        # Files from before encryption at rest (0.9): sealed once, here.
        root = self.backend.trusted.path.parent
        sealed = vault.seal_existing([root / name for name in APP_FILES])
        if sealed:
            log.info("Encrypted %d data file(s) at rest", sealed)
        if self.settings.message_retention_hours == 0:
            self.backend.clear_messages()
        await self.backend.start()

    # -- Ringing ---------------------------------------------------------

    def track_call_signal(
        self, hash_id: str, signal: dict[str, Any], event: dict[str, Any] | None = None
    ) -> None:
        """Follow call signals: an incoming offer starts ringing (with the
        phone's ringtone on Android) until it is answered, declined,
        cancelled or times out. ``event`` is set for signals from the peer."""
        from srltcp.utils.android_notify import ring_android_call, stop_android_ringing

        kind = str(signal.get("type", ""))
        call_id = str(signal.get("call_id", ""))
        pending = self.pending_call
        if event is not None and kind == "offer" and call_id:
            if pending and pending["hash_id"] != hash_id:
                return  # already ringing for someone else; the UI answers "busy"
            peer = self.backend.trusted.get(hash_id)
            self.pending_call = {
                "hash_id": hash_id, "call_id": call_id, "at": time.monotonic(),
                "events": [event],
            }
            ring_android_call(peer.name if peer else hash_id[:8], bool(signal.get("video")))
            return
        if not pending or pending["hash_id"] != hash_id or pending["call_id"] != call_id:
            return
        if event is not None and kind == "ice":
            pending["events"].append(event)
        elif kind in ("answer", "hangup", "busy"):
            self.pending_call = None  # answered here, or ended by either side
            stop_android_ringing()

    def pending_call_events(self) -> list[dict[str, Any]]:
        """Signals of a call still ringing, for a page that just opened."""
        pending = self.pending_call
        if not pending:
            return []
        if time.monotonic() - pending["at"] > CALL_RING_SECONDS:
            self.pending_call = None
            return []
        return list(pending["events"])

    async def decline_pending_call(self) -> bool:
        """Declined from the ringing notification: tell the caller and any open page."""
        from srltcp.routes.ws import broadcast_event
        from srltcp.utils.android_notify import stop_android_ringing

        pending, self.pending_call = self.pending_call, None
        stop_android_ringing()
        if not pending:
            return False
        hangup = {"type": "hangup", "call_id": pending["call_id"], "reason": "declined"}
        try:
            await self.backend.send_call_signal(pending["hash_id"], hangup)
        except (KeyError, OSError, ConnectionError, RuntimeError) as exc:
            log.info("Couldn't tell the caller the call was declined: %s", exc)
        # An open page closes its incoming-call screen.
        await broadcast_event(self, "call_signal", {
            "kind": "call_signal", "hash_id": pending["hash_id"], "signal": hangup,
        })
        return True

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
        self.config.disappear_seconds = settings.disappear_seconds
        self.config.auto_download_mb = settings.auto_download_mb
        self.config.auto_download_radio_mb = settings.auto_download_radio_mb
        self.config.enable_serial = settings.enable_serial
        self.config.serial_port = settings.serial_port
        self.config.serial_baud = settings.serial_baud
        direct_changed = (
            self.config.direct_internet != settings.direct_internet
            or self.config.router_port_mapping != settings.router_port_mapping
        )
        self.config.direct_internet = settings.direct_internet
        self.config.router_port_mapping = settings.router_port_mapping
        tor_changed = (
            self.config.tor_enabled != settings.tor_enabled
            or self.config.tor_path != settings.tor_path
        )
        self.config.tor_enabled = settings.tor_enabled
        self.config.tor_path = settings.tor_path
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
        if direct_changed:
            await self.backend.port_mapping_changed()
            await self.backend.addresses_changed()
        if tor_changed:
            await self.backend.tor_settings_changed()
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
        self.backend.trusted.add(trusted)
        link = self.backend.get_link(hash_id)
        if link and link.handshake_complete and link.pq_public:
            # Already connected: pin the post-quantum key this session proved.
            self.backend._pq_key_ok(hash_id, link.pq_public)
        self.backend.discovery.remove(hash_id)
        return self.backend.trusted_view(hash_id)

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
        if not target.is_relative_to(session.path):
            return None
        return target

    def status(self) -> dict[str, Any]:
        return {
            "name": self.config.name,
            "tor": self.backend.tor_status(),
            "identities": self.backend.get_identities(),
            "links": self.backend.list_links(),
            "peers": self.backend.get_discovered_peers(),
            "trusted": self.backend.get_trusted_peers(),
            "transfers": self.backend.list_transfers(),
            "settings": self.settings.to_dict(),
            "web_port": self.settings.web_port,
            "version": __version__,
            "auto_announce": self.config.announce,
            "transports": self.backend.get_transport_status(),
        }