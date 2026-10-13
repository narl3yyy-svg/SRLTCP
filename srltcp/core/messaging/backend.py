"""MessagingBackend — orchestrator composing all mixins."""

from __future__ import annotations

import asyncio
import contextlib
import struct
import time
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
from srltcp.core.messaging.groups import GroupMixin
from srltcp.core.messaging.links import PeerLink, PeerLinkMixin
from srltcp.core.messaging.models import ChatMessage
from srltcp.core.messaging.pairing import PairingMixin
from srltcp.core.messaging.ping import PingMixin
from srltcp.core.messaging.punch import PunchMixin
from srltcp.core.messaging.relay import RelayMixin, relay_peer_hash
from srltcp.core.messaging.share_peer import SharePeerMixin
from srltcp.core.messaging.store import MessageStore
from srltcp.core.messaging.transfer import TransferMixin
from srltcp.core.protocol.messages import (
    GROUP_MESSAGE_TYPES,
    SESSION_MESSAGE_TYPES,
    Flags,
    MessageType,
    build_header,
    decode_payload,
    encode_payload,
    header_aad,
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

MAX_TEXT_BYTES = 65536
HANDSHAKE_DEADLINE = 30.0  # seconds a TCP connection may stay unauthenticated
MAX_PENDING_CHUNKS = 32  # incoming file chunks buffered per peer before we stop reading
MAX_CONTACT_REQUESTS = 50  # pending requests from strangers kept at once


@dataclass
class NodeConfig:
    name: str = "srltcp-node"
    bind_host: str = "0.0.0.0"
    tcp_port: int = 7825
    discovery_port: int = DISCOVERY_PORT
    strict_ports: bool = True
    relay_enabled: bool = True
    relay_code: str = ""
    direct_internet: bool = True  # hole punching; shares public IP with contacts
    punch_port: int = 0  # 0 = listen port + 3
    enable_tcp: bool = True
    enable_serial: bool = False
    serial_port: str = ""
    serial_baud: int = 115200
    announce: bool = False
    lan_ip: str = ""
    incoming_dir: str = ""
    message_retention_hours: int = 168
    handshake_protocol: str = "identity"


class MessagingBackend(
    PeerLinkMixin,
    ConnectMixin,
    AnnounceMixin,
    PingMixin,
    TransferMixin,
    SharePeerMixin,
    RelayMixin,
    PairingMixin,
    GroupMixin,
    PunchMixin,
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
        self._message_store = MessageStore()
        self._persist_handle: asyncio.TimerHandle | None = None
        self._contact_requests: dict[str, float] = {}
        self._declined_requests: set[str] = set()
        self._dial_tasks: dict[str, asyncio.Task[None]] = {}
        self.call_relay: Any = None

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
        self._init_transfer()
        self._init_share_peer()
        self._init_relay()
        self._init_punch()
        self._init_pairing()
        self._init_groups()

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
        self.load_message_history()

        if self.config.enable_tcp:
            self.identities["tcp"] = self.identity_store.load_or_create(self.config.name, "tcp")
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
        await self.start_call_relay()
        if self.tcp_transport:
            await self.start_relay_client()
        self.connect_known_contacts()

        log.info(
            "MessagingBackend started (tcp=%s, serial=%s, relay=%s)",
            bool(self.tcp_transport),
            bool(self.serial_transport),
            bool(self.own_relay_code()),
        )

    async def stop(self) -> None:
        self._running = False
        self.save_message_history()
        if self.call_relay is not None:
            self.call_relay.close()
            self.call_relay = None
        for task in list(self._dial_tasks.values()):
            task.cancel()
        self._dial_tasks.clear()
        await self.stop_relay_client()
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
        self._transfer_tasks.clear()
        for peer_hash in list(self._pending_chunk_tasks.keys()):
            await self._drain_pending_chunk_tasks(peer_hash, timeout=2.0)
        self._pending_chunk_tasks.clear()
        self._transfer_started.clear()
        self._transfer_cooldown_until.clear()
        self._transfer_progress_emit.clear()
        self._pending_handshakes.clear()
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
        if not self._running:
            return
        if not self.trusted.is_trusted(hash_id):
            return
        if self.has_active_transfer_for(hash_id):
            return
        existing = self._reconnect_tasks.get(hash_id)
        if existing and not existing.done():
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
                    if self.in_transfer_cooldown(hash_id):
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
        elif event.kind == "connected" and event.peer and event.peer.transport == "tcp":
            self._expire_if_unauthenticated(event.peer.peer_id)
        elif event.kind == "disconnected" and event.peer:
            stale = self.get_link_by_peer_id(event.peer.peer_id)
            link_name = stale.peer_name if stale else ""
            link_hash = self.remove_link_for_peer(event.peer.peer_id)
            if link_hash:
                if stale and not link_name:
                    link_name = stale.peer_name
                self._pending_handshakes.pop(link_hash, None)
                if self.has_active_transfer_for(link_hash):
                    await self._recover_incoming_transfers_for_peer(link_hash)
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

    def _expire_if_unauthenticated(self, peer_id: str) -> None:
        """Close TCP connections that never complete a handshake, so idle or
        junk connections can't pile up."""

        async def _check() -> None:
            await asyncio.sleep(HANDSHAKE_DEADLINE)
            link = self.get_link_by_peer_id(peer_id)
            if link and link.handshake_complete:
                return
            if self.upgrade_for_peer_id(peer_id):
                return
            if self.tcp_transport:
                log.info("Closing connection %s: no handshake in time", peer_id[:8])
                await self.tcp_transport.disconnect(peer_id)

        if self._running:
            task = asyncio.create_task(_check())
            self.__dict__.setdefault("_expiry_tasks", set()).add(task)
            task.add_done_callback(self.__dict__["_expiry_tasks"].discard)

    async def _on_transport_frame(self, peer: TransportPeer, payload: bytes) -> None:
        try:
            msg_type, flags, _stream_id, _seq, body = parse_header(payload)
        except Exception:
            log.debug("Invalid packet from %s", peer.peer_id[:8])
            return

        try:
            if msg_type in SESSION_MESSAGE_TYPES:
                await self._dispatch_session_message(peer, msg_type, flags, body)
            elif msg_type == MessageType.ANNOUNCE:
                if peer.transport == "relay":
                    return  # addresses only come from the local network
                if peer.transport == "serial":
                    log.info("Serial ANNOUNCE received on %s", peer.address)
                await self._handle_discovered(peer.address, peer.transport, body)
            elif msg_type == MessageType.HANDSHAKE:
                await self._handle_handshake(peer.peer_id, body)
            elif msg_type == MessageType.HANDSHAKE_ACK:
                await self._handle_handshake_ack(peer.peer_id, body)
            elif msg_type == MessageType.HANDSHAKE_FINISH:
                await self._handle_handshake_finish(peer.peer_id, body)
            elif msg_type == MessageType.PING:
                await self._reply_pong_with_rtt(peer, body)
            elif msg_type == MessageType.PONG:
                await self._handle_pong(peer.peer_id, body)
        except Exception as exc:
            log.warning(
                "Handler failed for msg_type=%s from %s: %s",
                msg_type.name if hasattr(msg_type, "name") else msg_type,
                peer.peer_id[:8],
                exc,
            )

    async def _dispatch_session_message(
        self, peer: TransportPeer, msg_type: MessageType, flags: int, body: bytes
    ) -> None:
        """Decrypt and route a message that must travel inside an E2EE session."""
        link = self.get_link_by_peer_id(peer.peer_id)
        if not link or not link.handshake_complete:
            log.debug("Dropping %s from %s — no session", msg_type.name, peer.peer_id[:8])
            return
        if not flags & Flags.ENCRYPTED:
            log.warning("Dropping unencrypted %s from %s", msg_type.name, link.hash_id[:8])
            return
        try:
            body = link.crypto.decrypt(body, header_aad(msg_type, flags))
        except Exception as exc:
            log.warning("Decrypt failed for %s from %s: %s", msg_type.name, link.hash_id[:8], exc)
            return
        hash_id = link.hash_id
        link.last_seen = time.monotonic()
        if msg_type == MessageType.RECEIPT:
            await self._handle_receipt(link, body)
            return
        if msg_type == MessageType.PAIR:
            await self._handle_pair(link, body)
            return
        if msg_type in GROUP_MESSAGE_TYPES:
            await self._handle_group_packet(link, msg_type, body)
            return
        if not self.is_trusted(hash_id):
            await self._handle_untrusted_message(link, msg_type, body)
            return
        if msg_type == MessageType.TEXT:
            await self._handle_text(hash_id, body, flags)
        elif msg_type == MessageType.FILE_OFFER:
            await self._handle_file_offer(hash_id, body)
        elif msg_type == MessageType.FILE_ACCEPT:
            await self._handle_file_accept(hash_id, body)
        elif msg_type == MessageType.FILE_REJECT:
            await self._handle_file_reject(hash_id, body)
        elif msg_type == MessageType.FILE_CHUNK:
            compressed = bool(flags & Flags.COMPRESSED)

            async def _process_chunk() -> None:
                try:
                    await self._handle_file_chunk(hash_id, body, compressed=compressed)
                except Exception as exc:
                    log.warning("File chunk handler failed from %s: %s", hash_id[:8], exc)

            # Back-pressure: if the disk can't keep up, stop reading from this
            # peer until some chunks are written (TCP then slows the sender).
            pending = self._pending_chunk_tasks.get(hash_id)
            if pending and len(pending) >= MAX_PENDING_CHUNKS:
                await asyncio.wait(set(pending), return_when=asyncio.FIRST_COMPLETED)
            task = asyncio.create_task(_process_chunk())
            self._track_chunk_task(hash_id, task)
        elif msg_type == MessageType.FILE_COMPLETE:
            await self._handle_file_complete(hash_id, body)
        elif msg_type == MessageType.FILE_RESUME:
            await self._handle_file_resume(hash_id, body)
        elif msg_type == MessageType.SHARE_LIST:
            await self._handle_share_list(hash_id, body)
        elif msg_type == MessageType.SHARE_REQUEST:
            await self._handle_share_request(hash_id, body)
        elif msg_type == MessageType.CALL_SIGNAL:
            await self._handle_call_signal(hash_id, body)
        elif msg_type == MessageType.CALL_MEDIA:
            self._handle_call_media(hash_id, body)
        elif msg_type == MessageType.RENDEZVOUS:
            await self._handle_rendezvous(hash_id, body)
        elif msg_type == MessageType.PUNCH:
            await self._handle_punch(hash_id, body)

    async def _handle_untrusted_message(
        self, link: PeerLink, msg_type: MessageType, body: bytes
    ) -> None:
        """A verified but not-yet-trusted peer tried to talk to us.

        Nothing is stored or shown as a message; the user gets a contact
        request they can accept, and the sender learns it was not delivered.
        """
        if msg_type == MessageType.TEXT:
            with contextlib.suppress(Exception):
                data = decode_payload(body)
                msg_id = str(data.get("id", ""))
                if msg_id:
                    await self._send_receipt(link, msg_id, "not_trusted")
        await self._raise_contact_request(link)

    async def _raise_contact_request(self, link: PeerLink) -> None:
        """Ask the user whether to add a verified peer who has added us."""
        hash_id = link.hash_id
        now = time.monotonic()
        if now - self._contact_requests.get(hash_id, 0.0) < 30.0:
            return
        self._contact_requests[hash_id] = now
        if self.trusted.get(hash_id) or hash_id in self._declined_requests:
            return  # already a contact, blocked, or declined this session
        if (
            sum(1 for p in self.discovery.list_peers() if p.metadata.get("contact_request"))
            >= MAX_CONTACT_REQUESTS
        ):
            log.info("Too many pending contact requests; ignoring %s", hash_id[:8])
            return
        self.discovery.upsert_contact_request(
            hash_id=hash_id,
            name=link.peer_name,
            public_key=link.public_key.hex(),
            transport=link.transport,
            address=link.address,
        )
        log.info("Contact request from untrusted peer %s (%s)", link.peer_name, hash_id[:8])
        if self._on_event:
            await self._on_event(
                {
                    "kind": "contact_request",
                    "hash_id": hash_id,
                    "name": link.peer_name,
                    "transport": link.transport,
                }
            )

    def is_trusted(self, hash_id: str) -> bool:
        peer = self.trusted.get(hash_id)
        return bool(peer and not peer.blocked)

    def delete_message(self, message_id: str) -> bool:
        before = len(self._messages)
        self._messages = [m for m in self._messages if m.id != message_id]
        removed = len(self._messages) < before
        if removed:
            self._persist_messages()
        return removed

    def clear_messages_for_peer(self, hash_id: str) -> int:
        before = len(self._messages)
        self._messages = [
            m
            for m in self._messages
            if m.sender_hash != hash_id and m.recipient_hash != hash_id
        ]
        self._persist_messages()
        return before - len(self._messages)

    async def _handle_text(self, hash_id: str, body: bytes, flags: int) -> None:
        link = self.get_link(hash_id)
        if not link:
            return
        if flags & Flags.COMPRESSED:
            try:
                body = zstd.ZstdDecompressor().decompress(body, max_output_size=MAX_TEXT_BYTES)
            except Exception:
                return
        data = decode_payload(body)
        msg_id = str(data.get("id") or "")[:64]
        if msg_id:
            # Always acknowledge (even duplicates) so the sender stops retrying.
            await self._send_receipt(link, msg_id, "delivered")
            if any(m.id == msg_id and m.sender_hash == hash_id for m in self._messages):
                return
        text = str(data.get("text", ""))[:MAX_TEXT_BYTES]
        msg = ChatMessage.create(
            sender_hash=hash_id,
            recipient_hash=self._identity_for_transport(link.transport).hash_id,
            text=text,
            transport=link.transport,
        )
        if msg_id:
            msg.id = msg_id
        msg.status = "delivered"
        sent_ts = data.get("ts")
        if isinstance(sent_ts, int | float) and abs(time.time() - float(sent_ts)) < 7 * 86400:
            msg.metadata["sent_at"] = float(sent_ts)
        self._messages.append(msg)
        self._persist_messages()
        if self._on_message:
            await self._on_message(msg.to_dict())

    async def _send_receipt(self, link: PeerLink, msg_id: str, status: str) -> None:
        body = encode_payload({"id": msg_id, "status": status})
        with contextlib.suppress(Exception):
            packet = self._seal(link, MessageType.RECEIPT, body)
            await self._send_raw(link.transport_peer_id, link.transport, packet)

    async def notify_contact_accepted(self, hash_id: str) -> None:
        """Tell a peer we added them: they flush refused messages, or get asked to add us back.

        Sent again on every link-up with a trusted contact, so it also reaches
        peers that were offline when they were added.
        """
        self._declined_requests.discard(hash_id)
        self._contact_requests.pop(hash_id, None)
        link = self.get_link(hash_id)
        if link and link.handshake_complete and self.is_trusted(hash_id):
            await self._send_receipt(link, "", "accepted")
            await self.send_rendezvous(hash_id)

    def decline_contact_request(self, hash_id: str) -> None:
        """Dismiss a contact request until the app restarts."""
        self._declined_requests.add(hash_id)
        self.discovery.remove(hash_id)

    async def _handle_receipt(self, link: PeerLink, body: bytes) -> None:
        hash_id = link.hash_id
        data = decode_payload(body)
        msg_id = str(data.get("id", ""))
        status = str(data.get("status", ""))
        if status == "accepted":
            if self.is_trusted(hash_id):
                # They accepted our contact request: resend what they refused.
                await self.flush_outbox(hash_id)
            else:
                # They added us — offer to add them back so pairing works both ways.
                await self._raise_contact_request(link)
            return
        if status not in ("delivered", "not_trusted"):
            return
        if status == "delivered" and await self._group_receipt(hash_id, msg_id):
            return
        for msg in reversed(self._messages):
            if msg.id == msg_id and msg.recipient_hash == hash_id:
                if msg.status == status:
                    return
                msg.status = status
                self._persist_messages()
                if self._on_message:
                    await self._on_message(msg.to_dict())
                return

    async def send_message(
        self,
        recipient_hash: str,
        text: str,
        *,
        transport: str = "tcp",
    ) -> ChatMessage | None:
        """Queue a message and try to deliver it now.

        Returns immediately with status ``sent`` (written to an open session)
        or ``pending`` (will be retried when the contact comes online).
        A ``RECEIPT`` from the peer later upgrades the status to ``delivered``.
        """
        if not self.trusted.is_trusted(recipient_hash):
            log.warning("Recipient %s not in trusted list", recipient_hash[:8])
            return None
        link = self.get_link(recipient_hash)
        identity = self._identity_for_transport(link.transport if link else transport)
        msg = ChatMessage.create(
            sender_hash=identity.hash_id,
            recipient_hash=recipient_hash,
            text=text[:MAX_TEXT_BYTES],
            transport=link.transport if link else transport,
        )
        msg.status = "pending"
        self._messages.append(msg)
        self._persist_messages()
        if link and link.handshake_complete and self._is_link_reachable(link):
            await self._transmit_text(msg)
        else:
            self._kick_connect(recipient_hash, transport)
        if self._on_message:
            await self._on_message(msg.to_dict())
        return msg

    async def _transmit_text(self, msg: ChatMessage) -> bool:
        link = self.get_link(msg.recipient_hash)
        if not link or not link.handshake_complete:
            return False
        body = encode_payload({"id": msg.id, "text": msg.text, "ts": msg.timestamp})
        flags = 0
        if len(body) >= COMPRESS_THRESHOLD:
            compressed = zstd.ZstdCompressor(level=3).compress(body)
            if len(compressed) < len(body):
                body = compressed
                flags |= Flags.COMPRESSED
        try:
            packet = self._seal(link, MessageType.TEXT, body, flags)
            await self._send_raw(link.transport_peer_id, link.transport, packet)
        except (KeyError, OSError, RuntimeError, ConnectionError) as exc:
            log.info("Send to %s failed, will retry: %s", msg.recipient_hash[:8], exc)
            return False
        msg.status = "sent"
        msg.transport = link.transport
        msg.metadata["attempts"] = int(msg.metadata.get("attempts", 0)) + 1
        self._persist_messages()
        return True

    def _kick_connect(self, hash_id: str, transport: str = "tcp") -> None:
        """Start a background dial so queued messages flush on link-up."""
        if not self._running:
            return
        existing = self._dial_tasks.get(hash_id)
        if existing and not existing.done():
            return

        async def _dial() -> None:
            try:
                await self.connect_to_peer(hash_id, transport=transport, force=False)
            except (OSError, TimeoutError, ConnectionError, KeyError, RuntimeError) as exc:
                log.debug("Background dial to %s failed: %s", hash_id[:8], exc)
            finally:
                self._dial_tasks.pop(hash_id, None)

        self._dial_tasks[hash_id] = asyncio.create_task(_dial())

    def peers_with_undelivered(self) -> set[str]:
        return {
            m.recipient_hash
            for m in self._messages
            if m.msg_type == "text" and m.status in ("pending", "sent")
            and m.recipient_hash not in self._own_hash_ids()
        }

    def retry_outbox_connections(self) -> None:
        """Dial contacts we still owe messages to (no-op when already linked)."""
        for hash_id in self.peers_with_undelivered() | self.peers_owed_group_messages():
            if not self.is_trusted(hash_id):
                continue
            link = self.get_link(hash_id)
            if link and link.handshake_complete:
                continue
            self._kick_connect(hash_id)

    def connect_known_contacts(self) -> None:
        """Bring up sessions with trusted contacts (at startup and periodically)
        so they show as online and their queued messages reach us."""
        for peer in self.trusted.list_peers():
            link = self.get_link(peer.hash_id)
            if link and link.handshake_complete:
                continue
            if peer.tcp_host or peer.wan_host or self.relay_available_for(peer.hash_id):
                self._kick_connect(peer.hash_id)

    async def reap_dead_links(self, max_silence: float) -> list[str]:
        """Drop sessions whose peer stopped answering pings (network switch,
        phone asleep, VPN reconnect) so we reconnect instead of writing into
        a dead socket for minutes."""
        now = time.monotonic()
        dead: list[str] = []
        for hash_id, link in list(self._links.items()):
            if not link.handshake_complete or self.has_active_transfer_for(hash_id):
                continue
            if now - link.last_seen > max_silence:
                dead.append(hash_id)
        for hash_id in dead:
            stale = self.get_link(hash_id)
            name = stale.peer_name if stale else ""
            log.info("Link to %s (%s) went silent — reconnecting", name, hash_id[:8])
            await self._teardown_link(hash_id)
            if self._on_link_down:
                await self._on_link_down(hash_id, name)
            self._schedule_reconnect(hash_id)
        return dead

    async def flush_outbox(self, hash_id: str) -> int:
        """Resend messages still pending/sent-but-unacknowledged to a peer."""
        pending = [
            m
            for m in self._messages
            if m.recipient_hash == hash_id
            and m.msg_type == "text"
            and m.status in ("pending", "sent", "not_trusted")
        ]
        sent = 0
        for msg in pending:
            if await self._transmit_text(msg):
                sent += 1
                if self._on_message:
                    await self._on_message(msg.to_dict())
        return sent

    def _seal(
        self, link: PeerLink, msg_type: MessageType, body: bytes, flags: int = 0
    ) -> bytes:
        flags |= Flags.ENCRYPTED | Flags.E2EE
        encrypted = link.crypto.encrypt(body, header_aad(msg_type, flags))
        return build_header(msg_type, flags=flags, body=encrypted)

    async def _encrypt_for_link(
        self, link: PeerLink, msg_type: MessageType, body: bytes
    ) -> bytes:
        return self._seal(link, msg_type, body)

    # -- Call signalling (WebRTC offer/answer/ICE relayed inside the E2EE session) --

    async def send_call_signal(self, recipient_hash: str, signal: dict[str, Any]) -> bool:
        if not self.is_trusted(recipient_hash):
            return False
        link = self.get_link(recipient_hash)
        if not link or not link.handshake_complete:
            return False
        body = encode_payload(signal)
        if len(body) > 256 * 1024:
            return False
        packet = self._seal(link, MessageType.CALL_SIGNAL, body)
        await self._send_raw(link.transport_peer_id, link.transport, packet)
        return True

    async def start_call_relay(self) -> None:
        from srltcp.core.messaging.turn import LocalTurnServer

        if self.call_relay is not None:
            return
        relay = LocalTurnServer(self._own_call_hash, self._send_call_media)
        try:
            await relay.start("127.0.0.1", 0)
        except OSError as exc:
            log.warning("Call relay unavailable: %s", exc)
            return
        self.call_relay = relay

    def _own_call_hash(self) -> str:
        ident = self.identities.get("tcp") or next(iter(self.identities.values()), None)
        return ident.hash_id if ident else ""

    async def call_ice_config(self, peer_hash: str) -> dict[str, Any] | None:
        """ICE settings for one call: the browser talks only to our local relay."""
        if self.call_relay is None or not self.call_relay.port:
            return None
        from srltcp.utils.network import list_interfaces

        ips = [str(i.get("ip", "")) for i in list_interfaces()[:3]]
        await self.call_relay.ensure_interface_endpoints([ip for ip in ips if ip])
        creds = self.call_relay.issue_credentials(peer_hash)
        return {
            "iceServers": [{"urls": self.call_relay.urls(), **creds}],
            "iceTransportPolicy": "relay",
        }

    async def _send_call_media(
        self, peer_hash: str, src_port: int, dst_port: int, payload: bytes
    ) -> None:
        link = self.get_link(peer_hash)
        if not link or not link.handshake_complete:
            return
        body = struct.pack(">HH", src_port, dst_port) + payload
        try:
            packet = self._seal(link, MessageType.CALL_MEDIA, body)
            await self._send_raw(link.transport_peer_id, link.transport, packet)
        except (KeyError, OSError, ConnectionError, RuntimeError):
            pass  # media is loss-tolerant; the call recovers or ends

    def _handle_call_media(self, hash_id: str, body: bytes) -> None:
        if self.call_relay is None or len(body) < 4:
            return
        src_port, dst_port = struct.unpack(">HH", body[:4])
        self.call_relay.deliver(hash_id, src_port, dst_port, body[4:])

    async def _handle_call_signal(self, hash_id: str, body: bytes) -> None:
        data = decode_payload(body)
        if not isinstance(data, dict) or self._on_event is None:
            return
        await self._on_event({"kind": "call_signal", "hash_id": hash_id, "signal": data})

    async def _send_raw(self, peer_id: str, transport: str, packet: bytes) -> None:
        if transport == "relay":
            dest_hash = relay_peer_hash(peer_id)
            if not dest_hash:
                raise KeyError("bad relay peer")
            await self.send_via_relay(dest_hash, packet)
        elif transport == "tcp" and self.tcp_transport:
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
        status["relay"] = self.relay_status()
        return status

    def _history_enabled(self) -> bool:
        return self.config.message_retention_hours > 0

    def load_message_history(self) -> None:
        if not self._history_enabled():
            self._message_store.clear()
            return
        stored = self._message_store.load()
        stored_ids = {m.id for m in stored}
        for msg in stored:
            # Transfers do not survive a restart; show unfinished ones as failed.
            if msg.msg_type != "text" and msg.metadata.get("state") in (
                "pending",
                "offered",
                "accepted",
                "transferring",
                "paused",
            ):
                msg.metadata["state"] = "failed"
        self._messages = stored + [m for m in self._messages if m.id not in stored_ids]
        self._prune_messages()

    def _persist_messages(self) -> None:
        """Debounced write of chat history to disk."""
        if not self._history_enabled():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.save_message_history()
            return
        if self._persist_handle:
            return
        self._persist_handle = loop.call_later(0.5, self.save_message_history)

    def save_message_history(self) -> None:
        if self._persist_handle:
            self._persist_handle.cancel()
            self._persist_handle = None
        if not self._history_enabled():
            return
        self._prune_messages()
        self._message_store.save(self._messages)

    def _prune_messages(self) -> None:
        self._messages = prune_messages_by_retention(
            self._messages, self.config.message_retention_hours
        )
        # A contact deleted over 24 h ago and not added back: their one-to-one
        # chat goes for good (re-adding them later starts fresh). Group chats
        # they took part in stay.
        gone = set(self.trusted.take_expired_removals())
        if gone:
            self._messages = [
                m for m in self._messages
                if m.metadata.get("group_id")
                or not ({m.sender_hash, m.recipient_hash} & gone)
            ]
            if self._history_enabled():
                self._message_store.save(self._messages)

    def get_messages(
        self, *, limit: int = 200, peer: str = "", group: str = ""
    ) -> list[dict[str, Any]]:
        """Newest ``limit`` messages, optionally only one chat (a contact or a group)."""
        self._prune_messages()
        msgs = self._messages
        if group:
            msgs = [m for m in msgs if m.metadata.get("group_id") == group]
        elif peer:
            msgs = [
                m for m in msgs
                if not m.metadata.get("group_id") and peer in (m.sender_hash, m.recipient_hash)
            ]
        return [m.to_dict() for m in msgs[-limit:]]

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
        # Include blocked contacts so they can still be unblocked from the UI.
        for p in self.trusted.list_peers(include_blocked=True):
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
            d = p.public_dict()
            d["name"] = name
            link = self.get_link(p.hash_id)
            d["path"] = (
                ("relay" if link.transport == "relay" else "direct")
                if link and link.handshake_complete
                else ""
            )
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
        if self._persist_handle:
            self._persist_handle.cancel()
            self._persist_handle = None
        self._message_store.clear()
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
        self._persist_messages()
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
                if notify:
                    self._persist_messages()
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