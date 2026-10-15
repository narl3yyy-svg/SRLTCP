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
2. While the contacts already have a session (over Tor or a relay), they
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

PUNCH_WINDOW = 8.0  # seconds both sides keep dialing
PUNCH_ATTEMPT_TIMEOUT = 1.5
PUNCH_FOLLOWER_DELAY = 1.0  # the answering side starts dialing a moment later
PUNCH_ANSWER_EVERY = 30.0  # a contact can start at most one round this often
PUNCH_RETRY_EVERY = 120.0  # while on the relay
PUNCH_BACKOFF = (30.0, 60.0, 120.0, 300.0)  # after 1, 2, 3, 4+ failed rounds
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
        "0.0.0.0/8", "fc00::/7", "::/8",
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
            obs = conn.observed
            if conn.connected and obs and obs not in out and punchable_candidate(*obs):
                out.append(obs)
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

    # ── Punching ──

    async def start_punch(self: MessagingBackend, hash_id: str) -> None:
        """Offer a punch round (only the lower identity hash offers)."""
        now = time.monotonic()
        if now < self._punch_next.get(hash_id, 0.0) or hash_id in self._punching:
            return
        mine = self.public_candidates()
        if not mine or not _same_family(self._peer_public.get(hash_id, []), mine):
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
        if not link or (link.transport != "relay" and not self._link_on_tor(link)):
            return  # already direct (or no session): nothing to punch
        if phase == "o":
            if self._me_hash() < hash_id:
                return  # offers come from the lower hash only
            now = time.monotonic()
            if now - self._punch_answered.get(hash_id, -PUNCH_ANSWER_EVERY) < PUNCH_ANSWER_EVERY:
                return  # rounds are rare; a flood of offers gets no extra dialing
            self._punch_answered[hash_id] = now
            mine = self.public_candidates()
            theirs = _same_family(theirs, mine)
            if mine and theirs:
                # Open our router towards them *before* answering: the leader
                # dials as soon as the answer arrives.
                await self._prime(theirs)
            await self._send_punch(hash_id, "a", mine)
            if mine and theirs:
                asyncio.create_task(self._punch(hash_id, theirs, leader=False))
        elif phase == "a":
            offered = self._punch_offered.pop(hash_id, 0.0)
            theirs = _same_family(theirs, self.public_candidates())
            if theirs and time.monotonic() - offered < 15.0:
                asyncio.create_task(self._punch(hash_id, theirs, leader=True))
            elif not theirs:
                self._punch_next[hash_id] = time.monotonic() + PUNCH_BACKOFF[-1]

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
            peer_id = await self._race_dials(
                targets, port, delay=0.0 if leader else PUNCH_FOLLOWER_DELAY
            )
            if not peer_id:
                fails = self._punch_failures.get(hash_id, 0)
                self._punch_failures[hash_id] = fails + 1
                wait = PUNCH_BACKOFF[min(fails, len(PUNCH_BACKOFF) - 1)]
                log.info("No direct path through the NATs to %s — staying on the relay or "
                         "Tor (next try in %d s)", hash_id[:8], wait)
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
        """Why a contact on another network is on Tor/the relay rather than
        direct (the fast path), in plain words. Across mobile networks the
        phones can only connect directly over IPv6: carrier NAT on IPv4 lets
        nothing in."""
        mine6 = any(":" in h for h, _p in self.public_candidates())
        theirs6 = any(":" in h for h, _p in self._peer_public.get(hash_id, []))
        failed = self._punch_failures.get(hash_id, 0)
        tips = []
        if path == "direct":
            reason = "Direct connection: full speed."
        elif not self.direct_internet_enabled():
            reason = ("Direct internet connections are off on this device "
                      "(Settings → Network → Try direct internet connections).")
        elif not mine6:
            reason = ("This device's network has no IPv6, so it can't connect directly to a "
                      "phone on another network.")
            tips.append("On mobile data: Settings → Network → SIM → Access Point Names → your "
                        "APN → APN protocol: IPv4/IPv6 (some carriers and MVNOs default to IPv4 "
                        "only).")
        elif not theirs6:
            reason = ("Your contact's network has no IPv6 (or they turned direct connections "
                      "off), so the phones can't connect directly.")
            tips.append("On their phone: APN protocol IPv4/IPv6 (Settings → Network → SIM → "
                        "Access Point Names).")
        elif failed:
            reason = ("Both devices have IPv6, but a carrier's firewall blocked the direct "
                      f"connection ({failed} tr{'y' if failed == 1 else 'ies'}).")
        else:
            reason = "Both devices have IPv6: a direct connection is being tried."
        if path in ("tor", "relay"):
            tips.append("Works on any network at full speed: run your own relay "
                        "(Settings → Network → Internet relay) or put both phones on the same "
                        "VPN (WireGuard, or Tailscale which works through carrier NAT).")
        return {
            "my_ipv6": mine6, "their_ipv6": theirs6, "direct_tries_failed": failed,
            "tor_circuits": self.tor_circuits(hash_id), "reason": reason, "tips": tips,
        }

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
            path = "relay" if link.transport == "relay" else ("tor" if on_tor else "direct")
        relay_ok = self.relay_available_for(hash_id)
        relay_known = self.pair_relay(hash_id) is not None
        direct_ok = any(r["ok"] for r in results)
        tor_ok, tor_problem = (path == "tor"), self.tor_problem(hash_id)
        if not direct_ok and not relay_ok and not tor_ok and not tor_problem and probe_tor:
            try:  # a real test over Tor (only when you press Check connection)
                tor_peer = await self.dial_onion(hash_id)
                tor_ok = True
                if self.tcp_transport:
                    await self.tcp_transport.disconnect(tor_peer)
            except (OSError, TimeoutError, ConnectionError, asyncio.IncompleteReadError):
                tor_problem = ("their Tor address didn't answer: their app may be closed, or "
                               "Tor is still connecting on their side")
        if direct_ok:
            summary = "Reachable directly — no relay needed."
        elif relay_ok:
            summary = ("Not reachable directly from this network, but your relay connects you "
                       "(messages, files and calls all work).")
        elif tor_ok:
            summary = "Not on the same network, but reachable over Tor."
        else:
            summary = "Can't reach this contact right now."
            if tor_problem:
                summary += f" Tor: {tor_problem}."
            if hosts:
                summary += (" If one of you is on a VPN and the other on the home Wi-Fi, the VPN "
                            "must include the home network (WireGuard AllowedIPs).")
            if relay_known and not relay_ok:
                summary += " Your relay isn't connected right now — check Settings → Network."
        return {
            "hash_id": hash_id,
            "path": path,
            "speed": self._speed_advice(hash_id, path),
            "addresses": results,
            "relay": {"configured": relay_known, "connected": relay_ok},
            "my_addresses": self.local_candidates(),
            "tor": {**self.tor_status(), "contact_has_address": bool(self.peer_onion(hash_id))},
            "summary": summary,
        }
