"""A reliable link over a serial line or a serial radio modem.

Radio modems (SiK/3DR telemetry radios, LoRa and ESP32 serial bridges) pass
bytes through, but they lose some on the air, drop them when their small
buffer overflows, and are half-duplex: when both ends transmit at once, both
transmissions are lost. A whole SRLTCP message (the quantum-safe handshake
alone is ~9 KiB) sent in one burst almost never arrives intact. This layer
makes the line reliable:

- every message is cut into small packets (64 bytes on the line, the size
  of one USB packet and of the smallest radio frames), each with a sequence
  number and a CRC-32; corrupt packets are dropped and the stream resyncs on
  the next packet start;
- the receiver answers with a bitmap of the packets it has at the end of
  each of the sender's turns (its last packet asks for the answer; if that
  one is lost, once the sender has gone quiet), and only the missing ones are
  sent again (selective repeat), until the message is complete or
  ``GIVE_UP`` seconds pass;
- turns, with listen before talk: a side sends at most ``WINDOW`` packets,
  then waits for the answer; before a turn it waits while the other side is
  transmitting, so the two don't destroy each other's packets on the air;
- packets are paced at the rate the radio actually delivers (measured from
  the acknowledgements) and the wait for an answer follows the measured round
  trip, so the radio's buffer never overflows and nothing is resent too soon;
- broadcasts (beacons) go out once, unacknowledged: there may be nobody
  listening.

Three lanes share the line:

- **datagrams** (call audio): one packet each, never acknowledged or resent
  (a late voice packet is useless), sent at once between the other packets;
- **urgent** messages (chat, confirmations, call setup, pings): ahead of
- **bulk** messages (file data), which only go out when nothing urgent waits.

One reliable message is in flight at a time, so file data is sent in small
pieces: an urgent message waits behind one piece at most. Both ends must speak
this format (SRLTCP 0.13.5 and later).
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import struct
import time
import zlib
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

MAGIC = b"\x5a\xa6"
K_DATA, K_ACK, K_DATA_NOACK, K_DATA_LAST = 1, 2, 3, 4  # LAST: end of a turn, answer now
K_DATAGRAM = 5  # one packet, unacknowledged (call audio)
DATA_HDR = struct.Struct(">2sBHHHB")  # magic, kind, message id, index, count, payload length
ACK_HDR = struct.Struct(">2sBHHB")  # magic, kind, message id, count, bitmap length
ACK_RATE = struct.Struct(">H")  # after the bitmap: the rate the turn arrived at (bytes/s)
CRC = struct.Struct(">I")
FRAG = 50  # payload bytes per small packet: 64 bytes on the line with header and CRC
FRAG_BIG = 114  # …and per big one (128 bytes): less overhead while few packets are lost
PACKET = DATA_HDR.size + FRAG + CRC.size
BIG_UNDER_LOSS = 0.04  # big packets while fewer than this share of packets get lost
MAX_PARTS = 1024
MAX_MESSAGE = MAX_PARTS * FRAG
GIVE_UP = 120.0  # a message with no progress this long is dropped (the session retries)
MIN_RATE = 60.0  # bytes/s (the slowest LoRa settings)
ACK_MIN_WAIT, ACK_MAX_WAIT = 0.6, 20.0
RTT_GUESS_MAX = 3.0
FLOOR_TIME = 0.1  # s after answering a turn in which our messages go out at once…
DEFER_MIN = 0.2  # …while the other side waits at least this long before its next file turn
QUIET_MIN = 0.03  # s of silence before we talk (plus a few packet times)
QUIET_MAX_HOLD = 5.0  # never wait longer than this for the other side to finish
TURN_TIME = 1.5  # s of sending per turn: then we wait for the answer and the other side may talk
# File data takes longer turns (less time spent waiting for answers); a chat
# message, call setup or ping that comes up meanwhile cuts such a turn short.
TURN_TIME_BULK = 3.0
TURN_MIN, WINDOW = 8, 255  # packets per turn, at least / at most
TURN_MESSAGES, TURN_BYTES = 8, 2048  # small messages that may share a turn
# Pace: the rate the receiver saw packets arrive (the radio's real air rate),
# probing a little above it now and then to find out when more fits.
PACE_GAINS = (1.15, 0.9, 0.97, 0.97, 0.97, 0.97, 0.97, 0.97)
RATE_SAMPLES = len(PACE_GAINS)
BATCH = 8  # packets per write at most (each write is a USB transfer)…
BATCH_TIME = 0.12  # …and about this much air time: a radio's small buffer isn't flooded
QUEUE_MAX = 256
MAX_DATAGRAM = 255  # payload bytes of a datagram packet (one-byte length)
DATAGRAM_QUEUE = 8  # datagrams waiting at most; older ones are dropped (they'd be stale)


@dataclass
class LinkStats:
    bytes_out: int = 0
    bytes_in: int = 0
    packets_in: int = 0
    bad_packets: int = 0  # CRC errors and stretches of garbage
    junk_bytes: int = 0  # bytes that weren't part of any good packet
    resent: int = 0
    messages_out: int = 0
    messages_in: int = 0
    given_up: int = 0
    datagrams_out: int = 0
    datagrams_in: int = 0
    datagrams_dropped: int = 0  # too many waiting: the oldest (stale) went
    last_rx: float = 0.0  # time.time() of the last good packet
    rate: float = 0.0  # current pacing rate, bytes/s
    rtt: float = 0.0  # measured answer time for a round, s
    delivery: float = 0.0  # rate the other side receives our packets at (from its answers)
    loss: float = 0.0  # share of our packets lost (smoothed)

    def to_dict(self) -> dict[str, float | int]:
        return dict(self.__dict__)


@dataclass
class _Incoming:
    count: int
    parts: dict[int, bytes] = field(default_factory=dict)
    last_at: float = 0.0


@dataclass
class _Outgoing:
    message: bytes
    reliable: bool
    tag: str = ""  # bulk messages: what they belong to (cancel_bulk drops them)
    seq: int = 0  # order queued (a cancel drops only what was queued before it)


def _tail_heavy(lost: list[bool]) -> bool:
    """Most of a turn's losses (``lost``: per packet, in sending order) fell in
    its second half: a radio buffer that overflowed, not noise on the air."""
    if len(lost) < TURN_MIN:
        return False
    half = len(lost) // 2
    early, late = sum(lost[:half]), sum(lost[half:])
    return late >= max(3, 0.1 * len(lost)) and late > 3 * early


class _Sending:
    """A reliable message on its way: its packets and what still has to arrive."""

    def __init__(self, item: _Outgoing, msg_id: int, frag: int) -> None:
        self.item = item
        self.msg_id = msg_id
        message = item.message
        self.parts = [message[i : i + frag] for i in range(0, len(message), frag)] or [b""]
        self.count = len(self.parts)
        self.missing = set(range(self.count))
        self.sent_once: set[int] = set()
        self.answer = asyncio.Event()


class SerialLink:
    """Reliable messages over ``write`` (raw bytes to the line); feed
    received bytes to :meth:`feed`; complete messages go to ``deliver``."""

    def __init__(
        self,
        write: Callable[[bytes], Awaitable[None]],
        deliver: Callable[[bytes], Awaitable[None]],
        baudrate: int,
    ) -> None:
        self._write = write
        self._deliver = deliver
        self.max_rate = max(MIN_RATE, baudrate / 10)  # 10 bits per byte on the line
        self.stats = LinkStats(rate=self.max_rate, rtt=0.5)
        self._rate_samples: deque[float] = deque(maxlen=RATE_SAMPLES)
        self._pace_turn = 0
        self._buf = bytearray()
        self._urgent: deque[_Outgoing] = deque()
        self._bulk: deque[_Outgoing] = deque()
        self._urgent_ready = asyncio.Event()
        self._bulk_ready = asyncio.Event()
        self._bulk_room = asyncio.Event()
        self._bulk_room.set()
        self._queued_seq = 0
        # tag -> last seq a cancel covers: older messages with the tag are dropped,
        # newer ones (the same file resent or resumed) go out.
        self._cancelled: OrderedDict[str, int] = OrderedDict()
        self._datagrams: deque[bytes] = deque()
        self._datagram_ready = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._next_id = random.getrandbits(16)
        self._acked: dict[int, set[int]] = {}
        self._sending: dict[int, _Sending] = {}  # message id in flight -> the message
        # tag -> bytes of its messages the other side has confirmed so far (live
        # progress of a file piece: the file itself is confirmed once complete)
        self._tag_acked: dict[str, int] = {}
        self._sent_count: dict[int, int] = {}  # message id in flight -> its part count
        self._ack_events: dict[int, asyncio.Event] = {}
        # Partial messages by (id, part count); unacknowledged ones (broadcasts,
        # anyone can send them) apart, so they never push out a real message.
        self._incoming: dict[tuple[int, int], _Incoming] = {}
        self._incoming_noack: dict[tuple[int, int], _Incoming] = {}
        # (id, part count) -> checksum of each part: a packet is a resend of a
        # delivered message only if it matches (not just its visible id).
        self._delivered: OrderedDict[tuple[int, int], list[int]] = OrderedDict()
        self._write_lock = asyncio.Lock()
        # The line is taken in turns (a burst of packets and the wait for its
        # answer); urgent messages get the next turn before bulk ones.
        self._turn = asyncio.Condition()
        self._turn_busy = False
        self._urgent_waiting = 0
        self._heard_at = 0.0  # monotonic time any byte last arrived
        # Longest pause seen between the packets of one of the other side's
        # turns (decaying): a radio sends in air packets, and the pauses between
        # them aren't the end of its turn. Our silence before talking must be longer.
        self._burst_gap = 0.0
        # The sender's current turn, as we receive it.
        self._rx_turn_first = self._rx_turn_last = 0.0
        self._rx_turn_bytes = self._rx_turn_first_bytes = 0
        self._turn_keys: dict[tuple[int, int], None] = {}  # messages it touched (in order)
        self._answer_timer: asyncio.TimerHandle | None = None
        # Right after answering the other side's turn, the line is ours for a
        # moment (it waits before its next turn): what we have goes out then,
        # without listening first (else the sender's next turn always wins).
        self._floor_until = 0.0

    # ── Sending ──

    def start(self) -> None:
        if not self._tasks:
            self._tasks = [
                asyncio.create_task(self._send_loop(self._urgent, self._urgent_ready, True)),
                asyncio.create_task(self._send_loop(self._bulk, self._bulk_ready, False)),
                asyncio.create_task(self._datagram_loop()),
            ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        if self._answer_timer:
            self._answer_timer.cancel()

    async def send(
        self, message: bytes, *, reliable: bool = True, bulk: bool = False, tag: str = ""
    ) -> None:
        """Queue a message: urgent (default) or ``bulk`` (file data: it goes
        in the turns urgent messages leave, and ``tag`` lets
        :meth:`cancel_bulk` drop it); ``reliable=False``: once, unacknowledged."""
        if len(message) > MAX_MESSAGE:
            raise ValueError(f"message too large for the serial link ({len(message)} bytes)")
        self._queued_seq += 1
        item = _Outgoing(message, reliable, tag, self._queued_seq)
        if bulk:
            while len(self._bulk) >= QUEUE_MAX:  # (the sender's window normally keeps it short)
                self._bulk_room.clear()
                await self._bulk_room.wait()
            self._bulk.append(item)
            self._bulk_ready.set()
        else:
            self._urgent.append(item)
            self._urgent_ready.set()

    def cancel_bulk(self, tag: str) -> int:
        """Drop the bulk messages with ``tag`` queued so far (a cancelled or
        rewound file) and stop resending the one on its way; what is queued
        with the tag afterwards goes out. Returns how many were dropped."""
        kept = [item for item in self._bulk if item.tag != tag]
        dropped = len(self._bulk) - len(kept)
        self._bulk.clear()  # (in place: the bulk sender holds this deque)
        self._bulk.extend(kept)
        self._bulk_room.set()
        self._cancelled[tag] = self._queued_seq
        self._cancelled.move_to_end(tag)
        self._tag_acked.pop(tag, None)
        while len(self._cancelled) > 64:
            self._cancelled.popitem(last=False)
        return dropped

    def tag_delivered(self, tag: str) -> int:
        """Bytes of ``tag``'s messages confirmed since it was queued (or since
        :meth:`cancel_bulk`), including messages still partly on their way."""
        return self._tag_acked.get(tag, 0)

    def forget_tag(self, tag: str) -> None:
        self._tag_acked.pop(tag, None)

    def _is_cancelled(self, item: _Outgoing) -> bool:
        return bool(item.tag) and item.seq <= self._cancelled.get(item.tag, 0)

    def send_datagram(self, data: bytes) -> bool:
        """Send ``data`` as one unacknowledged packet, ahead of everything
        else. False if it doesn't fit in one packet (send it reliably)."""
        if len(data) > MAX_DATAGRAM:
            return False
        if len(self._datagrams) >= DATAGRAM_QUEUE:
            self._datagrams.popleft()
            self.stats.datagrams_dropped += 1
        self._datagrams.append(data)
        self._datagram_ready.set()
        return True

    async def _datagram_loop(self) -> None:
        while True:
            await self._datagram_ready.wait()
            self._datagram_ready.clear()
            while self._datagrams:
                packet = self._data_packet(K_DATAGRAM, 0, 0, 1, self._datagrams.popleft())
                try:
                    async with self._write_lock:
                        await self._write(packet)
                except Exception:  # noqa: BLE001 - the transport notices a dead line
                    self._datagrams.clear()
                    break
                self.stats.bytes_out += len(packet)
                self.stats.datagrams_out += 1

    async def _wait_quiet(self, *, defer: bool = False) -> None:
        """Listen before talk: wait while the other side is transmitting (at
        most about one of its turns), before a turn of ours. ``defer``: a turn
        that can wait (file data) leaves the other side half a round trip more
        to start one of its own after its answer: on a radio without its own
        medium access, what it sends then isn't heard yet when we'd start."""
        if not defer and time.monotonic() < self._floor_until:
            self._floor_until = 0.0  # (once)
            return
        gap = max(QUIET_MIN, 2.5 * PACKET / self.max_rate, 1.5 * self._burst_gap)
        if defer:
            gap += max(DEFER_MIN, min(0.5, self.stats.rtt / 2))
        hold = min(QUIET_MAX_HOLD, 1.5 * TURN_TIME + 4 * PACKET / self.max_rate)  # (one turn)
        deadline = time.monotonic() + hold
        held = False
        while (now := time.monotonic()) < deadline:
            quiet_for = now - self._heard_at
            if quiet_for >= gap:
                break
            held = True
            await asyncio.sleep(gap - quiet_for)
        if held:  # both may have waited for the same silence: don't restart together
            await asyncio.sleep(random.uniform(0, gap))

    async def _take_turn(self, urgent: bool) -> None:
        async with self._turn:
            if urgent:
                self._urgent_waiting += 1
            try:
                await self._turn.wait_for(
                    lambda: not self._turn_busy and (urgent or not self._urgent_waiting)
                )
            finally:
                if urgent:
                    self._urgent_waiting -= 1
            self._turn_busy = True

    async def _end_turn(self) -> None:
        async with self._turn:
            self._turn_busy = False
            self._turn.notify_all()

    async def _burst(
        self, packets: list[bytes], rate: float = 0.0, *, yield_to_urgent: bool = False
    ) -> int:
        """Write packets in batches (one write per BATCH packets: each write
        costs a USB transfer), paced at ``rate`` (default: the current one).
        ``yield_to_urgent``: stop early when an urgent message waits for the
        line. Returns how many packets went out."""
        rate = rate or self.stats.rate
        size = len(packets[0]) if packets else PACKET
        # At least ~256 bytes a write: a radio that sends what it has after a
        # pause in the serial stream would otherwise send many small air
        # packets, each with its full overhead.
        batch = max(-(-256 // size), min(BATCH, int(rate * BATCH_TIME / size)))
        sent = 0
        for i in range(0, len(packets), batch):
            if yield_to_urgent and sent and self._urgent_waiting:
                break
            data = b"".join(packets[i : i + batch])
            started = time.monotonic()
            async with self._write_lock:
                await self._write(data)
            self.stats.bytes_out += len(data)
            sent += len(packets[i : i + batch])
            pause = len(data) / rate - (time.monotonic() - started)
            if pause > 0:  # (a write that blocked at the line's speed already took its time)
                await asyncio.sleep(pause)
        return sent

    @staticmethod
    def _data_packet(kind: int, msg_id: int, idx: int, count: int, payload: bytes) -> bytes:
        head = DATA_HDR.pack(MAGIC, kind, msg_id, idx, count, len(payload)) + payload
        return head + CRC.pack(zlib.crc32(head))

    async def _send_loop(self, queue: deque[_Outgoing], ready: asyncio.Event, urgent: bool) -> None:
        while True:
            if not queue:
                ready.clear()
                await ready.wait()
                continue
            item = queue.popleft()
            if not urgent:
                self._bulk_room.set()
                if self._is_cancelled(item):
                    continue
            items = [item]
            if urgent and item.reliable:
                # Small messages waiting together go in one turn, answered
                # together: each its own turn would cost a whole wait for an
                # answer (and on a slow radio, a chance to collide).
                size = len(item.message)
                while (
                    queue and queue[0].reliable and len(items) < TURN_MESSAGES
                    and size + len(queue[0].message) <= TURN_BYTES
                ):
                    size += len(queue[0].message)
                    items.append(queue.popleft())
            try:
                await self._send_items(items, urgent)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a write error: the transport notices
                await asyncio.sleep(0.2)

    def _new_id(self) -> int:
        msg_id = self._next_id
        self._next_id = (self._next_id + 1) & 0xFFFF
        return msg_id

    async def _send_items(self, items: list[_Outgoing], urgent: bool) -> None:
        frag = FRAG_BIG if self.stats.loss < BIG_UNDER_LOSS else FRAG
        if not items[0].reliable:  # (a broadcast: once, unacknowledged)
            msg_id, message = self._new_id(), items[0].message
            parts = [message[i : i + frag] for i in range(0, len(message), frag)] or [b""]
            await self._take_turn(urgent)
            try:
                await self._wait_quiet()
                await self._burst([
                    self._data_packet(K_DATA_NOACK, msg_id, idx, len(parts), part)
                    for idx, part in enumerate(parts)
                ])
            finally:
                await self._end_turn()
            self.stats.messages_out += 1
            return
        msgs = [_Sending(item, self._new_id(), frag) for item in items]
        for m in msgs:
            self._acked[m.msg_id] = set()
            self._sent_count[m.msg_id] = m.count
            self._ack_events[m.msg_id] = m.answer
            self._sending[m.msg_id] = m
        started = time.monotonic()
        unanswered = 0
        turn: list[tuple[_Sending, int]] = []
        try:
            while any(m.missing for m in msgs) and time.monotonic() - started < GIVE_UP:
                if not urgent and self._is_cancelled(items[0]):
                    return  # (the file was cancelled or rewound: the rest isn't wanted)
                if unanswered:
                    # No answer: maybe both sides sent at once (on a radio
                    # without its own medium access, both are lost). A random
                    # wait, longer each time, gets the two out of step.
                    slot = max(0.05, 3 * PACKET / self.stats.rate + self._burst_gap)
                    await asyncio.sleep(random.uniform(0, slot * min(16, 2**unanswered)))
                await self._take_turn(urgent)
                try:
                    await self._wait_quiet(defer=not urgent)
                    if unanswered and turn:
                        # No answer to the last turn: most of it probably got
                        # there (the answer was lost). Its last packet again asks
                        # for the answers, instead of the whole turn once more.
                        turn = [turn[-1]]
                    else:
                        turn = self._turn_of(msgs, frag, bulk=not urgent)
                    pace = self._pace()
                    for m in msgs:
                        m.answer.clear()
                    sent = await self._burst([
                        self._data_packet(
                            K_DATA_LAST if n == len(turn) - 1 else K_DATA,
                            m.msg_id, idx, m.count, m.parts[idx],
                        )
                        for n, (m, idx) in enumerate(turn)
                    ], pace, yield_to_urgent=not urgent)
                    if sent < len(turn):
                        # Cut short for an urgent message: the last packet sent
                        # once more, as the turn's last, asks for the answer now.
                        turn = turn[:sent]
                        m, idx = turn[-1]
                        await self._burst([self._data_packet(
                            K_DATA_LAST, m.msg_id, idx, m.count, m.parts[idx],
                        )], pace)
                    self.stats.resent += sum(1 for m, idx in turn if idx in m.sent_once)
                    for m, idx in turn:
                        m.sent_once.add(idx)
                    sent_at = time.monotonic()
                    wait = min(ACK_MAX_WAIT, max(ACK_MIN_WAIT, 2.0 * self.stats.rtt + 0.2))
                    answered = False
                    with contextlib.suppress(TimeoutError):
                        # The answers come together at the end of the turn: the
                        # one for its last packet, then a moment for the others.
                        await asyncio.wait_for(turn[-1][0].answer.wait(), wait)
                        answered = True
                        others = {m for m, _idx in turn} - {turn[-1][0]}
                        if others:
                            grace = 0.05 + len(others) * 2 * PACKET / self.max_rate
                            deadline = time.monotonic() + grace
                            for m in others:
                                left = deadline - time.monotonic()
                                if left > 0:
                                    with contextlib.suppress(TimeoutError):
                                        await asyncio.wait_for(m.answer.wait(), left)
                finally:
                    await self._end_turn()
                progress = False
                for m in msgs:
                    before = len(m.missing)
                    m.missing = set(range(m.count)) - self._acked.get(m.msg_id, set())
                    progress = progress or len(m.missing) < before
                if progress:
                    started = time.monotonic()  # (a slow radio isn't given up on)
                unanswered = 0 if answered else unanswered + 1
                lost = [idx in m.missing for m, idx in turn]
                self._adapt(answered, sent_at, got=lost.count(False), sent=len(turn),
                            unanswered=unanswered, tail=_tail_heavy(lost))
        finally:
            for m in msgs:
                self._acked.pop(m.msg_id, None)
                self._sent_count.pop(m.msg_id, None)
                self._ack_events.pop(m.msg_id, None)
                self._sending.pop(m.msg_id, None)
                if m.missing:
                    self.stats.given_up += 1
                else:
                    self.stats.messages_out += 1

    def _turn_of(
        self, msgs: list[_Sending], frag: int, *, bulk: bool = False
    ) -> list[tuple[_Sending, int]]:
        """The packets of this turn: about TURN_TIME of sending at the current
        rate (TURN_TIME_BULK for file data); a message's rest is split evenly
        over its turns (no short last turn that costs a whole wait for its
        answer)."""
        size = DATA_HDR.size + frag + CRC.size
        if not self._rate_samples:
            # The radio's rate isn't known yet: a short first turn measures it
            # (a full one at the line's speed would overflow a slower radio).
            per_turn = TURN_MIN
        else:
            seconds = TURN_TIME_BULK if bulk else TURN_TIME
            per_turn = max(TURN_MIN, min(WINDOW, int(self.stats.rate * seconds / size)))
        waiting = [(m, idx) for m in msgs for idx in sorted(m.missing)]
        turns = -(-len(waiting) // per_turn)
        return waiting[: -(-len(waiting) // turns)]

    def _pace(self) -> float:
        """The rate for the next turn: what the other side measured our packets
        arriving at (the radio's real air rate, below the line's speed when the
        radio is slower), now and then a little above it to see if more fits.
        Pacing at it keeps the radio's buffer from overflowing; losses on a
        noisy air don't slow us down (they don't lower the arrival rate)."""
        s = self.stats
        if self._rate_samples:
            gain = PACE_GAINS[self._pace_turn % len(PACE_GAINS)]
            self._pace_turn += 1
            target = max(self._rate_samples) * gain
            s.rate = max(MIN_RATE, min(self.max_rate, target, s.rate * 1.3))
        return s.rate

    def _adapt(
        self, answered: bool, sent_at: float, *, got: int, sent: int, unanswered: int = 0,
        tail: bool = False,
    ) -> None:
        """Learn from the answer to a turn: the round trip (how long to wait
        next time) and how many packets got through (the size of the next
        message's packets)."""
        s = self.stats
        if not answered:  # nothing came back: a lost answer, collisions, or the line is gone
            # (Collisions say nothing about the round trip: wait a bit longer,
            # but only real answers make the wait longer than RTT_GUESS_MAX.)
            s.rtt = max(s.rtt, min(RTT_GUESS_MAX, s.rtt * 1.25))
            # (No slower pace: collisions are about timing, not speed, and a
            # radio buffer that overflowed shows as losses at a turn's end.)
            return
        s.rtt = 0.75 * s.rtt + 0.25 * (time.monotonic() - sent_at)
        if sent >= TURN_MIN:  # (a poll or a short message says little about losses)
            lost = 1 - got / sent
            if tail and self._rate_samples:
                # Lost mostly at the end of the turn: the radio's buffer filled
                # up and overflowed (we were faster than it sends; random losses
                # on the air are spread out). Slow down.
                cut = 1 - min(0.5, lost / 2)
                self._rate_samples = deque(
                    (r * cut for r in self._rate_samples), maxlen=RATE_SAMPLES
                )
            s.loss = 0.8 * s.loss + 0.2 * lost

    def _note_delivery(self, rate: float, packets: int) -> None:
        """The other side's answer says at what rate our turn arrived."""
        if rate <= 0 or packets < TURN_MIN:
            return  # (a few packets say little about the rate)
        self._rate_samples.append(min(self.max_rate, rate))
        self.stats.delivery = rate

    # ── Receiving ──

    async def feed(self, data: bytes) -> None:
        if data:
            self._heard_at = time.monotonic()
        self.stats.bytes_in += len(data)
        buf = self._buf
        buf += data
        while True:
            start = buf.find(MAGIC)
            if start < 0:
                drop = max(0, len(buf) - 1)  # (keep a possible half magic)
                self._junk(drop)
                del buf[:drop]
                return
            if start:
                self._junk(start)
                del buf[:start]
            if len(buf) < 3:
                return
            kind = buf[2]
            if kind in (K_DATA, K_DATA_NOACK, K_DATA_LAST, K_DATAGRAM):
                if len(buf) < DATA_HDR.size:
                    return
                _m, _k, msg_id, idx, count, ln = DATA_HDR.unpack_from(buf)
                total = DATA_HDR.size + ln + CRC.size
                limit = MAX_DATAGRAM if kind == K_DATAGRAM else FRAG_BIG
                if ln > limit or not 0 < count <= MAX_PARTS or idx >= count:
                    self._bad(buf)
                    continue
                if len(buf) < total:
                    return
                body = bytes(buf[: total - CRC.size])
                if CRC.unpack_from(buf, total - CRC.size)[0] != zlib.crc32(body):
                    self._bad(buf)
                    continue
                del buf[:total]
                self._good()
                if kind == K_DATAGRAM:
                    self.stats.datagrams_in += 1
                    await self._deliver(body[DATA_HDR.size :])
                else:
                    await self._on_data(kind, msg_id, idx, count, body[DATA_HDR.size :])
            elif kind == K_ACK:
                if len(buf) < ACK_HDR.size:
                    return
                _m, _k, msg_id, count, blen = ACK_HDR.unpack_from(buf)
                total = ACK_HDR.size + blen + ACK_RATE.size + CRC.size
                if not 0 < count <= MAX_PARTS or blen != (count + 7) // 8:
                    self._bad(buf)
                    continue
                if len(buf) < total:
                    return
                body = bytes(buf[: total - CRC.size])
                if CRC.unpack_from(buf, total - CRC.size)[0] != zlib.crc32(body):
                    self._bad(buf)
                    continue
                del buf[:total]
                self._good()
                bitmap = body[ACK_HDR.size : ACK_HDR.size + blen]
                (arrived,) = ACK_RATE.unpack_from(body, ACK_HDR.size + blen)
                have = {i for i in range(count) if bitmap[i // 8] >> (i % 8) & 1}
                if msg_id in self._acked and self._sent_count.get(msg_id) == count:
                    # The receiver's whole picture (it keeps what it got): the
                    # latest answer counts, not everything any answer claimed.
                    fresh = have - self._acked[msg_id]
                    new = len(fresh)
                    self._acked[msg_id] = have
                    sending = self._sending.get(msg_id)
                    if sending is not None and sending.item.tag and fresh:
                        tag = sending.item.tag
                        self._tag_acked[tag] = self._tag_acked.get(tag, 0) + sum(
                            len(sending.parts[i]) for i in fresh
                        )
                    self._note_delivery(float(arrived), new)
                    self._ack_events[msg_id].set()
            else:
                self._bad(buf)

    def _junk(self, n: int) -> None:
        if n > 1:
            self.stats.junk_bytes += n

    def _bad(self, buf: bytearray) -> None:
        self.stats.bad_packets += 1
        self.stats.junk_bytes += 1
        del buf[:1]  # resync on the next magic

    def _good(self) -> None:
        self.stats.packets_in += 1
        self.stats.last_rx = time.time()

    async def _on_data(self, kind: int, msg_id: int, idx: int, count: int, payload: bytes) -> None:
        reliable = kind != K_DATA_NOACK
        key = (msg_id, count)
        now = time.monotonic()
        if reliable:
            self._note_turn_packet(now, DATA_HDR.size + len(payload) + CRC.size)
            self._turn_keys[key] = None
        done = self._delivered.get(key) if reliable else None
        if done is not None and done[idx] != zlib.crc32(payload):
            del self._delivered[key]  # same id, other content: a new message (ids wrap)
            done = None
        if done is None:
            table = self._incoming if reliable else self._incoming_noack
            inc = table.get(key)
            if inc is None:
                inc = table[key] = _Incoming(count, last_at=now)
                while len(table) > (8 if reliable else 4):  # abandoned partial messages
                    table.pop(min(table, key=lambda k: table[k].last_at))
            inc.last_at = now
            inc.parts[idx] = payload
            if len(inc.parts) == count:
                table.pop(key, None)
                if reliable:
                    self._delivered[key] = [zlib.crc32(inc.parts[i]) for i in range(count)]
                    while len(self._delivered) > 512:
                        self._delivered.popitem(last=False)
                self.stats.messages_in += 1
                await self._deliver(b"".join(inc.parts[i] for i in range(count)))
        # (else: a resend of something we have: confirmed again with the turn)
        if not reliable:
            return
        if kind == K_DATA_LAST:
            await self._answer_turn()  # the sender waits for it now
        else:
            self._arm_answer()

    def _note_turn_packet(self, now: float, size: int) -> None:
        """Measure the sender's turn: the rate it arrives at, and the pauses
        inside it (a radio sends in air packets: those pauses aren't the end
        of its turn; our silence before talking must be longer)."""
        if self._rx_turn_bytes:
            pause = now - self._rx_turn_last
            if pause < 1.0:
                self._burst_gap = max(pause, 0.95 * self._burst_gap)
            if now - self._rx_turn_first < 0.005:
                # (arrived together with the first one, in the same air packet:
                # it says nothing about the rate)
                self._rx_turn_first_bytes += size
        else:
            self._rx_turn_first, self._rx_turn_first_bytes = now, size
        self._rx_turn_last = now
        self._rx_turn_bytes += size

    def _arm_answer(self) -> None:
        """The sender's last packet of the turn may be lost: answer anyway once
        it has gone quiet for a while (not sooner: an answer sent while it's
        still transmitting would be lost on a half-duplex radio)."""
        if self._answer_timer:
            self._answer_timer.cancel()
        delay = min(3.0, max(0.3, 4.0 * self._burst_gap, 4 * PACKET / self.max_rate))
        self._answer_timer = asyncio.get_running_loop().call_later(
            delay, lambda: asyncio.ensure_future(self._answer_turn())
        )

    async def _answer_turn(self) -> None:
        """At the end of the sender's turn: answer every message it touched
        (back to back), with the rate the turn arrived at."""
        if self._answer_timer:
            self._answer_timer.cancel()
            self._answer_timer = None
        span = self._rx_turn_last - self._rx_turn_first
        rate = (
            (self._rx_turn_bytes - self._rx_turn_first_bytes) / span if span > 0.02 else 0.0
        )
        self._rx_turn_bytes = 0
        keys, self._turn_keys = list(self._turn_keys), {}
        for key in keys:
            await self._send_ack(key, min(0xFFFF, int(rate)))
        if keys:
            self._floor_until = time.monotonic() + FLOOR_TIME

    async def _send_ack(self, key: tuple[int, int], rate: int = 0) -> None:
        msg_id, count = key
        if key in self._delivered:
            have = set(range(count))
        else:
            inc = self._incoming.get(key)
            if inc is None:
                return
            have = set(inc.parts)
        bitmap = bytearray((count + 7) // 8)
        for i in have:
            bitmap[i // 8] |= 1 << (i % 8)
        head = (ACK_HDR.pack(MAGIC, K_ACK, msg_id, count, len(bitmap)) + bytes(bitmap)
                + ACK_RATE.pack(rate or 0))
        packet = head + CRC.pack(zlib.crc32(head))
        try:
            async with self._write_lock:  # (answers aren't paced: they're small)
                await self._write(packet)
            self.stats.bytes_out += len(packet)
        except Exception:  # noqa: BLE001 - the transport notices a dead line
            return
