"""Contacts over a serial radio: private beacons, and the session on the air.

Nobody is discovered over the radio. A device only sends *beacons* that name,
privately, the contacts it has no working session with (see
:mod:`srltcp.core.protocol.beacon`): strangers in range learn nothing, and an
untrusted device can't start anything. A contact that finds itself named
answers with a beacon of its own and the two connect with the normal signed,
quantum-safe handshake over the radio:

- the lower identity hash dials at once; the other one dials too if nothing
  happened after ``RADIO_DIAL_GRACE`` (so a lost handshake never leaves the
  pair stuck);
- a beacon naming us from a contact we *do* have a radio session with means
  their side lost it (app restarted, a lost handshake answer): ours is dropped
  and the two connect again, instead of one phone showing "connected" and the
  other not, for good.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import TYPE_CHECKING

from srltcp.core.protocol import beacon
from srltcp.core.protocol.messages import MessageType, build_header
from srltcp.utils.logging import get_logger

if TYPE_CHECKING:
    from srltcp.core.messaging.backend import MessagingBackend

log = get_logger(__name__)

RADIO_BEACON_EVERY = 30.0  # while a contact we have no session with may be in range
RADIO_REPLY_EVERY = 5.0  # answer beacons at most this often
RADIO_NEAR_FOR = 120.0  # a contact counts as in range this long after its last beacon
RADIO_DIAL_GRACE = 45.0  # the higher hash dials too when the lower one hasn't by then
RADIO_ALIVE_WINDOW = 32.0  # any packet from the radio this recently: the line is alive
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
        self._radio_seen: OrderedDict[bytes, None] = OrderedDict()  # beacon heads (replays)
        self._radio_beacon_at = 0.0
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

    def radio_alive(self: MessagingBackend) -> bool:
        """The radio delivered a good packet lately (the line is point to
        point: whatever arrives comes from the contact at the other end)."""
        transport = self.serial_transport
        if transport is None or not getattr(transport, "alive", False):
            return False
        last_rx = float(transport.link_stats().get("last_rx", 0.0))
        return bool(last_rx) and time.time() - last_rx < RADIO_ALIVE_WINDOW

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

    def _radio_handshake_pending(self: MessagingBackend) -> bool:
        return any(
            link.transport == "serial" and not link.handshake_complete
            and hash_id in self._pending_handshakes
            for hash_id, link in self._links.items()
        )

    def _radio_wanted(self: MessagingBackend) -> list[str]:
        """Contacts with no working session (the Tor doorbell carries nothing)."""
        out = []
        for peer in self.trusted.list_peers():
            if peer.blocked:
                continue
            link = self.get_link(peer.hash_id)
            if link and link.handshake_complete and not self._link_on_tor(link):
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
        log.debug("Radio beacon sent (%d contact tag(s)%s)", len(keys), ", answer" if reply else "")
        self._radio_beacon_at = time.monotonic()
        return True

    async def radio_tick(self: MessagingBackend, *, now_open: bool = False) -> None:
        """From the ping loop (and when the port opens): beacon while a contact
        may be waiting, and take over from a contact that didn't dial."""
        if now_open or time.monotonic() - self._radio_beacon_at >= RADIO_BEACON_EVERY:
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
        ]
        if not named:
            return  # not for us (or not a contact): no answer, nothing learnt
        log.debug("Radio beacon from %s", ", ".join(h[:8] for h in named))
        now = time.monotonic()
        for hash_id in named:
            self._radio_heard[hash_id] = now
            link = self.get_link(hash_id)
            if self._radio_session(hash_id):
                if link and now - link.established < RADIO_SESSION_SETTLE:
                    continue  # (sent before our session came up)
                # They name us only while they have no session: theirs is gone.
                log.info("Radio session with %s lost on their side — reconnecting", hash_id[:8])
                await self._teardown_link(hash_id)
            self._radio_waiting.setdefault(hash_id, now)
            self._maybe_dial_radio(hash_id)
        if not heard.reply and now - self._radio_replied_at >= RADIO_REPLY_EVERY:
            self._radio_replied_at = now
            try:
                await self.send_radio_beacon(reply=True)
            except (OSError, ConnectionError, RuntimeError) as exc:
                log.debug("Radio beacon answer not sent: %s", exc)

    # ── Connecting ──

    def _maybe_dial_radio(self: MessagingBackend, hash_id: str) -> None:
        if hash_id in self._radio_dialing or self.serial_transport is None:
            return
        link = self.get_link(hash_id)
        if link and link.handshake_complete and not self._link_on_tor(link):
            self._radio_waiting.pop(hash_id, None)
            return  # connected (this radio, Wi-Fi or the internet)
        waiting = time.monotonic() - self._radio_waiting.get(hash_id, time.monotonic())
        if self._me_hash() > hash_id and waiting < RADIO_DIAL_GRACE:
            return  # they dial first
        self._radio_dialing.add(hash_id)
        task = asyncio.create_task(self._dial_radio(hash_id, replace=link is not None))
        self.__dict__.setdefault("_radio_tasks", set()).add(task)
        task.add_done_callback(self.__dict__["_radio_tasks"].discard)

    async def _dial_radio(self: MessagingBackend, hash_id: str, *, replace: bool) -> None:
        from srltcp.core.messaging.connect import SERIAL_HANDSHAKE_TTL

        try:
            log.info("Contact %s in radio range — connecting over serial", hash_id[:8])
            await self.connect_to_peer(hash_id, transport="serial", force=replace)
            if await self.wait_for_handshake(hash_id, timeout=SERIAL_HANDSHAKE_TTL):
                self._radio_waiting.pop(hash_id, None)
        except (OSError, ConnectionError, RuntimeError, KeyError, TimeoutError) as exc:
            log.info("Serial connect to %s failed: %s", hash_id[:8], exc)
        finally:
            self._radio_dialing.discard(hash_id)
            if self._radio_waiting.get(hash_id):
                self._radio_waiting[hash_id] = time.monotonic() - RADIO_DIAL_GRACE
