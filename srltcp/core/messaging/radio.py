"""Contacts over a serial radio: private beacons, and the session on the air.

Nobody is discovered over the radio. A device only sends *beacons* that name,
privately, the contacts it has no session with on the radio or a shared
Wi-Fi/LAN (see :mod:`srltcp.core.protocol.beacon`): strangers in range learn
nothing, and an untrusted device can't start anything. A contact that finds
itself named answers with a beacon of its own and the two connect with the
normal signed, quantum-safe handshake over the radio:

- the radio comes first unless the two share a Wi-Fi/LAN: a session over the
  internet (or the Tor doorbell) is replaced by one on the radio, opened
  alongside it so nothing is lost meanwhile; the same Wi-Fi/LAN takes over
  from the radio again (``maintain_paths``);
- the lower identity hash dials at once; the other one dials too if nothing
  happened after ``RADIO_DIAL_GRACE`` (so a lost handshake never leaves the
  pair stuck);
- the answering side counts the session as up only once the other side
  proves it has it (its first encrypted message): on a lossy radio the
  handshake answer can be lost;
- a beacon naming us from a contact we have a radio session with means their
  side doesn't have it (app restarted, a lost answer): ours is dropped and the
  two connect again, instead of one phone showing "connected" and the other
  not, for good.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections import OrderedDict
from typing import TYPE_CHECKING

from srltcp.core.messaging.links import PeerLink
from srltcp.core.protocol import beacon
from srltcp.core.protocol.messages import MessageType, build_header
from srltcp.utils.logging import get_logger

if TYPE_CHECKING:
    from srltcp.core.messaging.backend import MessagingBackend

log = get_logger(__name__)

RADIO_BEACON_EVERY = 30.0  # while a contact we have no session with may be in range…
RADIO_BEACON_EAGER = 4.0  # …and this often for RADIO_EAGER_FOR after the radio opens or a
RADIO_EAGER_FOR = 90.0  # radio session drops (each time ±30 %: two phones never stay in step)
RADIO_TICK = 1.0  # the radio loop: beacons, dialing
RADIO_WATCHDOG_EVERY = 2  # ticks: an adapter plugged in is noticed within ~2 s
RADIO_REPLY_EVERY = 5.0  # answer beacons at most this often
RADIO_NEAR_FOR = 120.0  # a contact counts as in range this long after its last beacon
RADIO_DIAL_GRACE = 45.0  # the higher hash dials too when the lower one hasn't by then
RADIO_ALIVE_WINDOW = 32.0  # any packet from the radio this recently: the line is alive
# A contact dialing us over the internet has no radio session on their side
# (it would keep that one): ours only wins while the radio is busy right now.
RADIO_HEARD_RECENTLY = 12.0
RADIO_CHECK_WAIT = 5.0  # a ping over a working radio is answered well within this
# A beacon can wait on the radio behind a handshake (one message at a time,
# slow line): one naming us this soon after our session came up may predate it.
RADIO_SESSION_SETTLE = 120.0


class RadioMixin:
    """Private beacons and connecting to contacts in radio range."""

    def _init_radio(self: MessagingBackend) -> None:
        self._radio_keys: dict[str, tuple[str, bytes]] = {}  # hash -> (their key hex, pair key)
        self._radio_heard: dict[str, float] = {}  # hash -> last beacon naming us
        self._radio_waiting: dict[str, float] = {}  # hash -> heard while we had no session
        self._radio_dialing: set[str] = set()
        self._radio_checking: set[str] = set()
        self._radio_seen: OrderedDict[bytes, None] = OrderedDict()  # beacon heads (replays)
        # hash -> newest beacon time naming us (kept, unlike _radio_seen: junk
        # beacons can't push it out, so an old beacon replayed later is ignored)
        self._radio_newest: dict[str, int] = {}
        self._radio_beacon_at = 0.0
        self._radio_beacon_due = 0.0  # monotonic time of the next beacon
        self._radio_eager_until = 0.0
        self._radio_replied_at = 0.0

    # ── State ──

    def _radio_key(self: MessagingBackend, hash_id: str) -> bytes | None:
        peer = self.trusted.get(hash_id)
        if not peer or peer.blocked or not peer.public_key:
            return None
        cached = self._radio_keys.get(hash_id)
        if cached and cached[0] == peer.public_key:
            return cached[1]
        try:
            key = beacon.pair_key(
                self._identity_for_transport("serial").private_key,
                bytes.fromhex(peer.public_key),
            )
        except ValueError:
            return None
        self._radio_keys[hash_id] = (peer.public_key, key)
        return key

    def radio_alive(self: MessagingBackend, window: float = RADIO_ALIVE_WINDOW) -> bool:
        """The radio delivered a good packet in the last ``window`` seconds
        (the line is point to point: whatever arrives comes from the contact
        at the other end)."""
        transport = self.serial_transport
        if transport is None or not getattr(transport, "alive", False):
            return False
        last_rx = float(transport.link_stats().get("last_rx", 0.0))
        return bool(last_rx) and time.time() - last_rx < window

    def radio_contact_near(self: MessagingBackend, hash_id: str) -> bool:
        """The contact's beacon was heard on the radio lately."""
        heard = self._radio_heard.get(hash_id)
        return (
            heard is not None
            and self.serial_transport is not None
            and time.monotonic() - heard < RADIO_NEAR_FOR
        )

    def _radio_session(self: MessagingBackend, hash_id: str) -> bool:
        link = self.get_link(hash_id)
        return bool(link and link.handshake_complete and link.transport == "serial")

    def _radio_holds(
        self: MessagingBackend, hash_id: str, window: float = RADIO_ALIVE_WINDOW
    ) -> bool:
        """A working radio session with this contact: something it encrypted
        arrived within ``window`` (any packet on the radio could be anyone's)."""
        link = self.get_link(hash_id)
        return bool(
            link and link.up and link.transport == "serial" and self.radio_alive(window)
            and time.monotonic() - link.last_seen < window
        )

    def _tcp_peer_on_lan(self: MessagingBackend, peer_id: str) -> bool:
        """A TCP/UDP connection to an address on our own Wi-Fi/LAN or VPN."""
        from srltcp.utils.network import same_network

        transport = self.tcp_transport
        if transport is None or transport.is_tor(peer_id):
            return False
        for peer in transport.peers():
            if peer.peer_id == peer_id:
                host = peer.address.rsplit(":", 1)[0].strip("[]")
                return bool(host) and same_network(host)
        return False

    def _beats_radio(self: MessagingBackend, link: PeerLink | None) -> bool:
        """This session is the radio already, or the same Wi-Fi/LAN (which
        comes before the radio); anything else, the radio replaces."""
        if not link or not link.up:
            return False
        return link.transport == "serial" or (
            link.transport == "tcp" and self._tcp_peer_on_lan(link.transport_peer_id)
        )

    def _radio_handshake_pending(self: MessagingBackend) -> bool:
        return any(
            link.transport == "serial" and not link.handshake_complete
            and hash_id in self._pending_handshakes
            for hash_id, link in self._links.items()
        )

    def _radio_wanted(self: MessagingBackend) -> list[str]:
        """Contacts we'd rather reach over the radio than how we reach them now."""
        out = []
        for peer in self.trusted.list_peers():
            if peer.blocked:
                continue
            if self._beats_radio(self.get_link(peer.hash_id)):
                self._radio_waiting.pop(peer.hash_id, None)
                continue
            out.append(peer.hash_id)
        return out

    # ── Beacons ──

    async def send_radio_beacon(
        self: MessagingBackend, *, reply: bool = False, force: bool = False
    ) -> bool:
        """Name, privately, the contacts we'd like a session with. ``force``:
        send even if there are none (only random filler then)."""
        transport = self.serial_transport
        if transport is None or not getattr(transport, "alive", False):
            return False
        if self._radio_dialing or self._radio_handshake_pending():
            return False  # it would go out after the handshake, naming them stale
        keys = [k for k in (self._radio_key(h) for h in self._radio_wanted()) if k]
        if not keys and not force:
            return False
        packet = build_header(MessageType.RADIO_BEACON, body=beacon.build(keys, reply=reply))
        await transport.broadcast(packet)  # (once, unacknowledged: maybe nobody listens)
        self._radio_beacon_at = time.monotonic()
        log.debug("Radio beacon sent (%d contact tag(s)%s)", len(keys), ", answer" if reply else "")
        return True

    def radio_eager(self: MessagingBackend) -> None:
        """The radio just opened or a session on it dropped: beacon often for a
        while, the first one after a short random delay (on a radio without
        its own medium access, two phones that start at the same moment would
        otherwise send at the same moment, and keep doing so)."""
        now = time.monotonic()
        self._radio_eager_until = now + RADIO_EAGER_FOR
        self._radio_beacon_due = min(
            self._radio_beacon_due or now + 2.0, now + random.uniform(0.2, 1.5)
        )

    async def radio_loop(self: MessagingBackend) -> None:
        """Every RADIO_TICK: the adapter (plugged in, unplugged), beacons, dialing."""
        tick = 0
        while self._running:
            await asyncio.sleep(RADIO_TICK)
            tick += 1
            try:
                if tick % RADIO_WATCHDOG_EVERY == 0:
                    await self.serial_watchdog()
                if self.serial_transport is not None:
                    await self.radio_tick()
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                log.debug("Radio check failed: %s", exc)

    async def radio_tick(self: MessagingBackend, *, now_open: bool = False) -> None:
        """Beacon while a contact may be waiting (at a jittered interval), and
        take over from a contact that didn't dial."""
        if now_open:
            self.radio_eager()
        now = time.monotonic()
        if now >= self._radio_beacon_due:
            eager = now < self._radio_eager_until
            every = RADIO_BEACON_EAGER if eager else RADIO_BEACON_EVERY
            self._radio_beacon_due = now + every * random.uniform(0.7, 1.3)
            try:
                await self.send_radio_beacon()
            except (OSError, ConnectionError, RuntimeError) as exc:
                log.debug("Radio beacon not sent: %s", exc)
        for hash_id in list(self._radio_waiting):
            if self.radio_contact_near(hash_id):
                self._maybe_dial_radio(hash_id)
            else:
                self._radio_waiting.pop(hash_id, None)

    async def _handle_radio_beacon(self: MessagingBackend, body: bytes) -> None:
        heard = beacon.parse(body)
        if heard is None or heard.head in self._radio_seen:
            return  # malformed, stale, or a copy of one we had (replay)
        self._radio_seen[heard.head] = None
        while len(self._radio_seen) > 256:
            self._radio_seen.popitem(last=False)
        named = [
            peer.hash_id
            for peer in self.trusted.list_peers()
            if (key := self._radio_key(peer.hash_id)) and heard.names(key)
            and heard.timestamp > self._radio_newest.get(peer.hash_id, 0)
        ]
        for hash_id in named:
            self._radio_newest[hash_id] = heard.timestamp
        if not named:
            return  # not for us (or not a contact): no answer, nothing learnt
        log.debug("Radio beacon from %s", ", ".join(h[:8] for h in named))
        now = time.monotonic()
        for hash_id in named:
            self._radio_heard[hash_id] = now
            link = self.get_link(hash_id)
            if link and self._radio_session(hash_id):
                if link.confirmed and (
                    now - link.established < RADIO_SESSION_SETTLE
                    or heard.timestamp <= link.established_wall
                ):
                    continue  # (sent before our session came up)
                # They name us only while they have no radio session: theirs is gone.
                log.info("Radio session with %s lost on their side — reconnecting", hash_id[:8])
                await self._drop_radio_session(hash_id)
            elif (
                link and link.up and self._beats_radio(link)
                and heard.timestamp > link.established_wall
            ):
                # They name us only while they have no session with us on the
                # radio or a shared Wi-Fi: our Wi-Fi session is stale on their
                # side (they switched Wi-Fi off, restarted). Check it now.
                self._verify_or_radio(hash_id)
            self._radio_waiting.setdefault(hash_id, now)
            self._maybe_dial_radio(hash_id)
        if not heard.reply and now - self._radio_replied_at >= RADIO_REPLY_EVERY:
            self._radio_replied_at = now
            try:
                await self.send_radio_beacon(reply=True)
            except (OSError, ConnectionError, RuntimeError) as exc:
                log.debug("Radio beacon answer not sent: %s", exc)

    def _verify_or_radio(self: MessagingBackend, hash_id: str) -> None:
        """Ping our (Wi-Fi) session with a contact whose beacon says it has
        none; no answer in RADIO_CHECK_WAIT: drop it and dial over the radio
        at once (whoever's turn it would be: they're waiting)."""
        if hash_id in self._radio_checking:
            return
        self._radio_checking.add(hash_id)

        async def check() -> None:
            try:
                link = self.get_link(hash_id)
                if not link:
                    return
                asked = time.monotonic()
                with contextlib.suppress(KeyError, OSError, ConnectionError, RuntimeError):
                    await self.ping_peer(hash_id)
                while time.monotonic() - asked < RADIO_CHECK_WAIT:
                    await asyncio.sleep(0.2)
                    if self.get_link(hash_id) is not link or link.last_seen > asked:
                        return  # answered (or replaced): it works
                log.info("Session with %s doesn't answer and they call on the radio — "
                         "moving to the radio", hash_id[:8])
                self.path_went_dead(hash_id)
                try:
                    await self._teardown_link(hash_id)
                finally:
                    self._radio_waiting[hash_id] = time.monotonic() - RADIO_DIAL_GRACE
                    self._maybe_dial_radio(hash_id)
            finally:
                self._radio_checking.discard(hash_id)

        task = asyncio.create_task(check())
        self.__dict__.setdefault("_radio_tasks", set()).add(task)
        task.add_done_callback(self.__dict__["_radio_tasks"].discard)

    def check_radio_session(self: MessagingBackend, hash_id: str) -> None:
        """Ping the radio session; no answer in RADIO_CHECK_WAIT: drop it and
        connect over the other paths at once."""
        if hash_id in self._radio_checking:
            return
        self._radio_checking.add(hash_id)
        task = asyncio.create_task(self._check_radio_session(hash_id))
        self.__dict__.setdefault("_radio_tasks", set()).add(task)
        task.add_done_callback(self.__dict__["_radio_tasks"].discard)

    async def _check_radio_session(self: MessagingBackend, hash_id: str) -> None:
        try:
            link = self.get_link(hash_id)
            if not link or link.transport != "serial":
                return
            asked = time.monotonic()
            with contextlib.suppress(KeyError, OSError, ConnectionError, RuntimeError):
                await self.ping_peer(hash_id)
            while time.monotonic() - asked < RADIO_CHECK_WAIT:
                await asyncio.sleep(0.2)
                current = self.get_link(hash_id)
                if current is not link:
                    return  # replaced meanwhile
                if link.last_seen > asked:
                    return  # it answered: the radio session works
            log.info("Radio session with %s doesn't answer — moving to the internet", hash_id[:8])
            await self._drop_radio_session(hash_id)
            await self.connect_to_peer(hash_id, transport="tcp")
        except (OSError, ConnectionError, RuntimeError, KeyError, TimeoutError) as exc:
            log.debug("Radio check for %s failed: %s", hash_id[:8], exc)
        finally:
            self._radio_checking.discard(hash_id)

    async def _drop_radio_session(self: MessagingBackend, hash_id: str) -> None:
        link = self.get_link(hash_id)
        name, was_up = (link.peer_name, link.up) if link else ("", False)
        await self._teardown_link(hash_id)
        self.radio_eager()
        if was_up and self._on_link_down:
            await self._on_link_down(hash_id, name)  # (the chat header shows it)

    # ── Connecting ──

    def _maybe_dial_radio(self: MessagingBackend, hash_id: str) -> None:
        if hash_id in self._radio_dialing or self.serial_transport is None:
            return
        link = self.get_link(hash_id)
        if self._beats_radio(link):
            self._radio_waiting.pop(hash_id, None)
            return  # on the radio already, or on the same Wi-Fi/LAN
        waiting = time.monotonic() - self._radio_waiting.get(hash_id, time.monotonic())
        if self._me_hash() > hash_id and waiting < RADIO_DIAL_GRACE:
            return  # they dial first
        self._radio_dialing.add(hash_id)
        task = asyncio.create_task(self._dial_radio(hash_id))
        self.__dict__.setdefault("_radio_tasks", set()).add(task)
        task.add_done_callback(self.__dict__["_radio_tasks"].discard)

    async def _dial_radio(self: MessagingBackend, hash_id: str) -> None:
        from srltcp.core.messaging.connect import SERIAL_HANDSHAKE_TTL

        try:
            link = self.get_link(hash_id)
            peers = self.serial_transport.peers() if self.serial_transport else []
            log.info("Contact %s in radio range — connecting over serial", hash_id[:8])
            if link and link.up and peers:
                # Connected over the internet or Tor: open the radio session
                # alongside; it replaces the current one once it's proven.
                peer_id = peers[0].peer_id
                if await self._send_upgrade_handshake(hash_id, peer_id, "serial") is None:
                    return
            else:
                await self.connect_to_peer(hash_id, transport="serial", force=link is not None)
            deadline = time.monotonic() + SERIAL_HANDSHAKE_TTL
            while time.monotonic() < deadline:
                link = self.get_link(hash_id)
                if self._radio_session(hash_id) or (
                    link and link.handshake_complete and self._beats_radio(link)
                ):
                    # (Also when the session went on to a shared Wi-Fi at
                    # once: a dial still "in progress" would block the next.)
                    self._radio_waiting.pop(hash_id, None)
                    return
                await asyncio.sleep(0.2)
        except (OSError, ConnectionError, RuntimeError, KeyError, TimeoutError) as exc:
            log.info("Serial connect to %s failed: %s", hash_id[:8], exc)
        finally:
            self._radio_dialing.discard(hash_id)
            if self._radio_waiting.get(hash_id):
                self._radio_waiting[hash_id] = time.monotonic() - RADIO_DIAL_GRACE
