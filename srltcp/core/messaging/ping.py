"""Ping / RTT and link quality metrics."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from srltcp.core.messaging.constants import PING_INTERVAL
from srltcp.core.protocol.messages import MessageType, build_header, decode_payload, encode_payload
from srltcp.utils.logging import get_logger

if TYPE_CHECKING:
    from srltcp.core.messaging.backend import MessagingBackend

log = get_logger(__name__)


class PingMixin:
    """Measure RTT and link quality for connected peers."""

    _pending_pings: dict[str, float]
    _ping_tasks: list[asyncio.Task[None]]

    def _init_ping(self: MessagingBackend) -> None:
        self._pending_pings = {}
        self._ping_tasks = []

    def _own_hash_ids(self: MessagingBackend) -> set[str]:
        return {i.hash_id for i in self.identities.values()}

    async def start_ping_loop(self: MessagingBackend) -> None:
        async def _loop() -> None:
            while self._running:
                for link in self.list_links():
                    hash_id = link.get("hash_id", "")
                    if link.get("handshake_complete") and not self.has_active_transfer_for(
                        hash_id
                    ):
                        await self.ping_peer(hash_id)
                await asyncio.sleep(PING_INTERVAL)

        self._ping_tasks.append(asyncio.create_task(_loop()))

    async def stop_ping_loop(self: MessagingBackend) -> None:
        tasks = list(self._ping_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._ping_tasks.clear()

    async def ping_peer(self: MessagingBackend, hash_id: str) -> float | None:
        link = self.get_link(hash_id)
        if not link or not link.handshake_complete:
            return None
        sent_at = time.time()
        self._pending_pings[hash_id] = sent_at
        body = encode_payload({"sent_at": sent_at, "hash_id": hash_id})
        packet = build_header(MessageType.PING, body=body)
        await self._send_raw(link.transport_peer_id, link.transport, packet)
        return sent_at

    async def _handle_pong(self: MessagingBackend, peer_id: str, body: bytes) -> None:
        try:
            data = decode_payload(body)
            sent_at = float(data.get("sent_at", 0))
        except Exception:
            return
        if sent_at <= 0:
            return
        rtt_ms = (time.time() - sent_at) * 1000.0
        link = self.get_link_by_peer_id(peer_id)
        if not link:
            return
        link.rtt_ms = rtt_ms
        self.discovery.update_metrics(link.hash_id, rtt_ms=rtt_ms)
        if link.transport == "serial" and self.serial_transport:
            self.serial_transport.record_ping_success(rtt_ms)
            lq = self.serial_transport.link_quality_pct()
            link.link_quality_pct = lq
            self.discovery.update_metrics(link.hash_id, link_quality_pct=lq)
        if self._on_peer_metrics:
            await self._on_peer_metrics(link.hash_id, self.get_peer_metrics(link.hash_id))

    async def _reply_pong_with_rtt(self: MessagingBackend, peer: object, body: bytes) -> None:
        from srltcp.transports.base import TransportPeer

        if not isinstance(peer, TransportPeer):
            return
        try:
            data = decode_payload(body)
        except Exception:
            data = {}
        pong_body = encode_payload({"sent_at": data.get("sent_at", time.time())})
        packet = build_header(MessageType.PONG, body=pong_body)
        await self._send_raw(peer.peer_id, peer.transport, packet)

    def get_peer_metrics(self: MessagingBackend, hash_id: str) -> dict[str, float | None]:
        link = self.get_link(hash_id)
        if link and link.rtt_ms is not None:
            return {
                "rtt_ms": link.rtt_ms,
                "link_quality_pct": link.link_quality_pct,
            }
        peer = self.discovery.get(hash_id)
        link_quality: float | None = None
        if link and link.transport == "serial" and self.serial_transport:
            link_quality = self.serial_transport.link_quality_pct()
        return {
            "rtt_ms": peer.rtt_ms if peer else (link.rtt_ms if link else None),
            "link_quality_pct": (
                peer.link_quality_pct
                if peer and peer.link_quality_pct is not None
                else link_quality
            ),
        }