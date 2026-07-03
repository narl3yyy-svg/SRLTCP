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
from srltcp.core.messaging.constants import COMPRESS_THRESHOLD
from srltcp.core.messaging.links import PeerLinkMixin
from srltcp.core.messaging.models import ChatMessage
from srltcp.core.messaging.ping import PingMixin
from srltcp.core.messaging.queue import QueueMixin
from srltcp.core.messaging.relay import RelayMixin
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
        self._running = False
        self._messages: list[ChatMessage] = []

        # Event callbacks for web UI
        self._on_message: EventCallback | None = None
        self._on_peer_discovered: EventCallback | None = None
        self._on_link_up: EventCallback | None = None
        self._on_file_offer: EventCallback | None = None
        self._on_transfer_progress: EventCallback | None = None
        self._on_transfer_complete: EventCallback | None = None
        self._on_event: EventCallback | None = None

        self._init_links()
        self._init_connect()
        self._init_announce()
        self._init_ping()
        self._init_queue()
        self._init_transfer()
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
        on_event: EventCallback | None = None,
    ) -> None:
        self._on_message = on_message
        self._on_peer_discovered = on_peer_discovered
        self._on_link_up = on_link_up
        self._on_file_offer = on_file_offer
        self._on_transfer_progress = on_transfer_progress
        self._on_transfer_complete = on_transfer_complete
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
            self.config.tcp_port = self.tcp_transport.port
        if self.serial_transport:
            try:
                await self.serial_transport.start()
            except Exception as exc:
                log.warning("Serial transport unavailable: %s", exc)
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
        tasks = list(self._transfer_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.tcp_transport:
            await self.tcp_transport.stop()
        if self.serial_transport:
            await self.serial_transport.stop()

    def _wire_transport(self, transport: Transport) -> None:
        transport.on_frame(self._on_transport_frame)
        transport.on_event(self._on_transport_event)

    def _identity_for_transport(self, transport: str) -> Identity:
        identity = self.identities.get(transport) or self.identities.get("tcp")
        if not identity:
            raise RuntimeError("no identity for transport")
        return identity

    async def _on_transport_event(self, event: TransportEvent) -> None:
        if event.kind == "discovered" and event.data and event.peer:
            await self._handle_discovered(event.peer.address, "tcp", event.data)
        elif event.kind == "disconnected" and event.peer:
            link = self.get_link_by_peer_id(event.peer.peer_id)
            if link:
                self.remove_link(link.hash_id)
                if self.config.relay_mode:
                    self.routing.remove_for_peer(event.peer.peer_id)
        if self._on_event:
            await self._on_event(
                {
                    "kind": event.kind,
                    "peer": event.peer.address if event.peer else None,
                    "error": event.error,
                }
            )

    async def _on_transport_frame(self, peer: TransportPeer, payload: bytes) -> None:
        try:
            msg_type, flags, _stream_id, _seq, body = parse_header(payload)
        except Exception:
            log.debug("Invalid packet from %s", peer.peer_id[:8])
            return

        if msg_type == MessageType.HANDSHAKE:
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
        elif msg_type == MessageType.FILE_CHUNK:
            link = self.get_link_by_peer_id(peer.peer_id)
            if link:
                await self._handle_file_chunk(link.hash_id, body)
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
        return self.trusted.is_trusted(hash_id)

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

    async def _encrypt_for_link(self, link, msg_type: MessageType, body: bytes) -> bytes:
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

    def _prune_messages(self) -> None:
        self._messages = prune_messages_by_retention(
            self._messages, self.config.message_retention_hours
        )

    def get_messages(self, *, limit: int = 200) -> list[dict[str, Any]]:
        self._prune_messages()
        return [m.to_dict() for m in self._messages[-limit:]]

    def get_discovered_peers(self) -> list[dict[str, Any]]:
        return [p.to_dict() for p in self.discovery.list_peers()]

    def get_trusted_peers(self) -> list[dict[str, Any]]:
        return [p.to_dict() for p in self.trusted.list_peers()]

    def clear_messages(self) -> int:
        count = len(self._messages)
        self._messages.clear()
        return count

    async def send_file(
        self, recipient_hash: str, path: Path, *, transport: str = "tcp"
    ) -> dict[str, Any] | None:
        if not self.trusted.is_trusted(recipient_hash):
            log.warning("Recipient %s not trusted for file send", recipient_hash[:8])
            return None
        transfer = await self.offer_file(recipient_hash, path, transport=transport)
        return transfer.to_dict() if transfer else None