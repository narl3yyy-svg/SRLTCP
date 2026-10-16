"""Connection and handshake mixin."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from srltcp.core.messaging.links import PeerLink
from srltcp.core.messaging.tor import ONION_VIRTUAL_PORT
from srltcp.core.protocol import pq
from srltcp.core.protocol.crypto import HANDSHAKE_MAX_SKEW, KeyExchange, SessionKeys
from srltcp.core.protocol.messages import MessageType, build_header, decode_payload, encode_payload
from srltcp.core.protocol.signed import verify_identity_binding
from srltcp.core.trusted import TrustedPeer
from srltcp.utils.logging import get_logger
from srltcp.utils.wan import resolve_wan_endpoint, validate_wan_port

if TYPE_CHECKING:
    from srltcp.core.messaging.backend import MessagingBackend

log = get_logger(__name__)


@dataclass(frozen=True)
class _Hello:
    """The key-exchange part of a HANDSHAKE / HANDSHAKE_ACK."""

    ephemeral: bytes
    signature: bytes
    timestamp: int
    kem: bytes  # ML-KEM public key (HANDSHAKE) or ciphertext (ACK)
    pq_public: bytes
    pq_signature: bytes


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(value: object, size: int) -> bytes:
    raw = base64.b64decode(str(value), validate=True)
    if len(raw) != size:
        raise ValueError("bad length")
    return raw

PENDING_HANDSHAKE_TTL = 15.0
DIRECT_ALIVE_WINDOW = 15.0  # a direct session heard from this recently is working (pings: 10 s)
HANDSHAKE_VERSION = 6  # v6: hybrid post-quantum (X25519+ML-KEM-768, Ed25519+ML-DSA-65)
# v5: windowed file transfers (FILE_ACK), Tor lanes
# v4: signature names the recipient (no cross-node replay)


class ConnectMixin:
    """Establish encrypted sessions with peers."""

    _pending_handshakes: dict[str, KeyExchange]

    def _init_connect(self: MessagingBackend) -> None:
        self._pending_handshakes = {}
        self._seen_handshake_nonces: dict[bytes, float] = {}
        self._connect_locks: dict[str, asyncio.Lock] = {}
        self._last_wan_dial: dict[str, float] = {}

    @staticmethod
    def _identity_protocol(data: dict[str, object]) -> bool:
        """Only the signed identity handshake is spoken (the experimental
        Noise XX option was retired; its code is gone)."""
        if str(data.get("protocol", "identity")) == "identity":
            return True
        log.warning("Handshake with an unsupported protocol (%s) refused", data.get("protocol"))
        return False

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
        if discovered and discovered.transport in ("tcp", "serial"):
            return discovered.transport
        trusted = self.trusted.get(hash_id)
        if trusted and (trusted.tcp_host or trusted.wan_host or trusted.alt_hosts):
            return "tcp"
        if trusted:
            return trusted.transport if trusted.transport in ("tcp", "serial") else "tcp"
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
            if self._is_link_reachable(existing):
                # Working session: keep it. Moving from the Tor doorbell to a
                # direct path is done by maintain_paths().
                await self.ping_peer(hash_id)
                return True
            await self._teardown_link(hash_id)
            existing = None

        if existing and not existing.handshake_complete and not force:
            if existing.transport != transport or not self._is_link_reachable(existing):
                await self._teardown_link(hash_id)
                existing = None
            else:
                pending = self._pending_handshakes.get(hash_id)
                if pending is not None and time.time() - pending.timestamp < 10:
                    # A handshake on this connection is already in flight. A
                    # second one would replace our key half while the peer
                    # answers the first: both sides end up with different
                    # keys and nothing decrypts. Let the first one finish.
                    return True
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
            if not endpoint and not host and isinstance(trusted, TrustedPeer) and trusted.alt_hosts:
                # Only addresses learnt from the contact (no saved main address).
                endpoint = (trusted.alt_hosts[0], trusted.tcp_port or self.config.tcp_port)
            if not endpoint:
                if not host and self._tor_path_to(hash_id):
                    return await self._connect_via_tor(hash_id, pub_bytes, peer_name)
                log.warning("No host for peer %s", hash_id[:8])
                return False
            candidates = [endpoint]
            if not host and trusted and isinstance(trusted, TrustedPeer):
                for alt in trusted.alt_hosts:
                    cand = (alt, endpoint[1])
                    if cand not in candidates:
                        candidates.append(cand)
            peer_id = ""
            target_host, target_port = endpoint
            # With Tor to fall back on (to set up a direct path), don't wait long for a dead
            # VPN/LAN path.
            tor_fallback = not host and self._tor_path_to(hash_id)
            fallback = tor_fallback
            timeout = 3.0 if fallback else (8.0 if len(candidates) == 1 else 5.0)
            # All addresses at once (LAN, VPN, the other network's address learnt
            # from the contact…), slightly staggered; the first to answer wins.
            peer_id, target_host, target_port = await self._dial_first(candidates, timeout)
            if not peer_id:
                if tor_fallback:
                    log.info("No direct path to %s — trying Tor", hash_id[:8])
                    return await self._connect_via_tor(hash_id, pub_bytes, peer_name)
                return False
            if trusted and isinstance(trusted, TrustedPeer) and target_host != trusted.tcp_host:
                # Remember the address that worked so the next dial is instant.
                others = [
                    h for h in [trusted.tcp_host, *trusted.alt_hosts] if h and h != target_host
                ]
                self.trusted.update(hash_id, tcp_host=target_host, alt_hosts=others)
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

    async def _dial_first(
        self: MessagingBackend, candidates: list[tuple[str, int]], timeout: float
    ) -> tuple[str, str, int]:
        """Happy-eyeballs dial: start each candidate 0.25 s after the previous,
        keep the first connection, close the rest. Returns (peer_id, host, port)."""
        assert self.tcp_transport is not None
        transport = self.tcp_transport

        async def one(index: int, host: str, port: int) -> tuple[str, str, int]:
            await asyncio.sleep(0.25 * index)
            try:
                return await transport.connect(host, port, timeout=timeout), host, port
            except (OSError, TimeoutError, ConnectionError) as exc:
                log.info("TCP connect to %s:%s failed: %s", host, port, exc or type(exc).__name__)
                return "", host, port

        tasks = [asyncio.create_task(one(i, h, p)) for i, (h, p) in enumerate(candidates)]
        winner: tuple[str, str, int] = ("", *candidates[0])
        try:
            pending = set(tasks)
            while pending and not winner[0]:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if not winner[0] and task.result()[0]:
                        winner = task.result()
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(BaseException):
                    extra = await task
                    if extra[0] and extra[0] != winner[0]:
                        await transport.disconnect(extra[0])
        return winner

    def _tor_path_to(self: MessagingBackend, hash_id: str) -> bool:
        return self.is_trusted(hash_id) and self.tor_ready() and bool(self.peer_onion(hash_id))

    async def _connect_via_tor(
        self: MessagingBackend, hash_id: str, pub_bytes: bytes, peer_name: str
    ) -> bool:
        """Session over Tor to a trusted contact's onion service."""
        try:
            peer_id = await self.dial_onion(hash_id)
        except (OSError, TimeoutError, ConnectionError, asyncio.IncompleteReadError) as exc:
            log.info("Tor path to %s failed: %s", hash_id[:8], exc or type(exc).__name__)
            return False
        link = PeerLink(
            hash_id=hash_id,
            transport_peer_id=peer_id,
            transport="tcp",
            address=f"{self.peer_onion(hash_id)}:{ONION_VIRTUAL_PORT}",
            public_key=pub_bytes,
            peer_name=peer_name,
        )
        self.register_link(link)
        await self._initiate_handshake(hash_id)
        log.info("Connecting to %s over Tor", hash_id[:8])
        return True

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

    async def _complete_handshake(
        self: MessagingBackend,
        remote_hash: str,
        remote_name: str,
        *,
        from_ack: bool = False,
    ) -> None:
        link = self.get_link(remote_hash)
        if not link:
            return
        if remote_name:
            link.peer_name = remote_name
        if link.link_up_announced:
            return  # re-key on an existing link
        link.link_up_announced = True
        self._cancel_reconnect(remote_hash)
        if not self.has_active_transfer_for(remote_hash):
            await self.ping_peer(remote_hash)
        await self._resume_paused_transfers_for_peer(remote_hash)
        # A failing UI callback must not stop queued messages from going out:
        # link_up_announced is already set, so this block never runs again.
        try:
            if self._on_link_up:
                await self._on_link_up(remote_hash, link.peer_name)
            if self._on_peer_metrics:
                metrics = self.get_peer_metrics(remote_hash)
                await self._on_peer_metrics(remote_hash, metrics)
        except Exception as exc:  # noqa: BLE001
            log.warning("Link-up UI callback failed for %s: %s", remote_hash[:8], exc)
        await self._send_pending_pair(remote_hash)
        if self.is_trusted(remote_hash):
            await self.send_pending_share_offers(remote_hash)
            await self.sync_link_up(remote_hash)
        await self.sync_groups_with(remote_hash)
        if self.is_trusted(remote_hash):
            await self.notify_contact_accepted(remote_hash)
            await self.flush_outbox(remote_hash)
        if self._link_on_tor(link) and self.is_trusted(remote_hash):
            # Found over Tor (the doorbell): set up the direct connection now.
            self.request_direct_upgrade(remote_hash, punch=True)
        log.info(
            "Link up with %s (%s)%s",
            link.peer_name,
            remote_hash[:8],
            " [ack]" if from_ack else "",
        )

    # ------------------------------------------------------------------
    # Handshake helpers
    # ------------------------------------------------------------------

    def _verify_remote_identity(
        self: MessagingBackend, data: dict[str, object]
    ) -> tuple[str, bytes, Ed25519PublicKey] | None:
        """Check that the claimed hash is derived from the claimed public key.

        Trust is keyed by hash, so this binding is what stops a peer from
        impersonating a contact. Also enforces block list and pinned keys.
        """
        remote_hash = str(data.get("hash_id", "")).strip().lower()
        pub_hex = str(data.get("public_key", "")).strip().lower()
        ed_pub = verify_identity_binding(remote_hash, pub_hex)
        if ed_pub is None:
            log.warning("Rejected handshake: hash %s does not match public key", remote_hash[:8])
            return None
        if remote_hash in self._own_hash_ids():
            log.debug("Ignoring handshake from our own identity")
            return None
        trusted = self.trusted.get(remote_hash)
        if trusted:
            if trusted.blocked:
                log.info("Rejected handshake from blocked peer %s", remote_hash[:8])
                return None
            pinned = (trusted.public_key or "").strip().lower()
            if pinned and pinned != pub_hex:
                log.warning("Rejected handshake: pinned key mismatch for %s", remote_hash[:8])
                return None
            if not pinned:
                self.trusted.set_public_key(remote_hash, pub_hex)
        return remote_hash, bytes.fromhex(pub_hex), ed_pub

    def _direct_alive(self: MessagingBackend, hash_id: str) -> bool:
        """A direct session with this contact that carried something lately."""
        link = self.get_link(hash_id)
        return bool(
            link
            and link.handshake_complete
            and link.transport == "tcp"
            and not self._link_on_tor(link)
            and self._is_link_reachable(link)
            and time.monotonic() - link.last_seen < DIRECT_ALIVE_WINDOW
        )

    def _handshake_is_fresh(self: MessagingBackend, nonce: bytes, timestamp: object) -> bool:
        """Reject stale or replayed handshake openings."""
        try:
            ts = float(timestamp)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        now = time.time()
        if abs(now - ts) > HANDSHAKE_MAX_SKEW:
            log.warning("Rejected handshake with timestamp skew %.0fs", now - ts)
            return False
        cutoff = now - HANDSHAKE_MAX_SKEW * 2
        if len(self._seen_handshake_nonces) > 256:
            self._seen_handshake_nonces = {
                k: v for k, v in self._seen_handshake_nonces.items() if v > cutoff
            }
        if nonce in self._seen_handshake_nonces:
            log.warning("Rejected replayed handshake")
            return False
        self._seen_handshake_nonces[nonce] = now
        return True

    def _wins_tie_break(self: MessagingBackend, remote_hash: str, transport: str) -> bool:
        """Deterministic choice when both peers dial each other at the same time."""
        return self._identity_for_transport(transport).hash_id < remote_hash

    def _pending_is_live(
        self: MessagingBackend, link: PeerLink | None, started: float
    ) -> bool:
        """True when our own outstanding handshake can still complete."""
        if not link or link.handshake_complete:
            return False
        if time.time() - started > PENDING_HANDSHAKE_TTL:
            return False
        return self._is_link_reachable(link)

    async def _reject_connection(self: MessagingBackend, peer_id: str) -> None:
        link = self.get_link_by_peer_id(peer_id)
        if link and link.handshake_complete:
            return
        if self.tcp_transport and self.tcp_transport.has_peer(peer_id):
            await self.tcp_transport.disconnect(peer_id)

    async def _bind_link(
        self: MessagingBackend,
        peer_id: str,
        remote_hash: str,
        remote_pub: bytes,
        remote_name: str,
    ) -> PeerLink | None:
        """Attach a verified identity to a transport connection."""
        link = self.get_link_by_peer_id(peer_id)
        if link and link.hash_id != remote_hash:
            log.warning(
                "Peer at %s proved identity %s but %s was expected — dropping",
                peer_id[:12],
                remote_hash[:8],
                link.hash_id[:8],
            )
            await self._teardown_link(link.hash_id)
            return None
        existing = self.get_link(remote_hash)
        if existing and existing.transport_peer_id != peer_id:
            # A newer, authenticated connection from the same identity replaces
            # the old one (peer restarted, or simultaneous dial tie-break).
            old_peer = existing.transport_peer_id
            old_transport = existing.transport
            self.remove_link(remote_hash)
            self._pending_handshakes.pop(remote_hash, None)
            if old_transport == "tcp" and self.tcp_transport:
                await self.tcp_transport.disconnect(old_peer)
        if link is None:
            transport = self._infer_transport(peer_id)
            address = ""
            if self.tcp_transport and transport == "tcp":
                for p in self.tcp_transport.peers():
                    if p.peer_id == peer_id:
                        address = p.address
                        break
            link = PeerLink(
                hash_id=remote_hash,
                transport_peer_id=peer_id,
                transport=transport,
                address=address,
                public_key=remote_pub,
                peer_name=remote_name,
            )
            self.register_link(link)
        else:
            link.public_key = remote_pub
            link.peer_name = remote_name or link.peer_name
        return link

    def _identity_fields(self: MessagingBackend, transport: str) -> dict[str, object]:
        identity = self._identity_for_transport(transport)
        return {
            "hash_id": identity.hash_id,
            "name": identity.name,
            "public_key": identity.public_bytes().hex(),
        }

    # ------------------------------------------------------------------
    # Default handshake (signed ephemeral X25519)
    # ------------------------------------------------------------------

    async def _initiate_handshake(self: MessagingBackend, hash_id: str) -> None:
        link = self.get_link(hash_id)
        if not link:
            return
        identity = self._identity_for_transport(link.transport)
        kx = KeyExchange(identity.private_key, identity.pq)
        kx.start_kem()
        self._pending_handshakes[hash_id] = kx
        packet = await asyncio.to_thread(
            self._identity_hello, MessageType.HANDSHAKE, kx, link.transport, hash_id
        )
        # The connection can close or be replaced (simultaneous dial) while
        # the post-quantum signature is computed off the event loop.
        if self.get_link(hash_id) is not link:
            return
        try:
            await self._send_raw(link.transport_peer_id, link.transport, packet)
        except (KeyError, OSError, ConnectionError) as exc:
            log.debug("Handshake to %s not sent, connection gone: %s", hash_id[:8], exc)
            if self._pending_handshakes.get(hash_id) is kx:
                self._pending_handshakes.pop(hash_id, None)

    def _identity_hello(
        self: MessagingBackend, kind: MessageType, kx: KeyExchange, transport: str, peer_hash: str
    ) -> bytes:
        identity = self._identity_for_transport(transport)
        body = encode_payload(
            {
                "protocol": "identity",
                "version": HANDSHAKE_VERSION,
                **self._identity_fields(transport),
                "ephemeral": kx.ephemeral_public.hex(),
                "timestamp": kx.timestamp,
                "signature": kx.sign_ephemeral(identity.hash_id, peer_hash).hex(),
                "kem": _b64(kx.kem_sent),
                "pq_public": _b64(kx.pq_public),
                "pq_signature": _b64(kx.sign_pq(identity.hash_id, peer_hash)),
            }
        )
        return build_header(kind, body=body)

    async def _send_upgrade_handshake(
        self: MessagingBackend, hash_id: str, peer_id: str
    ) -> KeyExchange | None:
        """Open a session on a new direct connection while the Tor session
        keeps working; it replaces the Tor link only once it's proven."""
        identity = self._identity_for_transport("tcp")
        kx = KeyExchange(identity.private_key, identity.pq)
        kx.start_kem()
        try:
            await self._send_raw(
                peer_id, "tcp",
                await asyncio.to_thread(
                    self._identity_hello, MessageType.HANDSHAKE, kx, "tcp", hash_id
                ),
            )
        except (KeyError, OSError, ConnectionError) as exc:
            log.debug("Direct upgrade to %s failed: %s", hash_id[:8], exc)
            return None
        return kx

    @staticmethod
    def _parse_identity_kx(data: dict[str, object], *, ack: bool) -> _Hello | None:
        if data.get("version") != HANDSHAKE_VERSION:
            log.warning("Handshake from an older SRLTCP version — both devices must be updated")
            return None
        try:
            eph = bytes.fromhex(str(data["ephemeral"]))
            sig = bytes.fromhex(str(data["signature"]))
            ts = int(data["timestamp"])  # type: ignore[call-overload]
            kem = _unb64(data["kem"], pq.KEM_CIPHERTEXT_SIZE if ack else pq.KEM_PUBLIC_SIZE)
            pq_public = _unb64(data["pq_public"], pq.DSA_PUBLIC_SIZE)
            pq_signature = _unb64(data["pq_signature"], pq.DSA_SIGNATURE_SIZE)
        except (KeyError, ValueError, TypeError):
            return None
        if len(eph) != 32 or len(sig) != 64:
            return None
        return _Hello(eph, sig, ts, kem, pq_public, pq_signature)

    @staticmethod
    def _complete_kx(
        kx: KeyExchange, hello: _Hello, ed_pub: Ed25519PublicKey, own_hash: str, peer_hash: str
    ) -> SessionKeys:
        return kx.complete(
            hello.ephemeral, hello.signature, ed_pub,
            peer_timestamp=hello.timestamp, own_hash=own_hash, peer_hash=peer_hash,
            peer_kem=hello.kem, peer_pq_public=hello.pq_public,
            peer_pq_signature=hello.pq_signature,
        )

    def _pq_key_ok(self: MessagingBackend, remote_hash: str, pq_public: bytes) -> bool:
        """Pin a contact's post-quantum identity key the first time we see it;
        a different key later is refused (someone else using their name)."""
        peer = self.trusted.get(remote_hash)
        if peer is None:
            return True  # not a contact: nothing pinned (contact requests only)
        pinned = getattr(peer, "pq_key", "")
        if not pinned:
            expect = getattr(peer, "pq_expect", "")
            if expect and hashlib.sha256(pq_public).hexdigest() != expect:
                # Not the key on the pairing code we scanned in person.
                log.warning(
                    "%s presented a post-quantum key that doesn't match their pairing code"
                    " — refused", remote_hash[:8],
                )
                return False
            self.trusted.update(remote_hash, pq_key=pq_public.hex(), pq_expect="")
            return True
        if pinned == pq_public.hex():
            return True
        log.warning(
            "%s presented a different post-quantum identity key — refused", remote_hash[:8]
        )
        return False

    async def _handle_handshake(
        self: MessagingBackend, peer_id: str, body: bytes, *, initiator: bool = False
    ) -> None:
        del initiator
        data = decode_payload(body)
        if not self._identity_protocol(data):
            await self._reject_connection(peer_id)
            return
        ident = self._verify_remote_identity(data)
        hello = self._parse_identity_kx(data, ack=False)
        if not ident or not hello:
            await self._reject_connection(peer_id)
            return
        remote_hash, remote_pub, ed_pub = ident
        remote_name = str(data.get("name", ""))[:64]
        if (
            self.tcp_transport
            and self.tcp_transport.is_tor(peer_id)
            and not self.is_trusted(remote_hash)
        ):
            # Our onion address is only given to contacts: a stranger using it
            # gets nothing, not even a contact request.
            log.info("Refused a Tor connection from a non-contact (%s)", remote_hash[:8])
            await self._reject_connection(peer_id)
            return
        if self.tcp_transport and self.tcp_transport.is_tor(peer_id) and self._direct_alive(
            remote_hash
        ):
            # Tor only rings the doorbell: while the direct connection works
            # it stays. (Letting the Tor one take over dropped the call, the
            # file, the speed, and had both sides punch again.)
            log.info("Kept the direct path to %s (refused a Tor session)", remote_hash[:8])
            await self._reject_connection(peer_id)
            return
        expected = self.tcp_transport.expected_peer(peer_id) if self.tcp_transport else None
        if expected and expected != remote_hash:
            # A direct UDP connection is set up with one contact (keys from our
            # session with them): nobody else may open a session on it.
            log.warning("Handshake from %s on a connection made for %s — refused",
                        remote_hash[:8], expected[:8])
            await self._reject_connection(peer_id)
            return

        existing = self.get_link(remote_hash)
        transport = existing.transport if existing else self._infer_transport(peer_id)
        pending_kx = self._pending_handshakes.get(remote_hash)
        if pending_kx is not None:
            if self._pending_is_live(existing, pending_kx.timestamp) and self._wins_tie_break(
                remote_hash, transport
            ):
                log.debug("Simultaneous handshake with %s — keeping ours", remote_hash[:8])
                return
            self._pending_handshakes.pop(remote_hash, None)

        identity = self._identity_for_transport(transport)
        kx = KeyExchange(identity.private_key, identity.pq)
        try:
            # Post-quantum math is pure Python on Android: off the event loop.
            await asyncio.to_thread(kx.respond_kem, hello.kem)
            keys = await asyncio.to_thread(
                self._complete_kx, kx, hello, ed_pub, identity.hash_id, remote_hash
            )
        except Exception as exc:
            log.warning("Handshake verification failed from %s: %s", remote_hash[:8], exc)
            await self._reject_connection(peer_id)
            return
        if not self._handshake_is_fresh(hello.ephemeral, hello.timestamp) or not self._pq_key_ok(
            remote_hash, hello.pq_public
        ):
            await self._reject_connection(peer_id)
            return
        link = await self._bind_link(peer_id, remote_hash, remote_pub, remote_name)
        if not link:
            return
        ack = await asyncio.to_thread(
            self._identity_hello, MessageType.HANDSHAKE_ACK, kx, link.transport, remote_hash
        )
        try:
            await self._send_raw(peer_id, link.transport, ack)
        except (KeyError, OSError, ConnectionError) as exc:
            # Closed while the signature was computed: no session on it.
            log.debug("Handshake answer to %s not sent: %s", remote_hash[:8], exc)
            return
        link.pq_public = hello.pq_public
        self.set_link_keys(remote_hash, keys)
        await self._complete_handshake(remote_hash, remote_name)

    async def _handle_handshake_ack(self: MessagingBackend, peer_id: str, body: bytes) -> None:
        data = decode_payload(body)
        if not self._identity_protocol(data):
            return
        ident = self._verify_remote_identity(data)
        hello = self._parse_identity_kx(data, ack=True)
        if not ident or not hello:
            return
        remote_hash, remote_pub, ed_pub = ident
        remote_name = str(data.get("name", ""))[:64]
        upgrade = self.upgrade_for_peer_id(peer_id)
        if upgrade is not None:
            await self._finish_upgrade(
                peer_id, upgrade, data, hello, remote_hash, remote_pub, ed_pub
            )
            return
        kx = self._pending_handshakes.get(remote_hash)
        link = self.get_link(remote_hash)
        if not kx or not link or link.transport_peer_id != peer_id:
            log.warning("Unexpected handshake ack from %s", peer_id[:8])
            return
        own = self._identity_for_transport(link.transport).hash_id
        try:
            keys = await asyncio.to_thread(self._complete_kx, kx, hello, ed_pub, own, remote_hash)
        except Exception as exc:
            log.warning("Handshake ack verification failed from %s: %s", remote_hash[:8], exc)
            return
        if not self._handshake_is_fresh(hello.ephemeral, hello.timestamp):
            return
        if not self._pq_key_ok(remote_hash, hello.pq_public):
            await self._teardown_link(remote_hash)
            return
        self._pending_handshakes.pop(remote_hash, None)
        link.public_key = remote_pub
        link.peer_name = remote_name or link.peer_name
        link.pq_public = hello.pq_public
        self.set_link_keys(remote_hash, keys)
        await self._complete_handshake(remote_hash, link.peer_name, from_ack=True)

    async def _finish_upgrade(
        self: MessagingBackend,
        peer_id: str,
        upgrade: tuple[str, KeyExchange],
        data: dict[str, object],
        hello: _Hello,
        remote_hash: str,
        remote_pub: bytes,
        ed_pub: Ed25519PublicKey,
    ) -> None:
        expected, kx = upgrade
        self._upgrades.pop(expected, None)
        own = self._identity_for_transport("tcp").hash_id
        try:
            if remote_hash != expected:
                raise ValueError("wrong identity on the direct path")
            keys = await asyncio.to_thread(self._complete_kx, kx, hello, ed_pub, own, remote_hash)
            if not self._handshake_is_fresh(hello.ephemeral, hello.timestamp):
                raise ValueError("stale handshake")
            if not self._pq_key_ok(remote_hash, hello.pq_public):
                raise ValueError("post-quantum identity key changed")
        except Exception as exc:  # noqa: BLE001
            log.warning("Direct path to %s rejected: %s", expected[:8], exc)
            if self.tcp_transport:
                await self.tcp_transport.disconnect(peer_id)
            return
        old = self.get_link(remote_hash)
        address = ""
        if self.tcp_transport:
            for p in self.tcp_transport.peers():
                if p.peer_id == peer_id:
                    address = p.address
        link = PeerLink(
            hash_id=remote_hash,
            transport_peer_id=peer_id,
            transport="tcp",
            address=address,
            public_key=remote_pub,
            peer_name=str(data.get("name", ""))[:64] or (old.peer_name if old else ""),
            pq_public=hello.pq_public,
        )
        self.register_link(link)  # replaces the Tor link
        if (
            old and old.transport == "tcp" and old.transport_peer_id != peer_id
            and self.tcp_transport
        ):
            # The Tor connection it replaces isn't needed any more.
            await self.tcp_transport.disconnect(old.transport_peer_id)
        self.set_link_keys(remote_hash, keys)
        log.info("Switched %s from Tor to a direct path (%s)", remote_hash[:8], address)
        await self._complete_handshake(remote_hash, link.peer_name, from_ack=True)
