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
from srltcp.core.messaging.disappear import DisappearMixin, expires_at, wire_ttl
from srltcp.core.messaging.groups import GroupMixin
from srltcp.core.messaging.links import PeerLink, PeerLinkMixin
from srltcp.core.messaging.models import ChatMessage, FileTransfer
from srltcp.core.messaging.pairing import PairingMixin
from srltcp.core.messaging.paths import PathsMixin
from srltcp.core.messaging.ping import PingMixin
from srltcp.core.messaging.punch import PunchMixin
from srltcp.core.messaging.radio import RadioMixin
from srltcp.core.messaging.share_peer import SharePeerMixin
from srltcp.core.messaging.store import MessageStore
from srltcp.core.messaging.sync import SyncMixin
from srltcp.core.messaging.tor import TorMixin
from srltcp.core.messaging.transfer import TERMINAL, TransferMixin
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
from srltcp.utils.platform import resolve_serial_port

log = get_logger(__name__)

# Over Tor only the connection setup goes (handshake, keep-alive, addresses,
# hole punching, pairing, receipts, cancels): Tor is the doorbell. Messages,
# files, calls, groups, shares and sync wait for the direct connection.
DOORBELL_REFUSED = frozenset({
    MessageType.TEXT, MessageType.FILE_OFFER, MessageType.FILE_ACCEPT, MessageType.FILE_CHUNK,
    MessageType.FILE_COMPLETE, MessageType.FILE_RESUME, MessageType.FILE_ACK,
    MessageType.SHARE_LIST, MessageType.SHARE_REQUEST, MessageType.SYNC,
    MessageType.CALL_SIGNAL, MessageType.CALL_MEDIA, MessageType.GROUP_UPDATE,
    MessageType.GROUP_TEXT, MessageType.GROUP_LEAVE,
})

EventCallback = Callable[..., Awaitable[None]]

MAX_TEXT_BYTES = 65536
HANDSHAKE_DEADLINE = 30.0  # seconds a TCP connection may stay unauthenticated
MAX_CONTACT_REQUESTS = 50  # pending requests from strangers kept at once


@dataclass
class NodeConfig:
    name: str = "srltcp-node"
    bind_host: str = "0.0.0.0"
    tcp_port: int = 7825
    discovery_port: int = DISCOVERY_PORT
    strict_ports: bool = True
    direct_internet: bool = True  # hole punching; shares public IP with contacts
    router_port_mapping: bool = True  # UPnP / NAT-PMP on the home router (portmap.py)
    stun_servers: str = ""  # for direct UDP ("host:port, ..."; empty = defaults)
    tor_enabled: bool = False  # reach contacts over Tor (onion service)
    tor_path: str = ""  # tor executable on a PC (empty: find it)
    punch_port: int = 0  # 0 = listen port + 3
    enable_tcp: bool = True
    enable_serial: bool = False
    serial_port: str = ""
    serial_baud: int = 115200
    announce: bool = False
    lan_ip: str = ""
    incoming_dir: str = ""
    message_retention_hours: int = 168
    disappear_seconds: int = 0  # timer on messages we send (0 = keep)
    auto_download_mb: int = 2048  # larger incoming files wait for a tap (MiB)
    auto_download_radio_mb: int = 2  # the same over the serial radio


class MessagingBackend(
    PeerLinkMixin,
    ConnectMixin,
    AnnounceMixin,
    PingMixin,
    TransferMixin,
    SharePeerMixin,
    PathsMixin,
    PairingMixin,
    GroupMixin,
    PunchMixin,
    RadioMixin,
    TorMixin,
    DisappearMixin,
    SyncMixin,
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
        self._init_paths()
        self._init_punch()
        self._init_radio()
        self._init_tor()
        self._init_pairing()
        self._init_groups()
        self._init_sync()

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
            self.identities["tcp"] = self.identity_store.load_or_create(self.config.name, "tcp")
            self.tcp_transport = TCPTransport(
                host=self.config.bind_host,
                port=self.config.tcp_port,
                discovery_port=self.config.discovery_port,
                strict_ports=self.config.strict_ports,
            )
            self._wire_transport(self.tcp_transport)
            self.tcp_transport.on_datagram(self._on_transport_datagram)

        if self.config.enable_serial:
            # (Serial uses the main identity: a contact is the same contact
            # over Wi-Fi, the internet or the radio.)
            port = self.config.serial_port or None
            self.serial_transport = SerialTransport(port=port, baudrate=self.config.serial_baud)
            self._wire_transport(self.serial_transport)
        self.restore_transfers()  # needs the identities: which side of each file we are
        self.load_message_history()
        await self.expire_messages(notify=False)  # timers that ran out while closed

        if self.tcp_transport:
            await self.tcp_transport.start()
            self.config.tcp_port = self.tcp_transport.port
        if self.serial_transport:
            try:
                await self.serial_transport.start()
                self._serial_error = None
                self.radio_eager()
            except Exception as exc:
                self._record_serial_failure(self.serial_transport.port, exc)
                self.serial_transport = None

        if self.config.announce:
            await self.start_announce_loop()
        await self.start_ping_loop()
        self._ping_tasks.append(asyncio.create_task(self.radio_loop()))
        self.start_progress_ticker()
        await self.start_call_relay()
        if self.tcp_transport:
            await self.start_udp()
        await self.start_tor()
        self.connect_known_contacts()
        if self._syncs:
            self._ensure_sync_loop()

        log.info(
            "MessagingBackend started (tcp=%s, serial=%s)",
            bool(self.tcp_transport),
            bool(self.serial_transport),
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
        await self.stop_sync_loop()
        await self.stop_tor()
        await self.stop_announce_loop()
        await self.stop_ping_loop()
        for task in list(self._reconnect_tasks.values()):
            task.cancel()
        if self._reconnect_tasks:
            await asyncio.gather(*self._reconnect_tasks.values(), return_exceptions=True)
        self._reconnect_tasks.clear()
        await self.stop_transfers()
        self._pending_handshakes.clear()
        await self.port_mapper.close()  # (the router forwards nothing for us once we're gone)
        self.stop_udp()
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
        from srltcp.transports.android_serial import PREFIX
        from srltcp.utils.serial_access import format_serial_permission_help

        error = (
            str(exc) if port.startswith(PREFIX) or not port
            else format_serial_permission_help(port, exc)
        ) or "No serial device selected"
        if error != self._serial_error:  # (the watchdog retries: say it once)
            log.warning("%s", error)
        self._serial_error = error

    async def serial_watchdog(self) -> None:
        """Serial on but not open (device plugged in later, USB permission just
        granted, adapter unplugged and back): open it again. With "Automatic"
        (or the saved adapter unplugged), move to a newly plugged-in one."""
        if not self.config.enable_serial:
            return
        transport = self.serial_transport
        if transport is not None and getattr(transport, "alive", True):
            wanted = resolve_serial_port(self.config.serial_port)
            if not wanted or wanted == transport.port:
                return
            log.info("Serial adapter %s plugged in — switching to it", wanted)
        if transport is not None:
            await transport.stop()
            self.serial_transport = None
        await self.apply_serial_transport(ask=False)  # (never nags: asks once per plug-in)
        if self.serial_transport is not None:
            await self.radio_tick(now_open=True)

    async def ensure_serial_transport(self) -> dict[str, str]:
        """Open serial transport if enabled but not yet running."""
        if self.serial_transport:
            return {"serial": "running"}
        if not self.config.enable_serial:
            return {"serial": "disabled"}
        return await self.apply_serial_transport()

    async def apply_serial_transport(self, *, ask: bool = True) -> dict[str, str]:
        """Start, stop, or restart serial transport when settings change.
        ``ask``: on Android, show the USB permission question again."""
        want = self.config.enable_serial
        port = resolve_serial_port(self.config.serial_port)
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
                getattr(self.serial_transport, "port", None) != port
                or getattr(self.serial_transport, "baudrate", None) != baud
            ):
                await self.serial_transport.stop()
                self.serial_transport = None
                needs_restart = True
        else:
            needs_restart = True

        if needs_restart:
            if not port:
                self._record_serial_failure("", FileNotFoundError("No serial adapter connected"))
                return {"serial": "failed", "error": self._serial_error or ""}
            self.serial_transport = SerialTransport(port=port, baudrate=baud, ask=ask)
            self._wire_transport(self.serial_transport)
            try:
                await self.serial_transport.start()
                self._serial_error = None
                self.radio_eager()  # (just opened: let contacts in range know)
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
                    if self.radio_contact_near(hash_id):
                        return  # the radio reconnects it (radio.py)
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
                await self._pause_outgoing_for_peer(link_hash)
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
            elif msg_type == MessageType.RADIO_BEACON:
                if peer.transport == "serial":
                    await self._handle_radio_beacon(body)
            elif msg_type == MessageType.ANNOUNCE:
                # Nobody is discovered over the radio (only contacts' beacons count).
                if peer.transport != "serial":
                    await self._handle_discovered(peer.address, peer.transport, body)
            elif msg_type == MessageType.HANDSHAKE:
                await self._handle_handshake(peer.peer_id, body)
            elif msg_type == MessageType.HANDSHAKE_ACK:
                await self._handle_handshake_ack(peer.peer_id, body)
            elif msg_type in (MessageType.PING, MessageType.PONG):
                if flags & Flags.ENCRYPTED:
                    await self._dispatch_session_message(peer, msg_type, flags, body)
                elif msg_type == MessageType.PING:
                    await self._reply_pong_with_rtt(peer, body)  # (a plaintext pong: ignored)
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
        await self.confirm_link(link)
        if msg_type == MessageType.PING:
            await self._answer_ping(link, body)
            return
        if msg_type == MessageType.PONG:
            await self._handle_pong(link, body)
            return
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
            await self._handle_file_chunk(
                hash_id, body, compressed=bool(flags & Flags.COMPRESSED)
            )
        elif msg_type == MessageType.FILE_ACK:
            await self._handle_file_ack(hash_id, body)
        elif msg_type == MessageType.FILE_COMPLETE:
            await self._handle_file_complete(hash_id, body)
        elif msg_type == MessageType.FILE_RESUME:
            await self._handle_file_resume(hash_id, body)
        elif msg_type == MessageType.SHARE_LIST:
            await self._handle_share_list(hash_id, body)
        elif msg_type == MessageType.SHARE_REQUEST:
            await self._handle_share_request(hash_id, body)
        elif msg_type == MessageType.SYNC:
            await self._handle_sync(hash_id, body)
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
        if not self._on_our_network(link):
            # Nobody outside our own networks can find us or knock: requests
            # only from the same Wi-Fi/LAN or VPN (pairing codes and links,
            # which carry a one-time token, work from anywhere).
            log.info("Contact request from %s ignored: not on our network", hash_id[:8])
            return
        recent = [t for t in self.__dict__.get("_request_times", []) if time.monotonic() - t < 600]
        if len(recent) >= 10 and hash_id not in self._contact_requests:
            # A stream of new identities on the LAN: no more than 10 new
            # requests (and notifications) per 10 minutes.
            self.__dict__["_request_times"] = recent
            return
        recent.append(time.monotonic())
        self.__dict__["_request_times"] = recent
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
        """Delete the chat with a contact (not their messages in groups)."""
        before = len(self._messages)
        self._messages = [m for m in self._messages if not self._in_chat(m, hash_id, "")]
        self._persist_messages()
        return before - len(self._messages)

    @staticmethod
    def _in_chat(msg: ChatMessage, peer: str, group: str) -> bool:
        gid = msg.metadata.get("group_id")
        if group:
            return gid == group
        return not gid and peer in (msg.sender_hash, msg.recipient_hash)

    async def delete_chat(
        self, *, peer: str = "", group: str = "", delete_files: bool = False
    ) -> int:
        """Delete every message of one chat (a contact or a group). Its
        unfinished file transfers stop; ``delete_files`` also removes the
        files received in it from this device."""
        if not peer and not group:
            return 0
        gone = [m for m in self._messages if self._in_chat(m, peer, group)]
        removed = await self._remove_messages(gone, delete_files=delete_files)
        if removed:
            log.info("Chat deleted (%d message(s)%s)", removed,
                     ", files too" if delete_files else "")
        return removed

    async def delete_messages(
        self, ids: list[str], *, peer: str = "", group: str = "", delete_files: bool = True
    ) -> int:
        """Delete these messages of one chat from this device (long-press →
        Delete). A file's message takes the received file with it, like in
        Signal; its transfer stops if it's still moving. (Ids of received
        messages are the sender's: only the chat's own count, so an id copied
        from another chat can't take that one with it.)"""
        wanted = set(ids)
        return await self._remove_messages(
            [m for m in self._messages
             if m.id in wanted and (not (peer or group) or self._in_chat(m, peer, group))],
            delete_files=delete_files,
        )

    async def _remove_messages(self, gone: list[ChatMessage], *, delete_files: bool) -> int:
        if not gone:
            return 0
        ids = {m.id for m in gone}
        self._messages = [m for m in self._messages if m.id not in ids]
        for msg in gone:
            tid = str(msg.metadata.get("transfer_id") or "")
            transfer = self._transfers.get(tid) if tid else None
            if transfer is None:
                continue
            if transfer.state not in TERMINAL:
                await self.cancel_transfer(tid)
            received = self.received_file(transfer)  # (only a file we wrote ourselves)
            if delete_files and received is not None:
                with contextlib.suppress(OSError):
                    received.unlink(missing_ok=True)
            self._incoming_paths.pop(tid, None)
            self._transfers.pop(tid, None)
        self._journal_changed()
        self._persist_messages()
        if self._on_event:
            await self._on_event({"kind": "messages_removed", "ids": sorted(ids)})
        return len(ids)

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
        self.stamp_ttl(msg, wire_ttl(data))  # their timer, counted from now
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

    def _on_our_network(self, link: PeerLink) -> bool:
        if link.transport != "tcp" or not self.tcp_transport:
            return link.transport == "serial"  # a cable is as local as it gets
        if self.tcp_transport.is_tor(link.transport_peer_id) or link.address.startswith("udp:"):
            return False
        from srltcp.utils.network import on_our_network

        host = link.address.rsplit(":", 1)[0] if ":" in link.address else link.address
        return on_our_network(host)

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
        self.stamp_ttl(msg, self.disappear_ttl(), msg.timestamp)
        self._messages.append(msg)
        self._persist_messages()
        if self.data_link(recipient_hash) and link and self._is_link_reachable(link):
            await self._transmit_text(msg)
        else:
            self._kick_connect(recipient_hash, transport)
        if self._on_message:
            await self._on_message(msg.to_dict())
        return msg

    async def _transmit_text(self, msg: ChatMessage) -> bool:
        link = self.data_link(msg.recipient_hash)
        if not link:
            return False
        data: dict[str, Any] = {"id": msg.id, "text": msg.text, "ts": msg.timestamp}
        if msg.metadata.get("ttl"):
            data["ttl"] = int(msg.metadata["ttl"])  # they delete it too
        body = encode_payload(data)
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
        link = self.get_link(hash_id)
        if link and link.handshake_complete and self._link_on_tor(link):
            # Reached over Tor (the doorbell): what's waiting needs the direct path.
            self.request_direct_upgrade(hash_id, punch=True)
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
        owed = self.peers_with_undelivered() | self.peers_owed_group_messages()
        for hash_id in owed | self.peers_with_queued_files():
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
            if (
                peer.tcp_host or peer.alt_hosts or peer.wan_host
                or (self.tor_ready() and peer.onion)
            ):
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
            if link.transport == "serial" and self.radio_alive():
                continue  # a slow radio busy with a long message: pongs wait their turn
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
        if msg_type in DOORBELL_REFUSED and self._link_on_tor(link):
            # Backstop for data_link(): nothing but connection setup over Tor.
            raise ConnectionError("Tor only sets up direct connections")
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
        link = self.data_link(recipient_hash)
        if not link:
            self._kick_connect(recipient_hash)
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
        link = self.data_link(peer_hash)
        return {
            "iceServers": [{"urls": self.call_relay.urls(), **creds}],
            "iceTransportPolicy": "relay",
            # Over a serial radio the page makes a low-bitrate voice call
            # (a radio carries a few KB/s: no video).
            "narrow": bool(link and link.transport == "serial"),
        }

    async def _send_call_media(
        self, peer_hash: str, src_port: int, dst_port: int, payload: bytes
    ) -> None:
        link = self.get_link(peer_hash)
        if not link or not link.handshake_complete:
            return
        body = struct.pack(">HH", src_port, dst_port) + payload
        try:
            if (
                link.transport == "tcp"
                and self.tcp_transport
                and self.tcp_transport.send_datagram(link.transport_peer_id, body)
            ):
                # Direct UDP: as a datagram, never waiting behind lost packets.
                # The UDP layer encrypts and authenticates it with keys only the
                # two of us have, and the media inside is DTLS-SRTP encrypted.
                return
            packet = self._seal(link, MessageType.CALL_MEDIA, body)
            if (
                link.transport == "serial"
                and self.serial_transport
                and self.serial_transport.send_datagram(link.transport_peer_id, packet)
            ):
                # The radio: one unacknowledged packet, ahead of everything else
                # (sealed with the session keys; the radio has no encryption).
                return
            await self._send_raw(link.transport_peer_id, link.transport, packet)
        except (KeyError, OSError, ConnectionError, RuntimeError):
            pass  # media is loss-tolerant; the call recovers or ends

    def _on_transport_datagram(self, peer: TransportPeer, body: bytes) -> None:
        """Call media over direct UDP: accepted only on a connection that
        carries a contact's established session."""
        link = self.get_link_by_peer_id(peer.peer_id)
        if link and link.handshake_complete and self.is_trusted(link.hash_id):
            self._handle_call_media(link.hash_id, body)

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
            transport = self.serial_transport
            status["serial"] = {
                "active": transport is not None and getattr(transport, "alive", True),
                "port": serial_port,
                "baud": self.config.serial_baud,
                "error": self._serial_error,
                # Radio link counters (Settings → Serial shows them live).
                "link": transport.link_stats() if transport is not None else {},
                "peers": [
                    link.peer_name or link.hash_id[:8]
                    for link in self._links.values()
                    if link.transport == "serial" and link.up
                ],
            }
        status["tor"] = self.tor_status()
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
            if msg.msg_type == "text" or msg.metadata.get("state") not in (
                "pending", "offered", "accepted", "transferring", "paused",
            ):
                continue
            # Unfinished file: it resumes if restore_transfers() brought it back.
            transfer = self._transfers.get(str(msg.metadata.get("transfer_id", "")))
            msg.metadata["state"] = transfer.state.value if transfer else "failed"
            msg.metadata.update(speed_bps=0, speed_mbps=0, eta_s=None)
            if transfer:
                msg.metadata["offset"] = transfer.offset
        self._messages = stored + [m for m in self._messages if m.id not in stored_ids]
        self._prune_messages()  # expired ones go in start() (their files too)

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
        now = time.time()
        msgs = [m for m in self._messages if not 0 < expires_at(m) <= now]
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
            d["verified"] = self.contact_verified(p.hash_id)
            link = self.get_link(p.hash_id)
            d["path"] = "direct" if link and link.handshake_complete else ""
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
        meta = transfer.get("metadata") or {}
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
                "speed_bps": transfer.get("speed_bps", 0),
                "speed_mbps": transfer.get("speed_mbps", 0),
                "eta_s": transfer.get("eta_s"),
                "direction": direction,
                "is_folder_zip": meta.get("is_folder_zip"),
                "folder_name": meta.get("folder_name"),
                "ask": bool(meta.get("ask")),  # waits for a tap to download
                "waiting": bool(meta.get("waiting")),  # (sender: they have to tap)
            },
        )
        self.stamp_ttl(msg, wire_ttl(meta))
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
                        "speed_bps": transfer.get("speed_bps", 0),
                        "speed_mbps": transfer.get("speed_mbps", 0),
                        "eta_s": transfer.get("eta_s"),
                        "filename": transfer.get("filename", msg.metadata.get("filename")),
                    }
                )
                if (
                    transfer.get("state") == "complete"
                    and msg.sender_hash in self._own_hash_ids()
                    and msg.status != "delivered"
                ):
                    # The receiver has the whole file and checked it: ✓✓, like a chat message.
                    msg.status = "delivered"
                meta = transfer.get("metadata") or {}
                msg.metadata["ask"] = bool(meta.get("ask"))
                msg.metadata["waiting"] = bool(meta.get("waiting"))
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
        link = self.data_link(recipient_hash)
        effective_transport = link.transport if link else transport
        transfer: FileTransfer | None
        if not link:
            # No session right now (other network, Tor still connecting, phone
            # asleep): queue the file like a message and dial in the
            # background. It is offered as soon as the session is up (the
            # handshake resumes queued transfers), without holding this
            # request open for a slow Tor dial.
            transfer = await self.queue_file(
                recipient_hash, path, transport=effective_transport, filename=filename,
                ttl=self.disappear_ttl(),
            )
            link = self.data_link(recipient_hash)
            if link:
                await self.resume_transfer(transfer.id)  # came up while hashing
            else:
                self._kick_connect(recipient_hash)
            link_transport = effective_transport
        else:
            transfer = await self.offer_file(
                recipient_hash,
                path,
                transport=link.transport,
                filename=filename,
                ttl=self.disappear_ttl(),
            )
            link_transport = link.transport
        if not transfer:
            return None
        data = transfer.to_dict()
        identity = self._identity_for_transport(link_transport)
        await self._append_file_message(
            sender_hash=identity.hash_id,
            recipient_hash=recipient_hash,
            transport=link_transport,
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