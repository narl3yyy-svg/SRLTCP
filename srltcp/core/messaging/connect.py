"""Connection and handshake mixin."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING

from srltcp.core.messaging.links import PeerLink
from srltcp.core.protocol.crypto import KeyExchange, load_public_key
from srltcp.core.protocol.messages import MessageType, build_header, decode_payload, encode_payload
from srltcp.core.trusted import TrustedPeer
from srltcp.utils.logging import get_logger
from srltcp.utils.wan import resolve_wan_endpoint, validate_wan_port

if TYPE_CHECKING:
    from srltcp.core.messaging.backend import MessagingBackend

log = get_logger(__name__)


class ConnectMixin:
    """Establish encrypted sessions with peers."""

    _pending_handshakes: dict[str, KeyExchange]

    def _init_connect(self: MessagingBackend) -> None:
        self._pending_handshakes = {}
        self._connect_locks: dict[str, asyncio.Lock] = {}
        self._last_wan_dial: dict[str, float] = {}

    def _resolve_tcp_endpoint(
        self: MessagingBackend,
        hash_id: str,
        *,
        host: str | None,
        port: int | None,
        discovered: object | None,
        trusted: object | None,
    ) -> tuple[str, int] | None:
        if host:
            port_val = port or self.config.tcp_port
            wan_dial = (
                trusted
                and isinstance(trusted, TrustedPeer)
                and (
                    trusted.connection_mode == "wan"
                    or (trusted.wan_enabled and trusted.wan_host == host.strip())
                )
            )
            try:
                if wan_dial:
                    endpoint = resolve_wan_endpoint(host, port_val)
                    return endpoint.host, endpoint.port
                return host.strip(), validate_wan_port(port_val)
            except ValueError as exc:
                log.warning("Invalid endpoint %s:%s — %s", host, port_val, exc)
                return None

        mode = "auto"
        if trusted and isinstance(trusted, TrustedPeer):
            mode = trusted.connection_mode or "auto"

        use_wan = mode == "wan" or (
            mode == "auto"
            and trusted
            and isinstance(trusted, TrustedPeer)
            and trusted.wan_enabled
            and trusted.wan_host
        )

        if use_wan and trusted and isinstance(trusted, TrustedPeer):
            key = f"{trusted.wan_host}:{trusted.wan_port}"
            now = time.monotonic()
            last = self._last_wan_dial.get(key, 0.0)
            if now - last < 1.0:
                log.debug("WAN dial rate-limited for %s", key)
            else:
                self._last_wan_dial[key] = now
            try:
                endpoint = resolve_wan_endpoint(
                    trusted.wan_host, trusted.wan_port or self.config.tcp_port
                )
                log.info(
                    "WAN dial to %s (%s) for peer %s",
                    endpoint.host,
                    endpoint.resolved_ip,
                    hash_id[:8],
                )
                return endpoint.host, endpoint.port
            except ValueError as exc:
                log.warning("WAN endpoint invalid for %s: %s", hash_id[:8], exc)
                if mode == "wan":
                    return None

        lan_host = ""
        lan_port: int | None = None
        if discovered:
            lan_host = getattr(discovered, "tcp_host", "") or ""
            lan_port = getattr(discovered, "tcp_port", None)
        if not lan_host and trusted and isinstance(trusted, TrustedPeer):
            lan_host = trusted.tcp_host
            lan_port = trusted.tcp_port or self.config.tcp_port
        if lan_host:
            port_val = lan_port or self.config.tcp_port
            return lan_host, validate_wan_port(port_val)
        return None

    async def _teardown_link(self: MessagingBackend, hash_id: str) -> None:
        link = self.get_link(hash_id)
        if not link:
            return
        self._pending_handshakes.pop(hash_id, None)
        peer_id = link.transport_peer_id
        transport = link.transport
        self.remove_link(hash_id)
        if transport == "tcp" and self.tcp_transport:
            await self.tcp_transport.disconnect(peer_id)

    async def connect_to_peer(
        self: MessagingBackend,
        hash_id: str,
        *,
        host: str | None = None,
        port: int | None = None,
        transport: str = "tcp",
        force: bool = False,
    ) -> bool:
        """Dial a peer by hash (uses discovery registry for address)."""
        lock = self._connect_locks.setdefault(hash_id, asyncio.Lock())
        async with lock:
            return await self._connect_to_peer_locked(
                hash_id, host=host, port=port, transport=transport, force=force
            )

    def _resolve_transport(
        self: MessagingBackend, hash_id: str, requested: str
    ) -> str:
        discovered = self.discovery.get(hash_id, requested) or self.discovery.get(
            hash_id
        )
        if discovered:
            return discovered.transport
        trusted = self.trusted.get(hash_id)
        if trusted:
            return trusted.transport
        return requested

    def _is_link_reachable(self: MessagingBackend, link: PeerLink) -> bool:
        if link.transport == "tcp" and self.tcp_transport:
            return self.tcp_transport.has_peer(link.transport_peer_id)
        if link.transport == "serial" and self.serial_transport:
            return self.serial_transport.has_peer(link.transport_peer_id)
        return False

    def _infer_transport(self: MessagingBackend, peer_id: str) -> str:
        if self.serial_transport and self.serial_transport.has_peer(peer_id):
            return "serial"
        if self.tcp_transport and self.tcp_transport.has_peer(peer_id):
            return "tcp"
        return "tcp"

    async def _connect_to_peer_locked(
        self: MessagingBackend,
        hash_id: str,
        *,
        host: str | None = None,
        port: int | None = None,
        transport: str = "tcp",
        force: bool = False,
    ) -> bool:
        transport = self._resolve_transport(hash_id, transport)
        discovered = self.discovery.get(hash_id, transport) or self.discovery.get(
            hash_id
        )
        trusted = self.trusted.get(hash_id)
        if not discovered and not trusted and not host:
            log.warning("Peer %s not in discovery registry", hash_id[:8])
            return False
        if trusted and trusted.blocked:
            log.warning("Peer %s is blocked", hash_id[:8])
            return False

        existing = self.get_link(hash_id)
        if existing and existing.handshake_complete and not force:
            if existing.transport == transport and self._is_link_reachable(existing):
                await self.ping_peer(hash_id)
                return True
            await self._teardown_link(hash_id)
            existing = None

        if existing and not existing.handshake_complete and not force:
            if existing.transport != transport or not self._is_link_reachable(existing):
                await self._teardown_link(hash_id)
                existing = None
            else:
                try:
                    await self._initiate_handshake(hash_id)
                    return True
                except (KeyError, RuntimeError, OSError) as exc:
                    log.warning(
                        "Handshake retry failed for %s: %s", hash_id[:8], exc
                    )
                    await self._teardown_link(hash_id)
                    existing = None

        if existing and force:
            if self.has_active_transfer_for(hash_id):
                log.info(
                    "Skipping forced reconnect for %s — transfer active",
                    hash_id[:8],
                )
                return existing.handshake_complete
            await self._teardown_link(hash_id)

        peer_name = (
            (discovered.name if discovered else "")
            or (trusted.name if trusted else "")
        )
        pub_hex = (discovered.public_key if discovered else "") or (
            trusted.public_key if trusted else ""
        )
        pub_bytes = bytes.fromhex(pub_hex) if pub_hex else b"\x00" * 32

        if transport == "tcp" and self.tcp_transport:
            endpoint = self._resolve_tcp_endpoint(
                hash_id,
                host=host,
                port=port,
                discovered=discovered,
                trusted=trusted,
            )
            if not endpoint:
                log.warning("No host for peer %s", hash_id[:8])
                return False
            target_host, target_port = endpoint
            try:
                peer_id = await self.tcp_transport.connect(target_host, target_port)
            except (OSError, TimeoutError, ConnectionError) as exc:
                log.warning(
                    "TCP connect to %s:%s failed: %s",
                    target_host,
                    target_port,
                    exc,
                )
                return False
            link = PeerLink(
                hash_id=hash_id,
                transport_peer_id=peer_id,
                transport="tcp",
                address=f"{target_host}:{target_port}",
                public_key=pub_bytes,
                peer_name=peer_name,
            )
            self.register_link(link)
            await self._initiate_handshake(hash_id)
            return True

        if transport == "serial" and self.serial_transport:
            peers = self.serial_transport.peers()
            if not peers:
                log.warning("Serial transport has no peer for %s", hash_id[:8])
                return False
            serial_peer = peers[0]
            link = PeerLink(
                hash_id=hash_id,
                transport_peer_id=serial_peer.peer_id,
                transport="serial",
                address=serial_peer.address,
                public_key=pub_bytes,
                peer_name=peer_name,
            )
            self.register_link(link)
            await self._initiate_handshake(hash_id)
            return True

        return False

    async def wait_for_handshake(
        self: MessagingBackend, hash_id: str, *, timeout: float = 10.0
    ) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            link = self.get_link(hash_id)
            if link and link.handshake_complete:
                return True
            await asyncio.sleep(0.1)
        link = self.get_link(hash_id)
        return bool(link and link.handshake_complete)

    def _cancel_reconnect(self: MessagingBackend, hash_id: str) -> None:
        task = self._reconnect_tasks.pop(hash_id, None)
        if task and not task.done():
            task.cancel()

    async def disconnect_peer(self: MessagingBackend, hash_id: str) -> bool:
        self._cancel_reconnect(hash_id)
        if not self.get_link(hash_id):
            return False
        await self._teardown_link(hash_id)
        return True

    async def _initiate_handshake(self: MessagingBackend, hash_id: str) -> None:
        link = self.get_link(hash_id)
        if not link:
            return
        identity = self._identity_for_transport(link.transport)
        kx = KeyExchange(identity.private_key)
        self._pending_handshakes[hash_id] = kx
        body = encode_payload(
            {
                "hash_id": identity.hash_id,
                "name": identity.name,
                "public_key": identity.public_bytes().hex(),
                "ephemeral": kx.ephemeral_public.hex(),
                "signature": kx.sign_ephemeral().hex(),
            }
        )
        packet = build_header(MessageType.HANDSHAKE, body=body)
        await self._send_raw(link.transport_peer_id, link.transport, packet)

    async def _complete_handshake(
        self: MessagingBackend,
        remote_hash: str,
        remote_name: str,
        *,
        from_ack: bool = False,
    ) -> None:
        link = self.get_link(remote_hash)
        if link and link.handshake_complete:
            link.peer_name = remote_name
            return
        if link:
            link.peer_name = remote_name
        self._cancel_reconnect(remote_hash)
        if not self.has_active_transfer_for(remote_hash):
            await self.ping_peer(remote_hash)
        await self._resume_paused_transfers_for_peer(remote_hash)
        if self._on_link_up:
            await self._on_link_up(remote_hash, remote_name)
        if self._on_peer_metrics:
            metrics = self.get_peer_metrics(remote_hash)
            await self._on_peer_metrics(remote_hash, metrics)
        log.info(
            "Link up with %s (%s)%s",
            remote_name,
            remote_hash[:8],
            " [ack]" if from_ack else "",
        )

    async def _handle_handshake(
        self: MessagingBackend, peer_id: str, body: bytes, *, initiator: bool
    ) -> None:
        data = decode_payload(body)
        remote_hash = data["hash_id"]
        remote_pub = bytes.fromhex(data["public_key"])
        remote_eph = bytes.fromhex(data["ephemeral"])
        remote_sig = bytes.fromhex(data["signature"])
        remote_name = data.get("name", "")

        link = self.get_link_by_peer_id(peer_id)
        existing = self.get_link(remote_hash)
        if existing and existing.handshake_complete and existing.transport_peer_id != peer_id:
            if self.tcp_transport:
                await self.tcp_transport.disconnect(peer_id)
            return
        if not link:
            transport = (
                existing.transport
                if existing
                else self._infer_transport(peer_id)
            )
            link = PeerLink(
                hash_id=remote_hash,
                transport_peer_id=peer_id,
                transport=transport,
                address=existing.address if existing else "",
                public_key=remote_pub,
                peer_name=remote_name,
            )
            self.register_link(link)
        else:
            old_hash = link.hash_id
            if old_hash != remote_hash:
                self._links.pop(old_hash, None)
            link.hash_id = remote_hash
            link.public_key = remote_pub
            link.peer_name = remote_name
            self._links[remote_hash] = link
            self._peer_id_to_hash[peer_id] = remote_hash
            if old_hash in self._pending_handshakes and old_hash != remote_hash:
                self._pending_handshakes[remote_hash] = self._pending_handshakes.pop(old_hash)

        identity = self._identity_for_transport(link.transport)
        if remote_hash in self._pending_handshakes:
            kx = self._pending_handshakes.pop(remote_hash)
            keys = kx.complete(
                remote_eph,
                remote_sig,
                load_public_key(remote_pub),
                initiator=True,
            )
        else:
            kx = KeyExchange(identity.private_key)
            keys = kx.complete(
                remote_eph,
                remote_sig,
                load_public_key(remote_pub),
                initiator=False,
            )
            ack_body = encode_payload(
                {
                    "hash_id": identity.hash_id,
                    "name": identity.name,
                    "public_key": identity.public_bytes().hex(),
                    "ephemeral": kx.ephemeral_public.hex(),
                    "signature": kx.sign_ephemeral().hex(),
                }
            )
            ack = build_header(MessageType.HANDSHAKE_ACK, body=ack_body)
            await self._send_raw(peer_id, link.transport, ack)

        self.set_link_keys(remote_hash, keys)
        await self._complete_handshake(remote_hash, remote_name)

    async def _handle_handshake_ack(self: MessagingBackend, peer_id: str, body: bytes) -> None:
        data = decode_payload(body)
        remote_hash = data["hash_id"]
        remote_pub = bytes.fromhex(data["public_key"])
        remote_eph = bytes.fromhex(data["ephemeral"])
        remote_sig = bytes.fromhex(data["signature"])
        remote_name = data.get("name", "")

        kx = self._pending_handshakes.pop(remote_hash, None)
        if not kx:
            link = self.get_link_by_peer_id(peer_id)
            if link:
                kx = self._pending_handshakes.pop(link.hash_id, None)
                if kx:
                    remote_hash = link.hash_id
        if not kx:
            log.warning("Unexpected handshake ack from %s", peer_id[:8])
            return

        keys = kx.complete(
            remote_eph,
            remote_sig,
            load_public_key(remote_pub),
            initiator=True,
        )
        self.set_link_keys(remote_hash, keys)
        link = self.get_link(remote_hash)
        if link:
            link.peer_name = remote_name
        await self._complete_handshake(remote_hash, remote_name, from_ack=True)