"""MessagingBackend — orchestrator composing all mixins."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import zstandard as zstd

from srltcp.core.discovery import DiscoveryRegistry
from srltcp.core.identity import Identity, IdentityStore
from srltcp.core.messaging.announce import AnnounceMixin
from srltcp.core.messaging.connect import ConnectMixin
from srltcp.core.messaging.constants import COMPRESS_THRESHOLD, DISCOVERY_PORT
from srltcp.core.messaging.links import PeerLink, PeerLinkMixin
from srltcp.core.messaging.models import ChatMessage
from srltcp.core.messaging.ping import PingMixin
from srltcp.core.messaging.queue import QueueMixin
from srltcp.core.messaging.relay import RelayMixin
from srltcp.core.messaging.share_peer import SharePeerMixin
from srltcp.core.messaging.transfer import TransferMixin
from srltcp.core.protocol.messages import (
    Flags,
    MessageType,
    build_header,
    decode_payload,
    encode_payload,
    parse_header,
)
from srltcp.core.settings import prune_messages_by_retention
from srltcp.core.trusted import TrustedStore
from srltcp.transports.base import Transport, TransportEvent, TransportPeer
from srltcp.transports.serial import SerialTransport
from srltcp.transports.tcp import TCPTransport
from srltcp.utils.logging import get_logger

log = get_logger(__name__)

EventCallback = Callable[..., Awaitable[None]]


@dataclass
class NodeConfig:
    name: str = "srltcp-node"
    bind_host: str = "0.0.0.0"
    tcp_port: int = 7825
    discovery_port: int = DISCOVERY_PORT
    strict_ports: bool = True
    relay_mode: bool = False
    enable_tcp: bool = True
    enable_serial: bool = False
    serial_port: str = ""
    serial_baud: int = 115200
    announce: bool = False
    lan_ip: str = ""
    incoming_dir: str = ""
    message_retention_hours: int = 168


class MessagingBackend(
    PeerLinkMixin,
    ConnectMixin,
    AnnounceMixin,
    PingMixin,
    QueueMixin,
    TransferMixin,
    SharePeerMixin,
    RelayMixin,
):
    """Central messaging orchestrator."""

    def __init__(self, config: NodeConfig) -> None:
        self.config = config
        self.identity_store = IdentityStore()
        self.trusted = TrustedStore()
        self.identities: dict[str, Identity] = {}
        self.discovery = DiscoveryRegistry()
        self.tcp_transport: TCPTransport | None = None
        self.serial_transport: SerialTransport | None = None
        self._serial_error: str | None = None
        self._running = False
        self._messages: list[ChatMessage] = []

        # Event callbacks for web UI
        self._on_message: EventCallback | None = None
        self._on_peer_discovered: EventCallback | None = None
        self._on_link_up: EventCallback | None = None
        self._on_file_offer: EventCallback | None = None
        self._on_transfer_progress: EventCallback | None = None
        self._on_transfer_complete: EventCallback | None = None
        self._on_peer_metrics: EventCallback | None = None
        self._on_link_down: EventCallback | None = None
        self._on_event: EventCallback | None = None
        self._reconnect_tasks: dict[str, asyncio.Task[None]] = {}

        self._init_links()
        self._init_connect()
        self._init_announce()
        self._init_ping()
        self._init_queue()
        self._init_transfer()
        self._init_share_peer()
        self._init_relay()

    def set_callbacks(
        self,
        *,
        on_message: EventCallback | None = None,
        on_peer_discovered: EventCallback | None = None,
        on_link_up: EventCallback | None = None,
        on_file_offer: EventCallback | None = None,
        on_transfer_progress: EventCallback | None = None,
        on_transfer_complete: EventCallback | None = None,
        on_peer_metrics: EventCallback | None = None,
        on_link_down: EventCallback | None = None,
        on_event: EventCallback | None = None,
    ) -> None:
        self._on_message = on_message
        self._on_peer_discovered = on_peer_discovered
        self._on_link_up = on_link_up
        self._on_file_offer = on_file_offer
        self._on_transfer_progress = on_transfer_progress
        self._on_transfer_complete = on_transfer_complete
        self._on_peer_metrics = on_peer_metrics
        self._on_link_down = on_link_down
        self._on_event = on_event

    async def start(self) -> None:
        if self._running:
            return
        self._running = True

        if self.config.enable_tcp:
            self.identities["tcp"] = self.identity_store.load_or_create(
                self.config.name, "tcp"
            )
            self.tcp_transport = TCPTransport(
                host=self.config.bind_host,
                port=self.config.tcp_port,
                discovery_port=self.config.discovery_port,
                strict_ports=self.config.strict_ports,
            )
            self._wire_transport(self.tcp_transport)

        if self.config.enable_serial:
            self.identities["serial"] = self.identity_store.load_or_create(
                self.config.name, "serial"
            )
            port = self.config.serial_port or None
            self.serial_transport = SerialTransport(port=port, baudrate=self.config.serial_baud)
            self._wire_transport(self.serial_transport)

        if self.tcp_transport:
            await self.tcp_transport.start()
            if not self.config.strict_ports:
                self.config.tcp_port = self.tcp_transport.port
        if self.serial_transport:
            try:
                await self.serial_transport.start()
                self._serial_error = None
            except Exception as exc:
                self._record_serial_failure(self.serial_transport.port, exc)
                self.serial_transport = None

        if self.config.announce:
            await self.start_announce_loop()
        await self.start_ping_loop()

        log.info(
            "MessagingBackend started (tcp=%s, serial=%s, relay=%s)",
            bool(self.tcp_transport),
            bool(self.serial_transport),
            self.config.relay_mode,
        )

    async def stop(self) -> None:
        self._running = False
        await self.stop_announce_loop()
        await self.stop_ping_loop()
        for task in list(self._reconnect_tasks.values()):
            task.cancel()
        if self._reconnect_tasks:
            await asyncio.gather(*self._reconnect_tasks.values(), return_exceptions=True)
        self._reconnect_tasks.clear()
        tasks = list(self._transfer_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.tcp_transport:
            log.info(
                "Closing TCP transport (port %d, discovery %d)",
                self.tcp_transport.port,
                self.tcp_transport.discovery_port,
            )
            await self.tcp_transport.stop()
            self.tcp_transport = None
        if self.serial_transport:
            log.info("Closing serial transport (%s)", self.serial_transport.port)
            await self.serial_transport.stop()
            self.serial_transport = None

    def _record_serial_failure(self, port: str, exc: Exception) -> None:
        from srltcp.utils.serial_access import format_serial_permission_help

        self._serial_error = format_serial_permission_help(port, exc)
        log.warning("%s", self._serial_error)

    async def ensure_serial_transport(self) -> dict[str, str]:
        """Open serial transport if enabled but not yet running."""
        if self.serial_transport:
            return {"serial": "running"}
        if not self.config.enable_serial:
            return {"serial": "disabled"}
        return await self.apply_serial_transport()

    async def apply_serial_transport(self) -> dict[str, str]:
        """Start, stop, or restart serial transport when settings change."""
        want = self.config.enable_serial
        port = self.config.serial_port or None
        baud = self.config.serial_baud

        if not want:
            if self.serial_transport:
                await self.serial_transport.stop()
                self.serial_transport = None
            self.identities.pop("serial", None)
            return {"serial": "stopped"}

        needs_restart = False
        if self.serial_transport:
            if (
                getattr(self.serial_transport, "port", None) != (port or "")
                or getattr(self.serial_transport, "baudrate", None) != baud
            ):
                await self.serial_transport.stop()
                self.serial_transport = None
                needs_restart = True
        else:
            needs_restart = True

        if needs_restart:
            self.identities["serial"] = self.identity_store.load_or_create(
                self.config.name, "serial"
            )
            self.serial_transport = SerialTransport(port=port, baudrate=baud)
            self._wire_transport(self.serial_transport)
            try:
                await self.serial_transport.start()
                self._serial_error = None
            except Exception as exc:
                port_name = port or self.serial_transport.port
                self._record_serial_failure(port_name, exc)
                self.serial_transport = None
                return {
                    "serial": "failed",
                    "error": self._serial_error or str(exc),
                }

        return {"serial": "running" if self.serial_transport else "failed"}

    def _schedule_reconnect(self, hash_id: str) -> None:
        if hash_id in self._reconnect_tasks or not self._running:
            return
        if not self.trusted.is_trusted(hash_id):
            return
        if self.has_active_transfer_for(hash_id):
            return

        async def _reconnect() -> None:
            delay = 5.0 if self.in_transfer_cooldown(hash_id) else 2.0
            try:
                for attempt in range(6):
                    await asyncio.sleep(delay)
                    if not self._running:
                        return
                    link = self.get_link(hash_id)
                    if link and link.handshake_complete:
                        return
                    if self.has_active_transfer_for(hash_id):
                        return
                    trusted = self.trusted.get(hash_id)
                    transport = trusted.transport if trusted else "tcp"
                    log.info(
                        "Auto-reconnecting to %s (attempt %d)",
                        hash_id[:8],
                        attempt + 1,
                    )
                    try:
                        await self.connect_to_peer(
                            hash_id, transport=transport, force=False
                        )
                    except (OSError, TimeoutError, ConnectionError) as exc:
                        log.debug(
                            "Reconnect attempt failed for %s: %s",
                            hash_id[:8],
                            exc,
                        )
                    else:
                        if await self.wait_for_handshake(hash_id, timeout=8.0):
                            return
                    delay = min(delay * 1.5, 30.0)
            finally:
                self._reconnect_tasks.pop(hash_id, None)

        self._reconnect_tasks[hash_id] = asyncio.create_task(_reconnect())

    def _wire_transport(self, transport: Transport) -> None:
        transport.on_frame(self._on_transport_frame)
        transport.on_event(self._on_transport_event)

    def _identity_for_transport(self, transport: str) -> Identity:
        identity = self.identities.get(transport) or self.identities.get("tcp")
        if not identity:
            raise RuntimeError("no identity for transport")
        return identity

    async def _on_transport_event(self, event: TransportEvent) -> None:
        link_hash: str | None = None
        link_name = ""
        if event.kind == "discovered" and event.data and event.peer:
            await self._handle_discovered(event.peer.address, "tcp", event.data)
        elif event.kind == "disconnected" and event.peer:
            stale = self.get_link_by_peer_id(event.peer.peer_id)
            link_name = stale.peer_name if stale else ""
            link_hash = self.remove_link_for_peer(event.peer.peer_id)
            if link_hash:
                if stale and not link_name:
                    link_name = stale.peer_name
                self._pending_handshakes.pop(link_hash, None)
                if self.config.relay_mode:
                    self.routing.remove_for_peer(event.peer.peer_id)
                transfer_active = self.has_active_transfer_for(link_hash)
                in_cooldown = self.in_transfer_cooldown(link_hash)
                if transfer_active or in_cooldown:
                    log.info(
                        "Suppressing link_down for %s — %s",
                        link_hash[:8],
                        "transfer active" if transfer_active else "post-transfer cooldown",
                    )
                elif self._on_link_down:
                    await self._on_link_down(link_hash, link_name)
                if not transfer_active:
                    self._schedule_reconnect(link_hash)
        if self._on_event:
            suppress_ui = (
                event.kind == "disconnected"
                and link_hash
                and (
                    self.has_active_transfer_for(link_hash)
                    or self.in_transfer_cooldown(link_hash)
                )
            )
            if not suppress_ui:
                await self._on_event(
                    {
                        "kind": event.kind,
                        "peer": event.peer.address if event.peer else None,
                        "hash_id": link_hash,
                        "name": link_name,
                        "error": event.error,
                    }
                )

    async def _on_transport_frame(self, peer: TransportPeer, payload: bytes) -> None:
        try:
            msg_type, flags, _stream_id, _seq, body = parse_header(payload)
        except Exception:
            log.debug("Invalid packet from %s", peer.peer_id[:8])
            return

        if msg_type == MessageType.ANNOUNCE:
            if peer.transport == "serial":
                log.info("Serial ANNOUNCE received on %s", peer.address)
            await self._handle_discovered(peer.address, peer.transport, body)
        elif msg_type == MessageType.HANDSHAKE:
            await self._handle_handshake(peer.peer_id, body, initiator=False)
        elif msg_type == MessageType.HANDSHAKE_ACK:
            await self._handle_handshake_ack(peer.peer_id, body)
        elif msg_type == MessageType.PING:
            await self._reply_pong_with_rtt(peer, body)
        elif msg_type == MessageType.PONG:
            await self._handle_pong(peer.peer_id, body)
        elif msg_type == MessageType.TEXT:
            await self._handle_text(peer.peer_id, body, flags)
        elif msg_type == MessageType.FILE_OFFER:
            link = self.get_link_by_peer_id(peer.peer_id)
            if link and flags & Flags.ENCRYPTED:
                body = link.crypto.decrypt(body)
            await self._handle_file_offer(
                link.hash_id if link else peer.peer_id, body
            )
        elif msg_type == MessageType.FILE_ACCEPT:
            link = self.get_link_by_peer_id(peer.peer_id)
            if link and flags & Flags.ENCRYPTED:
                body = link.crypto.decrypt(body)
            if link:
                await self._handle_file_accept(link.hash_id, body)
        elif msg_type == MessageType.FILE_REJECT:
            link = self.get_link_by_peer_id(peer.peer_id)
            if link and flags & Flags.ENCRYPTED:
                body = link.crypto.decrypt(body)
            if link:
                await self._handle_file_reject(link.hash_id, body)
        elif msg_type == MessageType.FILE_CHUNK:
            link = self.get_link_by_peer_id(peer.peer_id)
            if link:
                hash_id = link.hash_id
                compressed = bool(flags & Flags.COMPRESSED)

                async def _process_chunk() -> None:
                    try:
                        await self._handle_file_chunk(
                            hash_id, body, compressed=compressed
                        )
                    except Exception as exc:
                        log.warning(
                            "File chunk handler failed from %s: %s",
                            hash_id[:8],
                            exc,
                        )

                asyncio.create_task(_process_chunk())
        elif msg_type == MessageType.FILE_COMPLETE:
            link = self.get_link_by_peer_id(peer.peer_id)
            if link and flags & Flags.ENCRYPTED:
                body = link.crypto.decrypt(body)
            if link:
                await self._handle_file_complete(link.hash_id, body)
        elif msg_type == MessageType.FILE_RESUME:
            link = self.get_link_by_peer_id(peer.peer_id)
            if link and flags & Flags.ENCRYPTED:
                body = link.crypto.decrypt(body)
            if link:
                await self._handle_file_resume(link.hash_id, body)
        elif msg_type == MessageType.SHARE_LIST:
            link = self.get_link_by_peer_id(peer.peer_id)
            if link and flags & Flags.ENCRYPTED:
                body = link.crypto.decrypt(body)
            if link:
                await self._handle_share_list(link.hash_id, body)
        elif msg_type == MessageType.SHARE_REQUEST:
            link = self.get_link_by_peer_id(peer.peer_id)
            if link and flags & Flags.ENCRYPTED:
                body = link.crypto.decrypt(body)
            if link:
                await self._handle_share_request(link.hash_id, body)
        elif msg_type == MessageType.RELAY_ENVELOPE:
            await self._handle_relay_envelope(peer.peer_id, body)
        elif msg_type == MessageType.ROUTE_UPDATE:
            await self._handle_route_update(peer.peer_id, body)

    async def _dispatch_encrypted(self, peer_id: str, inner: bytes) -> None:
        try:
            msg_type, flags, _, _, body = parse_header(inner)
        except Exception:
            return
        link = self.get_link_by_peer_id(peer_id)
        if msg_type == MessageType.TEXT and link:
            await self._handle_text(peer_id, body, flags)

    def is_trusted(self, hash_id: str) -> bool:
        peer = self.trusted.get(hash_id)
        return bool(peer and not peer.blocked)

    def delete_message(self, message_id: str) -> bool:
        before = len(self._messages)
        self._messages = [m for m in self._messages if m.id != message_id]
        return len(self._messages) < before

    def clear_messages_for_peer(self, hash_id: str) -> int:
        before = len(self._messages)
        self._messages = [
            m
            for m in self._messages
            if m.sender_hash != hash_id and m.recipient_hash != hash_id
        ]
        return before - len(self._messages)

    async def _handle_text(self, peer_id: str, body: bytes, flags: int) -> None:
        link = self.get_link_by_peer_id(peer_id)
        if not link or not link.handshake_complete:
            return
        if flags & Flags.ENCRYPTED:
            try:
                body = link.crypto.decrypt(body)
            except Exception:
                return
        if flags & Flags.COMPRESSED:
            with contextlib.suppress(Exception):
                body = zstd.ZstdDecompressor().decompress(body)
        data = decode_payload(body)
        msg = ChatMessage.create(
            sender_hash=link.hash_id,
            recipient_hash=self._identity_for_transport(link.transport).hash_id,
            text=data.get("text", ""),
            transport=link.transport,
            metadata=data.get("metadata", {}),
        )
        self._messages.append(msg)
        if self._on_message:
            await self._on_message(msg.to_dict())

    async def send_message(
        self,
        recipient_hash: str,
        text: str,
        *,
        transport: str = "tcp",
    ) -> ChatMessage | None:
        if not self.trusted.is_trusted(recipient_hash):
            log.warning("Recipient %s not in trusted list", recipient_hash[:8])
            return None
        link = self.get_link(recipient_hash)
        if not link or not link.handshake_complete:
            await self.connect_to_peer(recipient_hash, transport=transport, force=False)
            await self.wait_for_handshake(recipient_hash, timeout=8.0)
            link = self.get_link(recipient_hash)
        identity = self._identity_for_transport(transport)
        msg = ChatMessage.create(
            sender_hash=identity.hash_id,
            recipient_hash=recipient_hash,
            text=text,
            transport=transport,
        )
        if not link or not link.handshake_complete:
            msg.status = "pending"
            await self.enqueue_message(msg)
            self._messages.append(msg)
            return msg

        body = encode_payload({"text": text})
        flags = Flags.ENCRYPTED | Flags.E2EE
        if len(body) >= COMPRESS_THRESHOLD:
            compressed = zstd.ZstdCompressor(level=3).compress(body)
            if len(compressed) < len(body):
                body = compressed
                flags |= Flags.COMPRESSED
        encrypted = link.crypto.encrypt(body)
        packet = build_header(MessageType.TEXT, flags=flags, body=encrypted)
        await self._send_raw(link.transport_peer_id, link.transport, packet)
        self._messages.append(msg)
        if self._on_message:
            await self._on_message(msg.to_dict())
        return msg

    async def _encrypt_for_link(
        self, link: PeerLink, msg_type: MessageType, body: bytes
    ) -> bytes:
        encrypted = link.crypto.encrypt(body)
        return build_header(msg_type, flags=Flags.ENCRYPTED | Flags.E2EE, body=encrypted)

    async def _send_raw(self, peer_id: str, transport: str, packet: bytes) -> None:
        if transport == "tcp" and self.tcp_transport:
            await self.tcp_transport.send(peer_id, packet)
        elif transport == "serial" and self.serial_transport:
            await self.serial_transport.send(peer_id, packet)

    def get_identities(self) -> dict[str, dict[str, str]]:
        return {
            t: {
                "name": i.name,
                "hash_id": i.hash_id,
                "transport": i.transport,
                "public_key": i.public_bytes().hex(),
            }
            for t, i in self.identities.items()
        }

    def get_transport_status(self) -> dict[str, dict[str, Any]]:
        from srltcp.core.messaging.constants import DISCOVERY_PORT

        status: dict[str, dict[str, Any]] = {}
        if self.identities.get("tcp") or self.tcp_transport:
            status["tcp"] = {
                "active": self.tcp_transport is not None,
                "tcp_port": self.tcp_transport.port if self.tcp_transport else self.config.tcp_port,
                "discovery_port": (
                    self.tcp_transport.discovery_port
                    if self.tcp_transport
                    else DISCOVERY_PORT
                ),
            }
        if self.identities.get("serial") or self.config.enable_serial:
            serial_port = (
                self.serial_transport.port
                if self.serial_transport
                else self.config.serial_port
            )
            status["serial"] = {
                "active": self.serial_transport is not None,
                "port": serial_port,
                "baud": self.config.serial_baud,
                "error": self._serial_error,
            }
        return status

    def _prune_messages(self) -> None:
        self._messages = prune_messages_by_retention(
            self._messages, self.config.message_retention_hours
        )

    def get_messages(self, *, limit: int = 200) -> list[dict[str, Any]]:
        self._prune_messages()
        return [m.to_dict() for m in self._messages[-limit:]]

    def get_discovered_peers(self) -> list[dict[str, Any]]:
        own_hashes = {i.hash_id for i in self.identities.values()}
        own_keys = {i.public_bytes().hex() for i in self.identities.values()}
        trusted_hashes = {t.hash_id for t in self.trusted.list_peers()}
        peers = []
        for p in self.discovery.list_peers():
            if p.hash_id in own_hashes or p.public_key in own_keys:
                self.discovery.remove(p.hash_id)
                continue
            if p.hash_id in trusted_hashes:
                continue
            d = p.to_dict()
            link = self.get_link(p.hash_id)
            if link and link.rtt_ms is not None:
                d["rtt_ms"] = link.rtt_ms
            peers.append(d)
        return peers

    def get_trusted_peers(self) -> list[dict[str, Any]]:
        from srltcp.core.trusted import _GENERIC_NAMES, is_valid_hash_id

        result = []
        seen: set[str] = set()
        for p in self.trusted.list_peers():
            if not is_valid_hash_id(p.hash_id) or p.hash_id in seen:
                continue
            seen.add(p.hash_id)
            name = p.name
            if name.strip().lower() in _GENERIC_NAMES:
                discovered = self.discovery.get(p.hash_id, p.transport) or self.discovery.get(
                    p.hash_id
                )
                if discovered and discovered.name:
                    name = discovered.name
                elif p.hash_id in self._links:
                    name = f"Peer {p.hash_id[:8]}"
            d = p.to_dict()
            d["name"] = name
            link = self.get_link(p.hash_id)
            if link:
                if link.rtt_ms is not None:
                    d["rtt_ms"] = link.rtt_ms
                if link.link_quality_pct is not None:
                    d["link_quality_pct"] = link.link_quality_pct
            result.append(d)
        return result

    def clear_messages(self) -> int:
        count = len(self._messages)
        self._messages.clear()
        return count

    def _file_msg_type(self, filename: str) -> str:
        ext = Path(filename).suffix.lower()
        if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"):
            return "image"
        if ext in (".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v", ".ogv"):
            return "video"
        return "file"

    async def _append_file_message(
        self,
        *,
        sender_hash: str,
        recipient_hash: str,
        transport: str,
        transfer: dict[str, Any],
        direction: str,
    ) -> None:
        msg = ChatMessage.create(
            sender_hash=sender_hash,
            recipient_hash=recipient_hash,
            text=transfer.get("filename", "file"),
            transport=transport,
            msg_type=self._file_msg_type(transfer.get("filename", "")),
            metadata={
                "transfer_id": transfer.get("id"),
                "filename": transfer.get("filename"),
                "size": transfer.get("size"),
                "sha256": transfer.get("sha256"),
                "state": transfer.get("state"),
                "offset": transfer.get("offset", 0),
                "speed_mbps": transfer.get("speed_mbps", 0),
                "direction": direction,
                "is_folder_zip": transfer.get("metadata", {}).get("is_folder_zip"),
                "folder_name": transfer.get("metadata", {}).get("folder_name"),
            },
        )
        self._messages.append(msg)
        if self._on_message:
            await self._on_message(msg.to_dict())

    async def _update_file_message(
        self, transfer: dict[str, Any], *, notify: bool = True
    ) -> None:
        transfer_id = transfer.get("id")
        for msg in reversed(self._messages):
            if msg.metadata.get("transfer_id") == transfer_id:
                msg.metadata.update(
                    {
                        "state": transfer.get("state"),
                        "offset": transfer.get("offset", 0),
                        "speed_mbps": transfer.get("speed_mbps", 0),
                        "filename": transfer.get("filename", msg.metadata.get("filename")),
                    }
                )
                meta = transfer.get("metadata") or {}
                if meta.get("is_folder_zip"):
                    msg.metadata["is_folder_zip"] = True
                    msg.metadata["folder_name"] = meta.get("folder_name")
                if notify and self._on_message:
                    await self._on_message(msg.to_dict())
                return

    async def send_file(
        self,
        recipient_hash: str,
        path: Path,
        *,
        transport: str = "tcp",
        filename: str | None = None,
    ) -> dict[str, Any] | None:
        if not self.trusted.is_trusted(recipient_hash):
            log.warning("Recipient %s not trusted for file send", recipient_hash[:8])
            return None
        link = self.get_link(recipient_hash)
        effective_transport = link.transport if link else transport
        if not link or not link.handshake_complete:
            await self.connect_to_peer(
                recipient_hash, transport=effective_transport, force=False
            )
            await self.wait_for_handshake(recipient_hash, timeout=10.0)
            link = self.get_link(recipient_hash)
        if not link or not link.handshake_complete:
            log.warning("No active link to %s for file send", recipient_hash[:8])
            return None
        transfer = await self.offer_file(
            recipient_hash,
            path,
            transport=link.transport,
            filename=filename,
        )
        if not transfer:
            return None
        data = transfer.to_dict()
        identity = self._identity_for_transport(link.transport)
        await self._append_file_message(
            sender_hash=identity.hash_id,
            recipient_hash=recipient_hash,
            transport=link.transport,
            transfer=data,
            direction="out",
        )
        return data

    async def send_folder(
        self, recipient_hash: str, folder: Path, *, transport: str = "tcp"
    ) -> dict[str, Any] | None:
        from srltcp.utils.files import FolderZipError, zip_path_to_temp_async

        root = folder.resolve()
        if not root.is_dir():
            return None
        zip_path: Path | None = None
        try:
            zip_path = await zip_path_to_temp_async(root)
            display_name = f"{root.name}.zip"
            result = await self.send_file(
                recipient_hash,
                zip_path,
                transport=transport,
                filename=display_name,
            )
            if result:
                transfer = self._transfers.get(result.get("id", ""))
                if transfer:
                    transfer.metadata["folder_name"] = root.name
                    transfer.metadata["is_folder_zip"] = True
                    transfer.metadata["temp_zip_path"] = str(zip_path)
                    result = transfer.to_dict()
                    await self._update_file_message(result)
                return result
            if zip_path:
                zip_path.unlink(missing_ok=True)
            return None
        except FolderZipError:
            if zip_path:
                zip_path.unlink(missing_ok=True)
            raise
        except OSError as exc:
            if zip_path:
                zip_path.unlink(missing_ok=True)
            log.warning("Folder zip failed for %s: %s", root, exc)
            raise FolderZipError(f"Could not zip folder: {exc}") from exc