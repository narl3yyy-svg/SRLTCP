"""Announce and discovery mixin."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING

from srltcp.core.messaging.constants import DISCOVERY_PORT
from srltcp.core.protocol.messages import MessageType, build_header
from srltcp.core.protocol.signed import sign_payload
from srltcp.utils.logging import get_logger
from srltcp.utils.network import in_vpn_subnet, primary_ipv4, sweep_targets

if TYPE_CHECKING:
    from srltcp.core.messaging.backend import MessagingBackend

log = get_logger(__name__)

ANNOUNCE_BURSTS = 3
ANNOUNCE_BURST_DELAY = 0.12
SWEEP_EVERY_N_LOOPS = 6  # auto-announce: VPN sweep every ~30 s (~250 tiny packets)
REPLY_INTERVAL = 20.0  # answer a given address at most this often
DISCOVERABLE_AFTER_MANUAL = 600.0  # reply to announces for 10 min after pressing Announce


class AnnounceError(Exception):
    """Raised when a manual announce cannot be sent on the requested transport."""

    def __init__(self, transport: str, reason: str) -> None:
        self.transport = transport
        self.reason = reason
        super().__init__(reason)


async def resolve_peer_address(host: str) -> str:
    """Accept a LAN/VPN/public IP or hostname; return an IP to send to."""
    import ipaddress
    import socket

    cleaned = (host or "").strip().strip("[]")
    if not cleaned or len(cleaned) > 253:
        raise AnnounceError("tcp", "Enter an IP address or hostname")
    try:
        ip = ipaddress.ip_address(cleaned)
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                cleaned, None, family=socket.AF_INET, type=socket.SOCK_DGRAM
            )
        except OSError as exc:
            raise AnnounceError("tcp", f"Cannot resolve {cleaned}") from exc
        if not infos:  # pragma: no cover - getaddrinfo raises instead
            raise AnnounceError("tcp", f"Cannot resolve {cleaned}") from None
        ip = ipaddress.ip_address(infos[0][4][0])
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast:
        raise AnnounceError("tcp", "That address cannot be a peer")
    return str(ip)


class AnnounceMixin:
    """Broadcast node presence on enabled transports."""

    _announce_tasks: list[asyncio.Task[None]]

    def _init_announce(self: MessagingBackend) -> None:
        self._announce_tasks = []
        self._last_manual_announce = 0.0
        self._announce_replies: dict[str, float] = {}

    def is_discoverable(self: MessagingBackend) -> bool:
        """Answer other devices' announces only while we are announcing ourselves."""
        if self.config.hub_mode:
            return False
        if self.config.announce:
            return True
        return time.monotonic() - self._last_manual_announce < DISCOVERABLE_AFTER_MANUAL

    def build_announce_payload(
        self: MessagingBackend, transport: str, *, reply: bool = False
    ) -> bytes:
        identity = self.identities.get(transport) or self.identities.get("tcp")
        if not identity:
            raise AnnounceError(transport, f"No identity for transport: {transport}")
        discovery_port = (
            self.tcp_transport.discovery_port
            if self.tcp_transport
            else DISCOVERY_PORT
        )
        payload = {
            "type": "announce",
            "hash_id": identity.hash_id,
            "name": identity.name,
            "public_key": identity.public_bytes().hex(),
            "transport": transport,
            "tcp_host": self._lan_ip(),
            "tcp_port": self.config.tcp_port,
            "discovery_port": discovery_port,
            "ts": int(time.time()),
        }
        if reply:
            payload["reply"] = True  # receivers never answer a reply (no ping-pong)
        return sign_payload(payload, identity.private_key)

    def _lan_ip(self: MessagingBackend) -> str:
        if self.config.lan_ip:
            return self.config.lan_ip
        return primary_ipv4()

    def _own_ips(self: MessagingBackend) -> set[str]:
        from srltcp.utils.network import list_interfaces

        return {str(i.get("ip", "")) for i in list_interfaces()}

    async def sweep_vpn(self: MessagingBackend) -> int:
        """Unicast our announce across VPN subnets (WireGuard carries no broadcast)."""
        if not self.tcp_transport or (hasattr(self, "_hub_enabled") and self._hub_enabled()):
            return 0
        hosts = await asyncio.to_thread(sweep_targets, self._own_ips())
        if not hosts:
            return 0
        payload = self.build_announce_payload("tcp")
        sent = await self.tcp_transport.unicast_discovery(payload, hosts)
        log.info("VPN discovery sweep: announced to %d addresses", len(hosts))
        return sent

    async def announce_to(self: MessagingBackend, host: str) -> None:
        """Announce directly to one address (Find by IP)."""
        if not self.tcp_transport:
            raise AnnounceError("tcp", "TCP transport is not enabled")
        host = await resolve_peer_address(host)
        self._last_manual_announce = time.monotonic()
        payload = self.build_announce_payload("tcp")
        for _ in range(ANNOUNCE_BURSTS):
            await self.tcp_transport.unicast_discovery(payload, [host])
            await asyncio.sleep(ANNOUNCE_BURST_DELAY)

    def _verify_link_soon(self: MessagingBackend, hash_id: str, wait: float = 6.0) -> None:
        """Ping a link now; if no answer arrives within ``wait`` s, replace it."""
        link = self.get_link(hash_id)
        if not link:
            return
        started = time.monotonic()

        async def _check() -> None:
            with contextlib.suppress(KeyError, OSError, ConnectionError):
                await self.ping_peer(hash_id)
            await asyncio.sleep(wait)
            current = self.get_link(hash_id)
            if current is link and link.last_seen < started:
                log.info("Link to %s did not answer after address change — redialing", hash_id[:8])
                await self._teardown_link(hash_id)
                self._kick_connect(hash_id)

        asyncio.ensure_future(_check())

    async def _reply_to_announce(
        self: MessagingBackend, address: str, discovery_port: object
    ) -> None:
        if not self.tcp_transport or ":" not in address:
            return
        ip = address.rsplit(":", 1)[0]
        now = time.monotonic()
        if now - self._announce_replies.get(ip, 0.0) < REPLY_INTERVAL:
            return
        self._announce_replies[ip] = now
        if len(self._announce_replies) > 512:
            cutoff = now - REPLY_INTERVAL
            self._announce_replies = {k: v for k, v in self._announce_replies.items() if v > cutoff}
        ports = {DISCOVERY_PORT, self.tcp_transport.discovery_port}
        try:
            port = int(discovery_port)  # type: ignore[call-overload]
            if 1 <= port <= 65535:
                ports.add(port)
        except (TypeError, ValueError):
            pass
        payload = self.build_announce_payload("tcp", reply=True)
        await self.tcp_transport.unicast_discovery(payload, [ip], ports)

    async def _send_announce(
        self: MessagingBackend, transport_name: str, *, sweep: bool = False
    ) -> None:
        transport_name = transport_name.lower()
        if transport_name not in ("tcp", "serial"):
            raise AnnounceError(transport_name, f"Unknown transport: {transport_name}")

        payload = self.build_announce_payload(transport_name)
        if transport_name == "tcp":
            if hasattr(self, "_hub_enabled") and self._hub_enabled():
                await self._send_hub_register()
            elif not self.tcp_transport:
                raise AnnounceError(transport_name, "TCP transport is not enabled")
            else:
                self.tcp_transport.set_announce_payload(payload)
                if not await self.tcp_transport.broadcast_discovery(payload):
                    raise AnnounceError(
                        transport_name,
                        "UDP discovery socket is not available — restart the node",
                    )
                if sweep:
                    await self.sweep_vpn()
        elif transport_name == "serial":
            if not self.serial_transport and self.config.enable_serial:
                result = await self.ensure_serial_transport()
                if result.get("serial") != "running":
                    err = (
                        result.get("error")
                        or getattr(self, "_serial_error", None)
                        or "Serial transport is not open — check port and permissions"
                    )
                    raise AnnounceError(transport_name, err)
            if not self.serial_transport:
                raise AnnounceError(
                    transport_name,
                    "Serial is disabled — enable it in Settings → Serial",
                )
            packet = build_header(MessageType.ANNOUNCE, body=payload)
            peers = self.serial_transport.peers()
            if peers:
                await self.serial_transport.send(peers[0].peer_id, packet)
            else:
                await self.serial_transport.broadcast(packet)
        log.info("Announced on %s", transport_name)

    async def announce(
        self: MessagingBackend, transport: str | None = None, *, manual: bool = True
    ) -> list[str]:
        transports: list[str] = []
        if transport:
            transports = [transport.strip().lower()]
        else:
            if self.tcp_transport:
                transports.append("tcp")
            if self.serial_transport:
                transports.append("serial")

        if not transports:
            raise AnnounceError("all", "No transports are available to announce on")

        if manual:
            self._last_manual_announce = time.monotonic()
        announced: list[str] = []
        for t in transports:
            for i in range(ANNOUNCE_BURSTS):
                # Manual announces also sweep VPN subnets once.
                await self._send_announce(t, sweep=manual and i == 0)
                await asyncio.sleep(ANNOUNCE_BURST_DELAY)
            announced.append(t)
        return announced

    async def start_announce_loop(self: MessagingBackend) -> None:
        await self.stop_announce_loop()
        from srltcp.core.messaging.constants import ANNOUNCE_INTERVAL

        async def _loop() -> None:
            n = 0
            last_ips: set[str] = set()
            while self._running and self.config.announce:
                try:
                    # Our address changed (Wi-Fi <-> mobile data, VPN reconnect):
                    # tell contacts right away instead of waiting for the next sweep.
                    ips = self._own_ips()
                    moved = bool(last_ips) and ips != last_ips
                    last_ips = ips
                    if moved:
                        log.info("Network address changed — announcing now")
                    for t in ("tcp", "serial"):
                        if (t == "tcp" and self.tcp_transport) or (
                            t == "serial" and self.serial_transport
                        ):
                            sweep = t == "tcp" and (moved or n % SWEEP_EVERY_N_LOOPS == 0)
                            await self._send_announce(t, sweep=sweep)
                except Exception as exc:  # noqa: BLE001 - keep announcing after transient errors
                    log.debug("Auto-announce failed: %s", exc)
                n += 1
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
        if is_new and peer.transport == "serial":
            log.info(
                "Discovered serial peer %s (%s)",
                peer.name,
                peer.hash_id[:8],
            )
        # Keep trusted contacts reachable when their IP changes (DHCP, VPN, roaming).
        trusted = self.trusted.get(peer.hash_id)
        fresh = abs(time.time() - float(peer.metadata.get("ts") or 0)) < 300
        if (
            trusted
            and fresh  # a replayed old announce must not redirect a contact
            and peer.transport == "tcp"
            and peer.tcp_host
            and (trusted.tcp_host != peer.tcp_host or trusted.tcp_port != peer.tcp_port)
        ):
            alts = [h for h in [trusted.tcp_host, *trusted.alt_hosts] if h and h != peer.tcp_host]
            self.trusted.update(
                peer.hash_id, tcp_host=peer.tcp_host, tcp_port=peer.tcp_port, alt_hosts=alts
            )
            log.info("Updated address for %s to %s:%d", trusted.name, peer.tcp_host, peer.tcp_port)
            link = self.get_link(peer.hash_id)
            if (
                link
                and link.transport == "tcp"
                and link.address
                and not link.address.startswith(f"{peer.tcp_host}:")
                and not self.has_active_transfer_for(peer.hash_id)
            ):
                # Our session points at the old address: check it still answers.
                self._verify_link_soon(peer.hash_id)
        # Answer so the sender discovers us too (one press on one phone is enough).
        src_ip = address.rsplit(":", 1)[0] if ":" in address else ""
        if (
            peer.transport == "tcp"
            and address != "hub"
            and not peer.metadata.get("reply")
            and (self.is_discoverable() or (src_ip and in_vpn_subnet(src_ip)))
        ):
            try:
                await self._reply_to_announce(address, peer.metadata.get("discovery_port"))
            except OSError as exc:
                log.debug("Announce reply to %s failed: %s", address, exc)
        if self._on_peer_discovered:
            await self._on_peer_discovered(peer.to_dict())