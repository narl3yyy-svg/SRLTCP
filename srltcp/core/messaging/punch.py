"""Direct connections across networks without port forwarding (hole punching).

Two devices behind ordinary home routers (NAT) or IPv6 firewalls can't accept
incoming connections, but each router lets an *outgoing* connection's replies
back in. If both devices dial each other at the same moment from the same
local port, each router sees the other's packets as replies to its own
connection and lets them through: a direct TCP connection (TCP simultaneous
open), no relay in the path and nothing to configure on either router.

How the two agree on "the same moment" and where to dial, privately:

1. Each phone connects to its relay from its *punch port* (listen port + 3);
   the relay tells it its public address as seen from the internet.
2. While the contacts already have a session (usually over the relay), they
   swap addresses inside the E2EE session: ``RENDEZVOUS`` carries LAN/VPN
   addresses (for plain direct dials) and, if *direct internet connections*
   is on, the public ones; ``PUNCH`` offer/answer starts a round.
3. Both dial all of the other's public candidates from the punch port for a
   few seconds; the first connection that opens is turned into a session
   with the normal signed handshake, which replaces the relay session.

What it can't do: carrier-grade NAT with per-destination ports ("symmetric")
won't open — then the relay keeps carrying the traffic. Addresses are shared
only with trusted contacts, inside the encrypted session; turning off *direct
internet connections* keeps your public IP away from them (relay only).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import time
from typing import TYPE_CHECKING, Any

from srltcp.core.protocol.messages import MessageType, decode_payload, encode_payload
from srltcp.utils.logging import get_logger

if TYPE_CHECKING:
    from srltcp.core.messaging.backend import MessagingBackend

log = get_logger(__name__)

PUNCH_WINDOW = 6.0  # seconds both sides keep dialing
PUNCH_ATTEMPT_TIMEOUT = 1.5
PUNCH_RETRY_EVERY = 120.0  # while on the relay
PUNCH_BACKOFF = 300.0  # after a failed round
MAX_CANDIDATES = 6


def acceptable_candidate(host: str, port: object) -> bool:
    """An address a contact may ask us to dial: an IP literal, not loopback,
    link-local, multicast, unspecified or reserved; a valid port."""
    if not isinstance(port, int) or not 0 < port < 65536:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not (
        ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified
        or ip.is_reserved
    )


def _clean_candidates(raw: object) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    if not isinstance(raw, list):
        return out
    for item in raw[: MAX_CANDIDATES * 2]:
        if not (isinstance(item, list) and len(item) == 2):
            continue
        host, port = str(item[0])[:64], item[1]
        if acceptable_candidate(host, port) and (host, port) not in out:
            out.append((host, int(port)))
    return out[:MAX_CANDIDATES]


class PunchMixin:
    """Address exchange and hole punching for :class:`MessagingBackend`."""

    def _init_punch(self: MessagingBackend) -> None:
        self._peer_public: dict[str, list[tuple[str, int]]] = {}
        self._punch_next: dict[str, float] = {}
        self._punch_offered: dict[str, float] = {}
        self._punching: set[str] = set()
        self._last_public: list[tuple[str, int]] = []

    # ── Our addresses ──

    def direct_internet_enabled(self: MessagingBackend) -> bool:
        return bool(getattr(self.config, "direct_internet", True))

    def punch_port(self: MessagingBackend) -> int:
        if not self.direct_internet_enabled() or not self.tcp_transport:
            return 0
        explicit = int(getattr(self.config, "punch_port", 0) or 0)
        if explicit:
            return explicit
        return self.config.tcp_port + 3 if self.config.tcp_port else 0

    def local_candidates(self: MessagingBackend) -> list[str]:
        """LAN / VPN addresses (contacts dial these directly)."""
        out = []
        for ip in sorted(self._own_ips()):
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if addr.version == 4 and not addr.is_loopback and not addr.is_link_local:
                out.append(ip)
        return out[:MAX_CANDIDATES]

    def public_candidates(self: MessagingBackend) -> list[tuple[str, int]]:
        """Where contacts can punch through to us: the public address relays
        report for our punch port, and our global IPv6 addresses."""
        port = self.punch_port()
        if not port:
            return []
        out: list[tuple[str, int]] = []
        for conn in self._relay_conns.values():
            if conn.connected and conn.observed and conn.observed not in out:
                out.append(conn.observed)
        from srltcp.utils.network import global_ipv6_addresses

        for ip in global_ipv6_addresses():
            if (ip, port) not in out:
                out.append((ip, port))
        return out[:MAX_CANDIDATES]

    def rendezvous_addresses(self: MessagingBackend) -> dict[str, Any]:
        public = self.public_candidates()
        return {
            "a": self.local_candidates(),
            "u": [list(c) for c in public],
        }

    async def addresses_changed(self: MessagingBackend) -> None:
        """Tell connected contacts where we are now; allow a new punch round."""
        self._punch_next.clear()
        for hash_id, link in list(self._links.items()):
            if link.handshake_complete and self.is_trusted(hash_id):
                await self.send_rendezvous(hash_id)

    async def note_public_address(self: MessagingBackend) -> None:
        current = self.public_candidates()
        if current != self._last_public:
            self._last_public = current
            if current:
                log.info("Public address for direct connections: %s", ", ".join(
                    f"{h}:{p}" for h, p in current))
            await self.addresses_changed()

    def store_peer_addresses(self: MessagingBackend, hash_id: str, data: dict[str, Any]) -> None:
        peer = self.trusted.get(hash_id)
        if not peer:
            return
        raw = data.get("a")
        local = (
            [h for h in (str(x)[:64] for x in raw[:MAX_CANDIDATES]) if acceptable_candidate(h, 1)]
            if isinstance(raw, list)
            else []
        )
        if local:
            merged = list(dict.fromkeys([*local, peer.tcp_host, *peer.alt_hosts]))
            merged = [h for h in merged if h and h != peer.tcp_host][:4]
            if merged != peer.alt_hosts:
                self.trusted.update(hash_id, alt_hosts=merged)
        if "u" in data:
            self._peer_public[hash_id] = _clean_candidates(data.get("u"))

    # ── Punching ──

    async def start_punch(self: MessagingBackend, hash_id: str) -> None:
        """Offer a punch round (only the lower identity hash offers)."""
        now = time.monotonic()
        if now < self._punch_next.get(hash_id, 0.0) or hash_id in self._punching:
            return
        mine = self.public_candidates()
        if not mine or not self._peer_public.get(hash_id):
            return
        self._punch_next[hash_id] = now + PUNCH_RETRY_EVERY
        self._punch_offered[hash_id] = now
        await self._send_punch(hash_id, "o", mine)

    async def _send_punch(
        self: MessagingBackend, hash_id: str, phase: str, cands: list[tuple[str, int]]
    ) -> None:
        link = self.get_link(hash_id)
        if not link or not link.handshake_complete:
            return
        body = encode_payload({"ph": phase, "c": [list(c) for c in cands]})
        with contextlib.suppress(KeyError, OSError, RuntimeError, ConnectionError):
            packet = self._seal(link, MessageType.PUNCH, body)
            await self._send_raw(link.transport_peer_id, link.transport, packet)

    async def _handle_punch(self: MessagingBackend, hash_id: str, body: bytes) -> None:
        if not self.is_trusted(hash_id):
            return
        data = decode_payload(body)
        phase = data.get("ph")
        theirs = _clean_candidates(data.get("c"))
        link = self.get_link(hash_id)
        if not link or link.transport != "relay":
            return  # already direct (or no session): nothing to punch
        if phase == "o":
            if self._me_hash() < hash_id:
                return  # offers come from the lower hash only
            mine = self.public_candidates()
            await self._send_punch(hash_id, "a", mine)
            if mine and theirs:
                asyncio.create_task(self._punch(hash_id, theirs, leader=False))
        elif phase == "a":
            offered = self._punch_offered.pop(hash_id, 0.0)
            if theirs and time.monotonic() - offered < 15.0:
                asyncio.create_task(self._punch(hash_id, theirs, leader=True))
            elif not theirs:
                self._punch_next[hash_id] = time.monotonic() + PUNCH_BACKOFF

    async def _punch(
        self: MessagingBackend, hash_id: str, targets: list[tuple[str, int]], *, leader: bool
    ) -> None:
        if hash_id in self._punching or not self.tcp_transport:
            return
        port = self.punch_port()
        if not port:
            return
        self._punching.add(hash_id)
        try:
            peer_id = await self._race_dials(targets, port)
            if not peer_id:
                log.info("No direct path through the NATs to %s — staying on the relay",
                         hash_id[:8])
                self._punch_next[hash_id] = time.monotonic() + PUNCH_BACKOFF
                return
            log.info("Punched a direct connection to %s", hash_id[:8])
            if not leader:
                # The leader opens the session on this connection; take over
                # only if nothing arrives (e.g. our dial reached its listener).
                await asyncio.sleep(2.0)
                link = self.get_link_by_peer_id(peer_id)
                if link and link.handshake_complete:
                    return
            if hash_id in self._upgrades:
                return
            kx = await self._send_upgrade_handshake(hash_id, peer_id)
            if kx is None:
                await self.tcp_transport.disconnect(peer_id)
                return
            self._upgrades[hash_id] = (peer_id, kx, time.monotonic())
        finally:
            self._punching.discard(hash_id)

    async def _race_dials(
        self: MessagingBackend, targets: list[tuple[str, int]], port: int
    ) -> str | None:
        assert self.tcp_transport is not None
        transport = self.tcp_transport
        deadline = time.monotonic() + PUNCH_WINDOW

        async def dial(host: str, rport: int) -> str | None:
            while time.monotonic() < deadline:
                try:
                    return await transport.connect_from(
                        host, rport, port, timeout=PUNCH_ATTEMPT_TIMEOUT
                    )
                except (OSError, TimeoutError, ConnectionError):
                    await asyncio.sleep(0.25)
            return None

        tasks = [asyncio.create_task(dial(h, p)) for h, p in targets]
        winner: str | None = None
        try:
            pending = set(tasks)
            while pending and winner is None:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task.exception() is None and task.result() and winner is None:
                        winner = task.result()
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(BaseException):
                    extra = await task
                    if extra and extra != winner:
                        await transport.disconnect(extra)
        return winner
