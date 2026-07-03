"""Announce and discovery mixin."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from srltcp.core.protocol.messages import encode_payload
from srltcp.utils.logging import get_logger

if TYPE_CHECKING:
    from srltcp.core.messaging.backend import MessagingBackend

log = get_logger(__name__)


class AnnounceMixin:
    """Broadcast node presence on enabled transports."""

    _announce_tasks: list[asyncio.Task[None]]

    def _init_announce(self: MessagingBackend) -> None:
        self._announce_tasks = []

    def build_announce_payload(self: MessagingBackend, transport: str) -> bytes:
        identity = self._identity_for_transport(transport)
        return encode_payload(
            {
                "type": "announce",
                "hash_id": identity.hash_id,
                "name": identity.name,
                "public_key": identity.public_bytes().hex(),
                "transport": transport,
                "tcp_host": self._lan_ip(),
                "tcp_port": self.config.tcp_port,
            }
        )

    def _lan_ip(self: MessagingBackend) -> str:
        if self.config.lan_ip:
            return self.config.lan_ip
        import socket

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    async def announce(self: MessagingBackend, transport: str | None = None) -> None:
        transports = []
        if transport:
            transports = [transport]
        else:
            if self.tcp_transport:
                transports.append("tcp")
            if self.serial_transport:
                transports.append("serial")

        for t in transports:
            payload = self.build_announce_payload(t)
            if t == "tcp" and self.tcp_transport:
                self.tcp_transport.set_announce_payload(payload)
                await self.tcp_transport.broadcast_discovery(payload)
            elif t == "serial" and self.serial_transport:
                await self.serial_transport.broadcast(payload)
            log.debug("Announced on %s", t)

    async def start_announce_loop(self: MessagingBackend) -> None:
        from srltcp.core.messaging.constants import ANNOUNCE_INTERVAL

        async def _loop() -> None:
            while self._running:
                await self.announce()
                await asyncio.sleep(ANNOUNCE_INTERVAL)

        self._announce_tasks.append(asyncio.create_task(_loop()))

    async def _handle_discovered(
        self: MessagingBackend, address: str, transport: str, payload: bytes
    ) -> None:
        peer = self.discovery.upsert_from_announce(address, transport, payload)
        if peer and self._on_peer_discovered:
            await self._on_peer_discovered(peer.to_dict())