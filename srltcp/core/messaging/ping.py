"""Ping / RTT and link quality metrics."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from srltcp.core.messaging.constants import PING_INTERVAL
from srltcp.core.protocol.messages import MessageType, build_header, decode_payload, encode_payload
from srltcp.utils.logging import get_logger

LINK_DEAD_AFTER = 3.2 * PING_INTERVAL  # ~32 s without any frame or pong
NETWORK_CHECK_EVERY = 3.0

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
        self._ping_tick = 0
        self._net_changed_tick = 0

    def _own_hash_ids(self: MessagingBackend) -> set[str]:
        return {i.hash_id for i in self.identities.values()}

    async def start_ping_loop(self: MessagingBackend) -> None:
        async def _loop() -> None:
            tick = 0
            last_ips: set[str] = set()
            while self._running:
                # Own address changed (network switch / VPN reconnect) while
                # auto-announce is off: still tell VPN peers where we are now.
                if not self.config.announce:
                    try:
                        ips = self._own_ips()
                        if last_ips and ips != last_ips:
                            log.info("Network address changed — notifying VPN peers")
                            await self.sweep_vpn()
                        last_ips = ips
                    except Exception as exc:  # noqa: BLE001 - keep the loop alive
                        log.debug("Address change check failed: %s", exc)
                for link in self.list_links():
                    hash_id = link.get("hash_id", "")
                    if link.get("handshake_complete") and not self.has_active_transfer_for(
                        hash_id
                    ):
                        try:
                            await self.ping_peer(hash_id)
                        except (KeyError, OSError, ConnectionError) as exc:
                            log.debug("Ping to %s failed: %s", hash_id[:8], exc)
                # Retry undelivered messages: every 30 s for the first few
                # minutes, then every ~2 minutes.
                if tick % (3 if tick < 30 else 12) == 0:
                    self.retry_outbox_connections()
                try:
                    await self._check_stale_offers()
                    await self.expire_messages()
                except Exception as exc:  # noqa: BLE001 - keep the loop alive
                    log.debug("Stale offer check failed: %s", exc)
                try:
                    await self.maintain_paths()  # Tor-only sessions → direct
                except Exception as exc:  # noqa: BLE001 - keep the loop alive
                    log.debug("Path check failed: %s", exc)
                if tick:
                    try:
                        await self.reap_dead_links(LINK_DEAD_AFTER)
                    except Exception as exc:  # noqa: BLE001 - keep the loop alive
                        log.debug("Link reaper failed: %s", exc)
                # Contacts we have no session with: every 30 s for the first few
                # minutes (after start or a network change), then every 2 min.
                # Matters when only one side can dial (e.g. VPN phone -> home LAN).
                since_change = tick - self._net_changed_tick
                if tick and tick % (3 if since_change < 30 else 12) == 0:
                    self.connect_known_contacts()
                tick += 1
                self._ping_tick = tick
                await asyncio.sleep(PING_INTERVAL)

        self._ping_tasks.append(asyncio.create_task(_loop()))
        self._ping_tasks.append(asyncio.create_task(self._watch_network()))

    async def _watch_network(self: MessagingBackend) -> None:
        """Notice right away when an interface goes away (VPN switched off,
        Wi-Fi lost): drop direct sessions that used it so they move to the
        other paths within seconds instead of waiting ~50 s for pings to fail."""
        last: set[str] = set()
        while self._running:
            await asyncio.sleep(NETWORK_CHECK_EVERY)
            try:
                ips = await asyncio.to_thread(self._own_ips)
            except Exception:  # noqa: BLE001 - best effort
                continue
            if last and ips != last and self.tcp_transport:
                for hash_id, link in list(self._links.items()):
                    if link.transport != "tcp":
                        continue
                    local = self.tcp_transport.local_ip(link.transport_peer_id)
                    if local and local not in ips and local not in ("127.0.0.1", "::1"):
                        log.info("Interface %s went away — moving %s off it", local, hash_id[:8])
                        name = link.peer_name
                        await self._teardown_link(hash_id)
                        if self._on_link_down:
                            await self._on_link_down(hash_id, name)
                        self._kick_connect(hash_id)
                await self.addresses_changed()
                await self._refresh_udp()  # new network: new public UDP address
                if ips - last:
                    # A network came up (VPN connected, new Wi-Fi): try contacts now.
                    self._net_changed_tick = self._ping_tick
                    self.connect_known_contacts()
            last = ips

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
        link.last_seen = time.monotonic()
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