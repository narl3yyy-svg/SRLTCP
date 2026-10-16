"""Direct connections across networks without port forwarding (hole punching).

Two devices behind ordinary routers or carrier NAT (or IPv6 firewalls) can't
accept incoming connections, but each NAT lets an *outgoing* flow's replies
back in. If both send to each other at the same moment, each NAT sees the
other's packets as replies to its own and lets them through: a direct
connection, no server in the path and nothing to configure on either side.

How the two agree on "the same moment" and where to send, privately:

1. Each phone asks a public STUN server which public address and port its UDP
   socket has (IPv6 needs no STUN: the phone's own global address).
2. While the contacts have a session over Tor (the doorbell), they swap those
   addresses inside the E2EE session: ``RENDEZVOUS`` carries LAN/VPN
   addresses (for plain direct dials) and the public ones; ``PUNCH``
   offer/answer starts a round (with fresh keys for the UDP connection).
3. Both send to each other's candidates for a few seconds (IPv4 and IPv6 at
   once); the first authenticated packet fixes the path, and the normal
   signed handshake inside it replaces the Tor session.

What it can't do: carrier NAT that gives a new random port for every
destination ("symmetric") on *both* sides can't be crossed by any direct
technique; then the phones need IPv6, or one of them on Wi-Fi. *Check
connection* says when that's the case. Addresses are shared only with trusted
contacts, inside the encrypted session; turning off *direct internet
connections* keeps your public IP away from them.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import re
import secrets
import time
from typing import TYPE_CHECKING, Any

from srltcp.core.protocol.messages import MessageType, decode_payload, encode_payload
from srltcp.transports.udp import (
    STUN_SERVERS,
    UdpConnection,
    UdpEndpoint,
    combine_secrets,
    derive_keys,
    parse_stun_servers,
)
from srltcp.utils.logging import get_logger

if TYPE_CHECKING:
    from srltcp.core.messaging.backend import MessagingBackend

log = get_logger(__name__)

PUNCH_WINDOW = 8.0  # seconds both sides keep dialing
UDP_PUNCH_WINDOW = 4.0  # seconds both sides keep probing over UDP (it works within 1-2 s or not)
UDP_RETRY_SOON = 4.0  # after a failed UDP round with a lower priming hop limit left
STUN_FRESH = 20.0  # re-ask STUN before a round if the answer is older (carrier NATs forget)
PUNCH_ATTEMPT_TIMEOUT = 1.5
PUNCH_FOLLOWER_DELAY = 1.0  # the answering side starts dialing a moment later
PUNCH_ANSWER_EVERY = 6.0  # a contact can start at most one round this often (each is small)
PUNCH_RETRY_EVERY = 120.0  # while only Tor connects us
PUNCH_BACKOFF = (15.0, 30.0, 60.0, 120.0)  # after 1, 2, 3, 4+ failed rounds
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


_NOT_PUNCHABLE = tuple(
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",  # home/office LANs
        "100.64.0.0/10",  # carrier NAT space (and Tailscale-style VPNs)
        "0.0.0.0/8", "192.0.0.0/24", "198.18.0.0/15",  # special use, benchmarking
        "fc00::/7", "fec0::/10", "::/8",  # unique/site local
        "2002::/16", "2001::/32",  # 6to4 and Teredo: can wrap a LAN IPv4 address
        "64:ff9b::/96", "64:ff9b:1::/48",  # NAT64: same
    )
)


def punchable_candidate(host: str, port: object) -> bool:
    """A public address a contact may ask us to punch towards. LAN and
    carrier-internal ranges are refused, so a contact can't make the phone
    knock on devices in our own network (those are reached by a normal
    dial to the addresses in RENDEZVOUS "a", on the peer port only)."""
    if not acceptable_candidate(host, port):
        return False
    ip = ipaddress.ip_address(host)
    if ip.version == 6 and ip.ipv4_mapped:
        return False
    if any(ip in net for net in _NOT_PUNCHABLE if net.version == ip.version):
        return False
    from srltcp.utils.network import same_network

    # Not this device or anything on our own LAN/VPN (incl. our IPv6 prefix).
    return not same_network(host)


def _public_address(host: str, port: int) -> bool:
    """Our own STUN-reported address is worth sharing (not LAN/carrier space)."""
    if not acceptable_candidate(host, port):
        return False
    ip = ipaddress.ip_address(host)
    return not any(ip in net for net in _NOT_PUNCHABLE if net.version == ip.version)


def _clean_candidates(raw: object) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    if not isinstance(raw, list):
        return out
    for item in raw[: MAX_CANDIDATES * 2]:
        if not (isinstance(item, list) and len(item) == 2):
            continue
        host, port = str(item[0])[:64], item[1]
        if punchable_candidate(host, port) and (host, port) not in out:
            out.append((host, int(port)))
    return out[:MAX_CANDIDATES]


_HEX = re.compile(r"[0-9a-f]+")


def _hex(value: object, size: int) -> bytes | None:
    """A hex field of exactly ``size`` bytes from a contact's message, else None."""
    if not isinstance(value, str) or len(value) != 2 * size or not _HEX.fullmatch(value):
        return None
    return bytes.fromhex(value)


def _same_family(
    theirs: list[tuple[str, int]], mine: list[tuple[str, int]]
) -> list[tuple[str, int]]:
    """Their candidates we can dial: an IPv6-only phone can't reach an IPv4
    address (and vice versa), so a round without a shared family is skipped."""
    families = {":" in host for host, _port in mine}
    return [c for c in theirs if (":" in c[0]) in families]


class PunchMixin:
    """Address exchange and hole punching for :class:`MessagingBackend`."""

    def _init_punch(self: MessagingBackend) -> None:
        self._peer_public: dict[str, list[tuple[str, int]]] = {}
        self._punch_next: dict[str, float] = {}
        self._punch_offered: dict[str, float] = {}
        self._punch_answered: dict[str, float] = {}
        self._punching: set[str] = set()
        self._punch_failures: dict[str, int] = {}
        self._last_public: list[tuple[str, int]] = []
        self.udp: UdpEndpoint | None = None
        self._peer_udp: dict[str, list[tuple[str, int]]] = {}
        self._udp_offers: dict[str, tuple[bytes, bytes, float]] = {}  # our secret, id, when
        self._prime_max: dict[str, int] = {}  # highest priming hop limit per contact
        self._punch_asked: dict[str, float] = {}
        # Recent punch rounds per contact, for the connection report:
        # (when, IPv4 tried, IPv6 tried, outcome). No addresses.
        self._punch_log: dict[str, list[tuple[float, bool, bool, str]]] = {}

    def _prime_hops(self: MessagingBackend, hash_id: str) -> tuple[int, ...]:
        return tuple(range(2, self._prime_max.get(hash_id, 4) + 1))

    # ── UDP (the fast path between networks, see transports/udp.py) ──

    async def start_udp(self: MessagingBackend) -> None:
        if self.udp is not None or not self.tcp_transport:
            return
        port = self.config.tcp_port + 3 if self.config.tcp_port else 0
        try:
            servers = parse_stun_servers(getattr(self.config, "stun_servers", ""))
        except ValueError:
            servers = STUN_SERVERS
        endpoint = UdpEndpoint(port, stun_servers=servers)
        try:
            await endpoint.start()
        except OSError as exc:
            log.warning("Direct UDP connections unavailable: %s", exc)
            return
        if not endpoint.families:
            return
        self.udp = endpoint
        if self.direct_internet_enabled():
            task = asyncio.create_task(self._refresh_udp())
            self.__dict__.setdefault("_udp_tasks", set()).add(task)
            task.add_done_callback(self.__dict__["_udp_tasks"].discard)

    def stop_udp(self: MessagingBackend) -> None:
        if self.udp is not None:
            self.udp.stop()
            self.udp = None

    async def _refresh_udp(self: MessagingBackend, *, if_older: float = 0.0) -> None:
        """Ask STUN for our public UDP address (only with direct internet
        connections on: the STUN server sees our IP address)."""
        udp = self.udp
        if udp is None or not self.direct_internet_enabled():
            return
        if if_older and time.monotonic() - udp.mapped_at < if_older:
            return
        before = self.udp_candidates()
        await udp.refresh_mapping()
        if self.udp_candidates() != before:
            log.info("UDP address for direct connections: %s", ", ".join(
                f"{h}:{p}" for h, p in self.udp_candidates()) or "none")
            await self.addresses_changed()  # contacts learn it (inside the session)

    def udp_candidates(self: MessagingBackend) -> list[tuple[str, int]]:
        """Where contacts can reach our UDP socket: STUN's answer per address
        family, and our global IPv6 addresses (no NAT on IPv6)."""
        udp = self.udp
        if udp is None or not self.direct_internet_enabled():
            return []
        import socket

        from srltcp.utils.network import global_ipv6_addresses

        out: list[tuple[str, int]] = []
        mapped4 = udp.mapped.get(socket.AF_INET)
        if mapped4 and _public_address(*mapped4):
            out.append(mapped4)
        # IPv6 has no NAT: our own global addresses, never one built from the
        # hardware (MAC) address — global_ipv6_addresses() leaves those out
        # (so a STUN answer naming one isn't passed on either).
        v6_port = udp.ports.get(socket.AF_INET6)
        if v6_port:
            for ip in global_ipv6_addresses():
                if (ip, v6_port) not in out:
                    out.append((ip, v6_port))
        return out[:MAX_CANDIDATES]

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
        """Where contacts can punch TCP through to us: our global IPv6
        addresses (TCP over IPv4 NAT isn't tried; UDP covers it)."""
        port = self.punch_port()
        if not port:
            return []
        out: list[tuple[str, int]] = []
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
            "w": [list(c) for c in self.udp_candidates()],
            # Our onion address (Tor) — "" tells them Tor is off for us.
            "o": self._tor_onion if self.tor_ready() else "",
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
            public = _clean_candidates(data.get("u"))
            if public and public != self._peer_public.get(hash_id):
                # New place to reach them: let the next path check try at once.
                self._upgrade_attempts.pop(hash_id, None)
                self._punch_next.pop(hash_id, None)
            self._peer_public[hash_id] = public
        if "w" in data:
            udp = _clean_candidates(data.get("w"))
            if udp and udp != self._peer_udp.get(hash_id):
                self._upgrade_attempts.pop(hash_id, None)
                self._punch_next.pop(hash_id, None)
            self._peer_udp[hash_id] = udp

    # ── Punching ──

    async def start_punch(self: MessagingBackend, hash_id: str) -> None:
        """Offer a punch round (only the lower identity hash offers)."""
        now = time.monotonic()
        if now < self._punch_next.get(hash_id, 0.0) or hash_id in self._punching:
            return
        self._punch_next[hash_id] = now + PUNCH_RETRY_EVERY
        await self._refresh_udp(if_older=STUN_FRESH)
        mine = self.public_candidates()
        mine_udp = self.udp_candidates()
        tcp_ok = bool(mine) and bool(_same_family(self._peer_public.get(hash_id, []), mine))
        theirs_udp = self._peer_udp.get(hash_id, [])
        # Offer UDP whenever we have an address: the answer brings theirs (it may
        # have changed since their last RENDEZVOUS). Older versions ignore it.
        udp_ok = bool(mine_udp) and (not theirs_udp or bool(_same_family(theirs_udp, mine_udp)))
        if not tcp_ok and not udp_ok:
            return
        self._punch_offered[hash_id] = now
        udp_fields: dict[str, Any] = {}
        if udp_ok:
            secret, my_id = secrets.token_bytes(32), secrets.token_bytes(8)
            self._udp_offers[hash_id] = (secret, my_id, now)
            udp_fields = {"w": [list(c) for c in mine_udp], "k": secret.hex(), "i": my_id.hex()}
        await self._send_punch(hash_id, "o", mine, udp_fields)

    def ask_for_punch(self: MessagingBackend, hash_id: str) -> None:
        """We have something waiting for the direct path but the contact
        leads punching (lower identity hash): ask it to start a round now
        (at most every few seconds)."""
        now = time.monotonic()
        if now - self._punch_asked.get(hash_id, -PUNCH_ANSWER_EVERY) < PUNCH_ANSWER_EVERY:
            return
        self._punch_asked[hash_id] = now
        task = asyncio.ensure_future(self._send_punch(hash_id, "r", []))
        self.__dict__.setdefault("_udp_tasks", set()).add(task)
        task.add_done_callback(self.__dict__["_udp_tasks"].discard)

    async def _send_punch(
        self: MessagingBackend,
        hash_id: str,
        phase: str,
        cands: list[tuple[str, int]],
        extra: dict[str, Any] | None = None,
    ) -> None:
        link = self.get_link(hash_id)
        if not link or not link.handshake_complete:
            return
        body = encode_payload({"ph": phase, "c": [list(c) for c in cands], **(extra or {})})
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
        if not link or not self._link_on_tor(link):
            return  # already direct (or no session): nothing to punch
        if phase == "r":
            if self._me_hash() < hash_id:  # we lead: start a round (rate-limited there)
                self.request_direct_upgrade(hash_id, punch=True)
            return
        if phase == "o":
            if self._me_hash() < hash_id:
                return  # offers come from the lower hash only
            now = time.monotonic()
            if now - self._punch_answered.get(hash_id, -PUNCH_ANSWER_EVERY) < PUNCH_ANSWER_EVERY:
                return  # rounds are rare; a flood of offers gets no extra dialing
            self._punch_answered[hash_id] = now
            mine = self.public_candidates()
            theirs = _same_family(theirs, mine)
            conn, udp_targets, udp_fields = await self._udp_answer(hash_id, data)
            if mine and theirs:
                # Open our router towards them *before* answering: the leader
                # dials as soon as the answer arrives.
                await self._prime(theirs)
            await self._send_punch(hash_id, "a", mine, udp_fields)
            if (mine and theirs) or conn:
                asyncio.create_task(self._punch(
                    hash_id, theirs if mine else [], leader=False,
                    udp_conn=conn, udp_targets=udp_targets,
                ))
        elif phase == "a":
            offered = self._punch_offered.pop(hash_id, 0.0)
            theirs = _same_family(theirs, self.public_candidates())
            conn, udp_targets = self._udp_lead(hash_id, data)
            if (theirs or conn) and time.monotonic() - offered < 15.0:
                asyncio.create_task(self._punch(
                    hash_id, theirs, leader=True, udp_conn=conn, udp_targets=udp_targets,
                ))
            else:
                if conn:
                    conn._shutdown()
                if not theirs:
                    self._punch_next[hash_id] = time.monotonic() + PUNCH_BACKOFF[-1]

    async def _udp_answer(
        self: MessagingBackend, hash_id: str, data: dict[str, Any]
    ) -> tuple[UdpConnection | None, list[tuple[str, int]], dict[str, Any]]:
        """Follower: their offer carries UDP candidates, a key share and an
        id; make our connection (it can take their probes at once) and the
        answer's UDP part."""
        theirs = [c for c in _clean_candidates(data.get("w")) if punchable_candidate(*c)]
        their_key, their_id = _hex(data.get("k"), 32), _hex(data.get("i"), 8)
        if not theirs or their_key is None or their_id is None or self.udp is None:
            return None, [], {}
        await self._refresh_udp(if_older=STUN_FRESH)
        mine = self.udp_candidates()
        targets = _same_family(theirs, mine)
        if not mine or not targets:
            return None, [], {}
        secret, my_id = secrets.token_bytes(32), secrets.token_bytes(8)
        # Keys: leader->follower and follower->leader; they lead (they offered).
        l2f, f2l = derive_keys(combine_secrets(their_key, secret), hash_id, self._me_hash())
        conn = self.udp.new_connection(their_id, send_key=f2l, recv_key=l2f, my_id=my_id)
        if conn is None:
            return None, [], {}
        conn.expected_hash = hash_id
        # Before answering: the leader probes as soon as it hears.
        self.udp.prime(targets, self._prime_hops(hash_id), conn.socks)
        return conn, targets, {"w": [list(c) for c in mine], "k": secret.hex(), "i": my_id.hex()}

    def _udp_lead(
        self: MessagingBackend, hash_id: str, data: dict[str, Any]
    ) -> tuple[UdpConnection | None, list[tuple[str, int]]]:
        """Leader: the answer to our offer completes the UDP keys."""
        offer = self._udp_offers.pop(hash_id, None)
        if offer is None or self.udp is None:
            return None, []
        secret, my_id, when = offer
        theirs = [c for c in _clean_candidates(data.get("w")) if punchable_candidate(*c)]
        their_key, their_id = _hex(data.get("k"), 32), _hex(data.get("i"), 8)
        targets = _same_family(theirs, self.udp_candidates())
        if not targets or their_key is None or their_id is None or time.monotonic() - when > 15:
            return None, []
        l2f, f2l = derive_keys(combine_secrets(secret, their_key), self._me_hash(), hash_id)
        conn = self.udp.new_connection(their_id, send_key=l2f, recv_key=f2l, my_id=my_id)
        if conn is None:
            return None, []
        conn.expected_hash = hash_id
        return conn, targets

    async def _punch(
        self: MessagingBackend,
        hash_id: str,
        targets: list[tuple[str, int]],
        *,
        leader: bool,
        udp_conn: UdpConnection | None = None,
        udp_targets: list[tuple[str, int]] | None = None,
    ) -> None:
        port = self.punch_port()
        if hash_id in self._punching or not self.tcp_transport or not port:
            if udp_conn:
                udp_conn._shutdown()
            return
        self._punching.add(hash_id)
        retry_soon = False
        try:
            peer_id: str | None = None
            if udp_conn:
                # UDP first: it gets through carrier NATs far more often than TCP.
                if self.udp and udp_targets and await self.udp.punch(
                    udp_conn, udp_targets, UDP_PUNCH_WINDOW,
                    delay=0.0 if leader else PUNCH_FOLLOWER_DELAY,
                    hops=self._prime_hops(hash_id),
                ):
                    peer_id = await self.tcp_transport.adopt_udp(udp_conn)
                    log.info("Direct UDP connection to %s via %s", hash_id[:8], udp_conn.address)
                    self._note_round(hash_id, udp_targets or [], "direct")
                else:
                    udp_conn._shutdown()
                    self._note_round(hash_id, udp_targets or [], "no answer")
                    # Maybe our priming packets reached their router (a short
                    # path, e.g. the same ISP): try again soon, lower hop limit,
                    # from a fresh port (routers hold stale state for the old
                    # pair of ports for a while; every retry would refresh it).
                    current = self._prime_max.get(hash_id, 4)
                    if current > 2:
                        self._prime_max[hash_id] = current - 1
                        retry_soon = True
                        if self.udp:
                            await self.udp.rotate()
            if not peer_id and targets and not retry_soon:
                # (Not while a UDP retry is coming: it nearly always works
                # where TCP would, and the TCP round only delays it.)
                peer_id = await self._race_dials(
                    targets, port, delay=0.0 if leader else PUNCH_FOLLOWER_DELAY
                )
            if not peer_id and retry_soon:
                log.info("No direct UDP path to %s yet — trying again in %d s",
                         hash_id[:8], int(UDP_RETRY_SOON))
                self._punch_next[hash_id] = time.monotonic() + UDP_RETRY_SOON - 1
                asyncio.get_running_loop().call_later(
                    UDP_RETRY_SOON, lambda: self.request_direct_upgrade(hash_id, punch=True)
                )
                return
            if not peer_id:
                fails = self._punch_failures.get(hash_id, 0)
                self._punch_failures[hash_id] = fails + 1
                wait = PUNCH_BACKOFF[min(fails, len(PUNCH_BACKOFF) - 1)]
                log.info("No direct path through the NATs to %s yet "
                         "(next try in %d s)", hash_id[:8], wait)
                self._punch_next[hash_id] = time.monotonic() + wait
                return
            self._punch_failures.pop(hash_id, None)
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

    def _note_round(
        self: MessagingBackend, hash_id: str, targets: list[tuple[str, int]], outcome: str
    ) -> None:
        log_ = self._punch_log.setdefault(hash_id, [])
        log_.append((
            time.time(), any(":" not in h for h, _p in targets), any(":" in h for h, _p in targets),
            outcome,
        ))
        del log_[:-8]

    async def _prime(self: MessagingBackend, targets: list[tuple[str, int]]) -> None:
        port = self.punch_port()
        if not port or not self.tcp_transport:
            return
        for host, rport in targets:
            await self.tcp_transport.prime_from(host, rport, port)

    async def _race_dials(
        self: MessagingBackend, targets: list[tuple[str, int]], port: int, *, delay: float = 0.0
    ) -> str | None:
        """Keep dialing every candidate until one connects or the window closes.

        The answering side first opens its own router towards the leader
        (low-TTL SYNs that don't travel far) and waits ``delay`` before its
        first real SYN, so that SYN never reaches the leader's router before
        the leader has dialed (some routers answer unexpected SYNs with a
        reset, which spoils the mapping); the leader's SYNs pass the primed
        mapping meanwhile.
        """
        assert self.tcp_transport is not None
        transport = self.tcp_transport
        if delay:
            await self._prime(targets)  # again, in case the first expired
            await asyncio.sleep(delay)
        deadline = time.monotonic() + PUNCH_WINDOW

        async def dial(host: str, rport: int) -> str | None:
            while time.monotonic() < deadline:
                try:
                    return await transport.connect_from(
                        host, rport, port, timeout=PUNCH_ATTEMPT_TIMEOUT
                    )
                except (OSError, TimeoutError, ConnectionError) as exc:
                    log.debug("Punch dial %s:%s: %r", host, rport, exc)
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

    # ── "Check connection" (Edit contact) ──

    def _speed_advice(self: MessagingBackend, hash_id: str, path: str) -> dict[str, Any]:
        """Why a contact on another network isn't connected directly yet, in
        plain words. Across networks the devices
        connect directly over UDP (IPv4 through carrier NAT, or IPv6); each
        needs a public address (STUN, or IPv6) to give the other."""
        mine = self.udp_candidates() + self.public_candidates()
        theirs = self._peer_udp.get(hash_id, []) + self._peer_public.get(hash_id, [])
        failed = self._punch_failures.get(hash_id, 0)
        symmetric = bool(self.udp and self.udp.symmetric)
        tips: list[str] = []
        if path in ("direct", "direct (udp)"):
            reason = "Direct connection: full speed."
        elif not self.direct_internet_enabled():
            reason = ("Direct internet connections are off on this device "
                      "(Settings → Network → Try direct internet connections).")
        elif not mine:
            reason = ("This device couldn't find its public address (the STUN servers didn't "
                      "answer: blocked on this network?), so it can't connect directly.")
        elif not theirs:
            reason = ("Your contact's device hasn't shared a public address (an older version, "
                      "direct connections turned off, or STUN blocked on their network).")
        elif failed:
            reason = ("Both devices have public addresses, but the networks between you block "
                      f"direct connections ({failed} tr{'y' if failed == 1 else 'ies'}).")
            if symmetric:
                reason += (" This network gives a different port for every destination "
                           "(\"symmetric\" carrier NAT), which direct connections can't cross.")
            tips.append("Wi-Fi on either side, or IPv6 on mobile data (Settings → Network → "
                        "SIM → Access Point Names → APN protocol: IPv4/IPv6), often works.")
        else:
            reason = "Both devices have public addresses: a direct connection is being tried."
        if path == "tor" and failed:
            tips.append("Turn on Wi-Fi on one of the phones (any home or public Wi-Fi that "
                        "allows normal internet use): most Wi-Fi routers can be crossed.")
        return {
            "my_ipv6": any(":" in h for h, _p in mine),
            "their_ipv6": any(":" in h for h, _p in theirs),
            "my_public": bool(mine), "their_public": bool(theirs),
            "symmetric_nat": symmetric, "direct_tries_failed": failed,
            "reason": reason, "tips": tips,
        }

    def _network_blockers(
        self: MessagingBackend, hash_id: str, results: list[dict[str, Any]]
    ) -> list[str]:
        """What this network does that stops a direct connection, in plain
        words (from what the app already knows; nothing extra is sent)."""
        import socket

        from srltcp.utils.network import same_network

        out: list[str] = []
        isolated = [
            r for r in results
            if not r["ok"] and same_network(r["address"].rsplit(":", 1)[0])
            and "isn't listening" not in r["detail"]
        ]
        if isolated:
            out.append("You're both on this Wi-Fi, but it keeps devices apart (common on plane, "
                       "hotel, café and guest Wi-Fi), so the phones can't talk over it directly.")
        mapped = self.udp.mapped.get(socket.AF_INET) if self.udp else None
        theirs = self._peer_udp.get(hash_id, []) + self._peer_public.get(hash_id, [])
        if mapped and any(h == mapped[0] for h, _p in theirs):
            out.append("You both reach the internet through the same public address (the same "
                       "Wi-Fi, satellite link like Starlink, or carrier). Most of these don't "
                       "pass traffic between their own users, so a direct connection can't "
                       "go that way.")
        if self.direct_internet_enabled() and self.udp and not self.udp_candidates():
            out.append("This network blocks the UDP that direct connections use (or the STUN "
                       "servers that find your public address).")
        return out

    def connection_report(self: MessagingBackend, hash_id: str, path: str) -> str:
        """Plain-text summary for troubleshooting a direct connection: what
        kind of NAT this network has, which address families each side
        offers, and how recent attempts ended. No IP addresses or ports."""
        import socket

        from srltcp import __version__

        udp = self.udp
        answers = udp.stun_answers if udp else {}

        def nat(family: int) -> str:
            if not udp or not udp.mapped_at:
                return "not checked yet"
            if not answers.get(family):
                return "no answer (no address of this kind, or blocked)"
            if family == socket.AF_INET6:
                return "yes (no NAT)"
            if answers[family] < 2:
                return "yes (type unknown: only one STUN server answered)"
            return ("yes, hard NAT (a new port for every destination)" if udp.symmetric
                    else "yes, easy NAT (same port for every destination)")

        theirs = self._peer_udp.get(hash_id, [])
        lines = [
            f"SRLTCP {__version__} connection report",
            f"Path now: {path or 'not connected'}",
            f"Direct internet connections: {'on' if self.direct_internet_enabled() else 'off'}",
            f"Tor (doorbell): {self.tor_status().get('state', 'off')}",
            f"This phone, IPv4 public address: {nat(socket.AF_INET)}",
            f"This phone, IPv6: {nat(socket.AF_INET6)}",
            "Contact offers: "
            + (", ".join(
                n for n, ok in (("IPv4", any(':' not in h for h, _p in theirs)),
                                ("IPv6", any(':' in h for h, _p in theirs))) if ok
            ) or "nothing yet (not connected over Tor, or their STUN failed)"),
            f"Failed rounds in a row: {self._punch_failures.get(hash_id, 0)}",
        ]
        now = time.time()
        for when, v4, v6, outcome in self._punch_log.get(hash_id, [])[-5:]:
            fams = "+".join(f for f, ok in (("IPv4", v4), ("IPv6", v6)) if ok) or "-"
            lines.append(f"  {int(now - when)} s ago: tried {fams} -> {outcome}")
        return "\n".join(lines)

    async def diagnose_contact(
        self: MessagingBackend, hash_id: str, *, probe_tor: bool = True
    ) -> dict[str, Any]:
        """Try every address we know for a contact and explain the result in
        plain words. Opens plain TCP connections only (no data is sent)."""
        peer = self.trusted.get(hash_id)
        if not peer:
            return {"error": "not a contact"}
        port = peer.tcp_port or self.config.tcp_port
        hosts = [h for h in dict.fromkeys([peer.tcp_host, *peer.alt_hosts]) if h]

        async def probe(host: str) -> dict[str, Any]:
            started = time.monotonic()
            try:
                _r, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 3.0)
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
                ms = round((time.monotonic() - started) * 1000)
                return {"address": f"{host}:{port}", "ok": True, "detail": f"answered in {ms} ms"}
            except TimeoutError:
                detail = "no answer (blocked, or not on a network you can reach)"
            except ConnectionRefusedError:
                detail = "the device is reachable but SRLTCP isn't listening (app closed?)"
            except OSError as exc:
                detail = "no route to this network" if exc.errno in (101, 113, 10051, 10065) else (
                    exc.strerror or str(exc)
                )
            return {"address": f"{host}:{port}", "ok": False, "detail": detail}

        results = list(await asyncio.gather(*(probe(h) for h in hosts[:6])))
        link = self.get_link(hash_id)
        path = ""
        if link and link.handshake_complete:
            on_tor = self.tcp_transport is not None and self.tcp_transport.is_tor(
                link.transport_peer_id
            )
            path = "tor" if on_tor else "direct"
            if path == "direct" and link.address.startswith("udp:"):
                path = "direct (udp)"
        direct_ok = any(r["ok"] for r in results)
        tor_ok, tor_problem = (path == "tor"), self.tor_problem(hash_id)
        if not direct_ok and not tor_ok and not tor_problem and probe_tor:
            try:  # a real test over Tor (only when you press Check connection)
                tor_peer = await self.dial_onion(hash_id)
                tor_ok = True
                if self.tcp_transport:
                    await self.tcp_transport.disconnect(tor_peer)
            except (OSError, TimeoutError, ConnectionError, asyncio.IncompleteReadError):
                tor_problem = ("their Tor address didn't answer: their app may be closed, or "
                               "Tor is still connecting on their side")
        blockers = [] if direct_ok or path.startswith("direct") else self._network_blockers(
            hash_id, results
        )
        if direct_ok or path.startswith("direct"):
            summary = "Connected directly."
        elif tor_ok:
            summary = ("Found over Tor; connecting directly (messages and files wait for the "
                       "direct connection).")
        else:
            summary = "Can't reach this contact right now."
            if tor_problem:
                summary += f" Tor: {tor_problem}."
            if hosts:
                summary += (" If one of you is on a VPN and the other on the home Wi-Fi, the VPN "
                            "must include the home network (WireGuard AllowedIPs).")
        if blockers:
            summary += " " + " ".join(blockers)
        return {
            "hash_id": hash_id,
            "path": path,
            "speed": self._speed_advice(hash_id, path),
            "addresses": results,
            "my_addresses": self.local_candidates(),
            "tor": {**self.tor_status(), "contact_has_address": bool(self.peer_onion(hash_id))},
            "network": blockers,
            "summary": summary,
            "report": self.connection_report(hash_id, path),
        }
