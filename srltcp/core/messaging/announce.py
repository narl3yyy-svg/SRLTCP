"""Announce and discovery mixin."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from srltcp.core.protocol.messages import MessageType, build_header, encode_payload
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

    async def _send_announce(self: MessagingBackend, transport_name: str) -> None:
        payload = self.build_announce_payload(transport_name)
        if transport_name == "tcp" and self.tcp_transport:
            self.tcp_transport.set_announce_payload(payload)
            await self.tcp_transport.broadcast_discovery(payload)
        elif transport_name == "serial" and self.serial_transport:
            packet = build_header(MessageType.ANNOUNCE, body=payload)
            peers = self.serial_transport.peers()
            if peers:
                await self.serial_transport.send(peers[0].peer_id, packet)
            else:
                await self.serial_transport.broadcast(packet)
        log.info("Announced on %s", transport_name)

    async def announce(self: MessagingBackend, transport: str | None = None) -> None:
        transports: list[str] = []
        if transport:
            transports = [transport]
        else:
            if self.tcp_transport:
                transports.append("tcp")
            if self.serial_transport:
                transports.append("serial")

        for t in transports:
            for _ in range(3):
                await self._send_announce(t)
                await asyncio.sleep(0.12)

    async def start_announce_loop(self: MessagingBackend) -> None:
        await self.stop_announce_loop()
        from srltcp.core.messaging.constants import ANNOUNCE_INTERVAL

        async def _loop() -> None:
            while self._running and self.config.announce:
                await self.announce()
                await asyncio.sleep(ANNOUNCE_INTERVAL)

        self._announce_tasks.append(asyncio.create_task(_loop()))

    async def stop_announce_loop(self: MessagingBackend) -> None:
        tasks = list(self._announce_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._announce_tasks.clear()

    async def set_auto_announce(self: MessagingBackend, enabled: bool) -> None:
        self.config.announce = enabled
        if enabled:
            await self.start_announce_loop()
        else:
            await self.stop_announce_loop()

    async def _handle_discovered(
        self: MessagingBackend, address: str, transport: str, payload: bytes
    ) -> None:
        own_hashes = {i.hash_id for i in self.identities.values()}
        peer, is_new = self.discovery.upsert_from_announce(address, transport, payload)
        if not peer or peer.hash_id in own_hashes:
            if peer and peer.hash_id in own_hashes:
                self.discovery.remove(peer.hash_id)
            return
        if self._on_peer_discovered:
            await self._on_peer_discovered(peer.to_dict())