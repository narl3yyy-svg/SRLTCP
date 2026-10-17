"""Getting from Tor (the doorbell) to a direct connection, and staying direct.

Contacts on different networks first meet over Tor, swap their current
addresses inside the end-to-end encrypted session (``RENDEZVOUS``) and then
connect directly: LAN/VPN addresses are dialed, otherwise both punch through
their NATs (``punch.py``). Nothing but this setup ever goes over Tor; there are
no relays and no servers in between.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, Any

from srltcp.core.messaging.connect import SERIAL_HANDSHAKE_TTL
from srltcp.core.protocol.messages import MessageType, decode_payload, encode_payload
from srltcp.utils.logging import get_logger

if TYPE_CHECKING:
    from srltcp.core.messaging.backend import MessagingBackend

log = get_logger(__name__)

UPGRADE_EVERY = 45.0  # try a direct path this often while only Tor connects us
UPGRADE_EVERY_BUSY = 15.0  # …and this often while something waits for it
DIRECT_TIMEOUT = 3.0


class PathsMixin:
    """Address exchange and the move from the Tor doorbell to a direct path."""

    def _init_paths(self: MessagingBackend) -> None:
        self._upgrade_attempts: dict[str, float] = {}
        self._upgrades: dict[str, tuple[str, Any, float]] = {}  # hash -> (tcp peer_id, kx, started)

    def _me_hash(self: MessagingBackend) -> str:
        return self.identities["tcp"].hash_id

    # ── Rendezvous: our current addresses, inside the E2EE session ──

    async def send_rendezvous(self: MessagingBackend, hash_id: str) -> None:
        peer = self.trusted.get(hash_id)
        link = self.get_link(hash_id)
        if not peer or peer.blocked or not link or not link.handshake_complete:
            return
        # "h": we have theirs already; when it's missing, they answer with
        # theirs (ours may have arrived before they had added us as a contact).
        body = encode_payload(
            {"v": 2, "h": link.have_their_addresses, **self.rendezvous_addresses()}
        )
        with contextlib.suppress(KeyError, OSError, RuntimeError, ConnectionError):
            packet = self._seal(link, MessageType.RENDEZVOUS, body)
            await self._send_raw(link.transport_peer_id, link.transport, packet)

    async def _handle_rendezvous(self: MessagingBackend, hash_id: str, body: bytes) -> None:
        peer = self.trusted.get(hash_id)
        if not peer or peer.blocked:
            return
        data = decode_payload(body)
        self.store_peer_addresses(hash_id, data)  # current LAN/VPN/public addresses
        self.store_peer_onion(hash_id, data)  # their Tor onion address, if any
        link = self.get_link(hash_id)
        if link is None:
            return
        link.have_their_addresses = True
        if not data.get("h"):
            await self.send_rendezvous(hash_id)  # they don't have ours yet (once: ours says "h")
        if hash_id not in self._upgrade_attempts and (
            link.transport == "serial" or self._link_on_tor(link)
        ):
            self.check_paths_now(hash_id)  # their new addresses: a better path now?

    # ── Path switching ──

    def _direct_candidates(self: MessagingBackend, hash_id: str) -> list[tuple[str, int]]:
        peer = self.trusted.get(hash_id)
        if not peer:
            return []
        out: list[tuple[str, int]] = []
        found = self.discovery.get(hash_id, "tcp")
        for host, port in (
            ((found.tcp_host, found.tcp_port) if found else ("", 0)),
            (peer.tcp_host, peer.tcp_port),
            *((h, peer.tcp_port) for h in peer.alt_hosts),
        ):
            if host and (host, port or self.config.tcp_port) not in out:
                out.append((host, port or self.config.tcp_port))
        return out[:4]

    async def maintain_paths(self: MessagingBackend) -> None:
        """Periodically: a session that only Tor carries → try the direct path."""
        now = time.monotonic()
        for hash_id, (peer_id, _kx, started) in list(self._upgrades.items()):
            on_radio = bool(self.serial_transport and self.serial_transport.has_peer(peer_id))
            if now - started > (SERIAL_HANDSHAKE_TTL if on_radio else 10.0):
                self._upgrades.pop(hash_id, None)
                if not on_radio and self.tcp_transport and not self.get_link_by_peer_id(peer_id):
                    await self.tcp_transport.disconnect(peer_id)
        for hash_id, link in list(self._links.items()):
            # Tor only rings the doorbell: go direct. On the radio: move to
            # the same Wi-Fi/LAN when we share one (never to the internet).
            on_radio = link.transport == "serial"
            if not link.up or not (on_radio or self._link_on_tor(link)):
                continue
            # One side drives upgrades so both don't race each other.
            if self._me_hash() > hash_id or hash_id in self._upgrades:
                continue
            busy = self.has_active_transfer_for(hash_id) or self._has_waiting(hash_id)
            every = UPGRADE_EVERY_BUSY if busy else UPGRADE_EVERY
            if now - self._upgrade_attempts.get(hash_id, 0.0) < every:
                continue
            self._upgrade_attempts[hash_id] = now
            asyncio.create_task(self._try_direct_upgrade(hash_id, lan_only=on_radio))

    def check_paths_now(self: MessagingBackend, hash_id: str | None = None) -> None:
        """Where we or a contact are just changed (new network, they showed up
        on our Wi-Fi, new addresses from them): look for a better path now
        instead of at the next periodic check (up to 45 s later)."""
        if hash_id is None:
            self._upgrade_attempts.clear()
        else:
            self._upgrade_attempts.pop(hash_id, None)
        if not self._running:
            return
        task = asyncio.create_task(self.maintain_paths())
        self.__dict__.setdefault("_path_tasks", set()).add(task)
        task.add_done_callback(self.__dict__["_path_tasks"].discard)

    def _has_waiting(self: MessagingBackend, hash_id: str) -> bool:
        return hash_id in self.peers_with_undelivered() or hash_id in self.peers_with_queued_files()

    def request_direct_upgrade(
        self: MessagingBackend, hash_id: str, *, punch: bool = False
    ) -> None:
        """The contact just showed up over Tor, or something waits for it: try
        the direct path now. ``punch`` also skips the wait after earlier
        failed hole-punching rounds (the network may have changed)."""
        link = self.get_link(hash_id)
        if not link or not link.handshake_complete or not self.tcp_transport:
            return
        if not self._link_on_tor(link):
            return
        if self._me_hash() > hash_id and punch:
            # The other side leads hole punching: ask it to start a round now.
            self.ask_for_punch(hash_id)
            return
        if self._me_hash() > hash_id or hash_id in self._upgrades:
            return
        now = time.monotonic()
        if now - self._upgrade_attempts.get(hash_id, 0.0) < 5.0:
            return
        self._upgrade_attempts[hash_id] = now
        if punch:
            self._punch_next.pop(hash_id, None)
        asyncio.create_task(self._try_direct_upgrade(hash_id))

    async def _try_direct_upgrade(
        self: MessagingBackend, hash_id: str, *, lan_only: bool = False
    ) -> None:
        """Dial the contact's LAN/VPN addresses, else punch through the NATs
        (``lan_only``: only addresses on a network we're on ourselves)."""
        if not self.tcp_transport:
            return
        candidates = self._direct_candidates(hash_id)
        if lan_only:
            from srltcp.utils.network import same_network

            candidates = [(h, p) for h, p in candidates if same_network(h)]
        if candidates:
            peer_id, host, port = await self._dial_first(candidates, DIRECT_TIMEOUT)
            if peer_id:
                kx = await self._send_upgrade_handshake(hash_id, peer_id)
                if kx is None:
                    await self.tcp_transport.disconnect(peer_id)
                    return
                log.info("Trying a direct path to %s at %s:%d", hash_id[:8], host, port)
                return
        if not lan_only:
            await self.start_punch(hash_id)  # no LAN/VPN path: through the NATs between us

    def upgrade_for_peer_id(self: MessagingBackend, peer_id: str) -> tuple[str, Any] | None:
        for hash_id, (pid, kx, _started) in self._upgrades.items():
            if pid == peer_id:
                return hash_id, kx
        return None
