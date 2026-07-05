"""Hub server and client — outbound connections, presence, opaque E2EE forwarding."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from srltcp.core.messaging.presence import HubPresenceRegistry
from srltcp.core.protocol.crypto import (
    identity_hash,
    load_public_key,
    relay_unwrap,
    relay_wrap,
    sign_bytes,
    verify_bytes,
)
from srltcp.core.protocol.messages import (
    MessageType,
    build_header,
    decode_payload,
    encode_payload,
)
from srltcp.transports.base import TransportPeer
from srltcp.utils.logging import get_logger
from srltcp.utils.wan import resolve_wan_endpoint, validate_wan_port

if TYPE_CHECKING:
    from srltcp.core.messaging.backend import MessagingBackend

log = get_logger(__name__)

HUB_SYNTHETIC_PREFIX = "hub-"


def hub_synthetic_peer_id(hash_id: str) -> str:
    return f"{HUB_SYNTHETIC_PREFIX}{hash_id}"


def hub_hash_from_synthetic(peer_id: str) -> str | None:
    if not peer_id.startswith(HUB_SYNTHETIC_PREFIX):
        return None
    raw = peer_id[len(HUB_SYNTHETIC_PREFIX) :]
    if len(raw) == 32 and all(c in "0123456789abcdef" for c in raw):
        return raw
    return None


class HubMixin:
    """Hub server presence + hub client outbound connection and forwarding."""

    _hub_presence: HubPresenceRegistry
    _hub_peer_id: str | None
    _hub_connect_task: asyncio.Task[None] | None
    _hub_registered: bool

    def _init_hub(self: MessagingBackend) -> None:
        self._hub_presence = HubPresenceRegistry()
        self._hub_peer_id = None
        self._hub_connect_task = None
        self._hub_registered = False

    def _hub_enabled(self: MessagingBackend) -> bool:
        return bool(
            not self.config.hub_mode
            and self.config.hub_enabled
            and self.config.hub_host.strip()
        )

    def hub_status(self: MessagingBackend) -> dict[str, object]:
        connected = bool(
            self._hub_peer_id
            and self.tcp_transport
            and self.tcp_transport.has_peer(self._hub_peer_id)
        )
        return {
            "enabled": self._hub_enabled(),
            "host": self.config.hub_host,
            "port": self.config.hub_port,
            "connected": connected,
            "registered": self._hub_registered,
        }

    async def start_hub_client(self: MessagingBackend) -> None:
        if not self._hub_enabled() or self.config.hub_mode:
            return
        await self._ensure_hub_connection()

    async def stop_hub_client(self: MessagingBackend) -> None:
        if self._hub_connect_task and not self._hub_connect_task.done():
            self._hub_connect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._hub_connect_task
        self._hub_connect_task = None
        if self._hub_peer_id and self.tcp_transport:
            await self.tcp_transport.disconnect(self._hub_peer_id)
        self._hub_peer_id = None
        self._hub_registered = False

    async def apply_hub_settings(self: MessagingBackend) -> None:
        await self.stop_hub_client()
        if self._hub_enabled():
            await self.start_hub_client()

    async def _ensure_hub_connection(self: MessagingBackend) -> str | None:
        if self.config.hub_mode or not self._hub_enabled():
            return None
        if (
            self._hub_peer_id
            and self.tcp_transport
            and self.tcp_transport.has_peer(self._hub_peer_id)
        ):
            return self._hub_peer_id
        if not self.tcp_transport:
            return None
        host = self.config.hub_host.strip()
        port = validate_wan_port(self.config.hub_port)
        try:
            endpoint = resolve_wan_endpoint(host, port)
            peer_id = await self.tcp_transport.connect(endpoint.host, endpoint.port)
            self._hub_peer_id = peer_id
            self._hub_registered = False
            log.info("Connected to hub at %s:%d", endpoint.host, endpoint.port)
            return peer_id
        except (OSError, TimeoutError, ConnectionError, ValueError) as exc:
            log.warning("Hub connection to %s:%d failed: %s", host, port, exc)
            self._schedule_hub_reconnect()
            return None

    def _schedule_hub_reconnect(self: MessagingBackend) -> None:
        if self.config.hub_mode or not self._hub_enabled() or not self._running:
            return
        if self._hub_connect_task and not self._hub_connect_task.done():
            return

        async def _reconnect() -> None:
            await asyncio.sleep(5.0)
            if not self._running or not self._hub_enabled():
                return
            await self._ensure_hub_connection()
            if self._hub_peer_id:
                with contextlib.suppress(Exception):
                    await self._send_hub_register()

        self._hub_connect_task = asyncio.create_task(_reconnect())

    def _build_hub_register_body(self: MessagingBackend) -> bytes:
        identity = self.identities.get("tcp")
        if not identity:
            raise RuntimeError("no TCP identity for hub register")
        payload = {
            "type": "hub_register",
            "hash_id": identity.hash_id,
            "name": identity.name,
            "public_key": identity.public_bytes().hex(),
            "transport": "hub",
            "via_hub": True,
        }
        body = encode_payload(payload)
        signature = sign_bytes(identity.private_key, body).hex()
        signed = encode_payload({**payload, "hub_signature": signature})
        return signed

    @staticmethod
    def _verify_hub_register(body: bytes) -> dict[str, object] | None:
        try:
            data = decode_payload(body)
        except Exception:
            return None
        if data.get("type") != "hub_register":
            return None
        hash_id = str(data.get("hash_id", ""))
        pub_hex = str(data.get("public_key", ""))
        sig_hex = str(data.get("hub_signature", ""))
        if len(hash_id) != 32 or not pub_hex or not sig_hex:
            return None
        try:
            pub = bytes.fromhex(pub_hex)
            if identity_hash(pub) != hash_id:
                return None
            unsigned = {k: v for k, v in data.items() if k != "hub_signature"}
            payload = encode_payload(unsigned)
            if not verify_bytes(
                load_public_key(pub), bytes.fromhex(sig_hex), payload
            ):
                return None
        except (ValueError, TypeError):
            return None
        return data

    async def _send_hub_register(self: MessagingBackend) -> None:
        if not self._hub_peer_id or not self.tcp_transport:
            peer_id = await self._ensure_hub_connection()
            if not peer_id:
                raise RuntimeError("hub not connected")
        if not self.tcp_transport or not self._hub_peer_id:
            raise RuntimeError("hub not connected")
        body = self._build_hub_register_body()
        packet = build_header(MessageType.HUB_REGISTER, body=body)
        await self.tcp_transport.send(self._hub_peer_id, packet)
        self._hub_registered = True
        log.info("Registered presence on hub")

    async def _handle_hub_register_server(
        self: MessagingBackend, conn_peer_id: str, body: bytes
    ) -> None:
        data = self._verify_hub_register(body)
        if not data:
            log.warning("Rejected invalid hub register from %s", conn_peer_id[:8])
            if self.tcp_transport:
                await self.tcp_transport.disconnect(conn_peer_id)
            return
        hash_id = str(data["hash_id"])
        previous = self._hub_presence.register(hash_id, conn_peer_id, body)
        if previous and previous.hash_id != hash_id:
            log.debug("Replaced hub registration for conn %s", conn_peer_id[:8])

        presence_body = encode_payload(
            {
                "type": "hub_presence",
                "hash_id": data.get("hash_id"),
                "name": data.get("name", "unknown"),
                "public_key": data.get("public_key", ""),
                "transport": "hub",
                "via_hub": True,
            }
        )
        presence_pkt = build_header(MessageType.HUB_PRESENCE, body=presence_body)
        if self.tcp_transport:
            for other_conn in self._hub_presence.list_other_conns(conn_peer_id):
                with contextlib.suppress(Exception):
                    await self.tcp_transport.send(other_conn, presence_pkt)
            for member in self._hub_presence.list_members():
                if member.hash_id == hash_id:
                    continue
                try:
                    reg = decode_payload(member.announce)
                except Exception:
                    continue
                existing_presence = encode_payload(
                    {
                        "type": "hub_presence",
                        "hash_id": reg.get("hash_id"),
                        "name": reg.get("name", "unknown"),
                        "public_key": reg.get("public_key", ""),
                        "transport": "hub",
                        "via_hub": True,
                    }
                )
                await self.tcp_transport.send(
                    conn_peer_id,
                    build_header(MessageType.HUB_PRESENCE, body=existing_presence),
                )

        log.info("Hub member online: %s (%s)", data.get("name"), hash_id[:8])

    async def _handle_hub_presence_client(
        self: MessagingBackend, body: bytes
    ) -> None:
        await self._handle_discovered("hub", "hub", body)

    async def _handle_hub_depart_server(
        self: MessagingBackend, conn_peer_id: str
    ) -> None:
        member = self._hub_presence.unregister_conn(conn_peer_id)
        if not member:
            return
        depart_body = encode_payload(
            {
                "type": "hub_depart",
                "hash_id": member.hash_id,
            }
        )
        depart_pkt = build_header(MessageType.HUB_DEPART, body=depart_body)
        if self.tcp_transport:
            for other_conn in self._hub_presence.list_other_conns(conn_peer_id):
                with contextlib.suppress(Exception):
                    await self.tcp_transport.send(other_conn, depart_pkt)
        self.discovery.remove(member.hash_id)
        log.info("Hub member offline: %s", member.hash_id[:8])

    async def _handle_hub_depart_client(
        self: MessagingBackend, body: bytes
    ) -> None:
        try:
            data = decode_payload(body)
        except Exception:
            return
        if data.get("type") != "hub_depart":
            return
        hash_id = str(data.get("hash_id", ""))
        if hash_id:
            self.discovery.remove(hash_id)

    async def _handle_hub_relay_server(
        self: MessagingBackend, from_conn: str, body: bytes
    ) -> None:
        try:
            dest_hash, _src_hash, _inner = relay_unwrap(body)
        except ValueError:
            log.debug("Invalid relay envelope from %s", from_conn[:8])
            return
        if len(dest_hash) != 32:
            log.debug("Invalid dest hash in relay envelope")
            return
        target = self._hub_presence.get_conn(dest_hash)
        if not target or target == from_conn:
            log.debug("No hub route to %s", dest_hash[:8])
            return
        if self.tcp_transport:
            packet = build_header(MessageType.RELAY_ENVELOPE, flags=0x08, body=body)
            await self.tcp_transport.send(target, packet)

    async def _handle_hub_relay_client(
        self: MessagingBackend, body: bytes
    ) -> None:
        identity = self.identities.get("tcp")
        if not identity:
            return
        try:
            dest_hash, src_hash, inner = relay_unwrap(body)
        except ValueError:
            return
        if dest_hash != identity.hash_id:
            return
        if not src_hash:
            try:
                from srltcp.core.protocol.messages import parse_header

                msg_type, _flags, _sid, _seq, msg_body = parse_header(inner)
                if msg_type in (MessageType.HANDSHAKE, MessageType.HANDSHAKE_ACK):
                    payload = decode_payload(msg_body)
                    src_hash = str(payload.get("hash_id", ""))
            except Exception:
                return
        if not src_hash or len(src_hash) != 32:
            log.debug("Hub relay: missing sender hash")
            return
        synthetic = hub_synthetic_peer_id(src_hash)
        peer = TransportPeer(
            peer_id=synthetic,
            address="hub",
            transport="hub",
        )
        await self._on_transport_frame(peer, inner)

    async def _send_via_hub(
        self: MessagingBackend, dest_hash: str, packet: bytes
    ) -> None:
        if not self._hub_peer_id or not self.tcp_transport:
            raise KeyError("hub not connected")
        identity = self.identities.get("tcp")
        src = identity.hash_id if identity else None
        wrapped = relay_wrap(packet, dest_hash, src_hash=src)
        envelope = build_header(MessageType.RELAY_ENVELOPE, flags=0x08, body=wrapped)
        await self.tcp_transport.send(self._hub_peer_id, envelope)

    def _on_hub_transport_event(self: MessagingBackend, event: object) -> None:
        if self.config.hub_mode or not self._hub_enabled():
            return
        from srltcp.transports.base import TransportEvent

        if not isinstance(event, TransportEvent):
            return
        if (
            event.kind == "disconnected"
            and event.peer
            and event.peer.peer_id == self._hub_peer_id
        ):
            self._hub_peer_id = None
            self._hub_registered = False
            for hash_id in list(self._links.keys()):
                link = self._links.get(hash_id)
                if link and link.transport == "hub":
                    self.remove_link(hash_id)
            self._schedule_hub_reconnect()