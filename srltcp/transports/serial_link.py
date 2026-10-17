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
CRC = struct.Struct(">I")
FRAG = 50  # payload bytes per packet: 64 bytes on the line with header and CRC
PACKET = DATA_HDR.size + FRAG + CRC.size
MAX_PARTS = 1024
MAX_MESSAGE = MAX_PARTS * FRAG
GIVE_UP = 120.0  # a message not through by then is dropped (the session retries)
MIN_RATE = 60.0  # bytes/s (the slowest LoRa settings)
START_RATE = 1.0  # of the line's speed (slower radios: lowered on loss)
ACK_MIN_WAIT, ACK_MAX_WAIT = 0.6, 20.0
QUIET_MIN = 0.03  # s of silence before we talk (plus a few packet times)
QUIET_MAX_HOLD = 5.0  # never wait longer than this for the other side to finish
WINDOW = 64  # packets per turn: then we wait for the answer and the other side may talk
BATCH = 8  # packets per write (each write is a USB transfer)
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

    def to_dict(self) -> dict[str, float | int]:
        return dict(self.__dict__)


@dataclass
class _Incoming:
    count: int
    parts: dict[int, bytes] = field(default_factory=dict)
    last_at: float = 0.0
    gap: float = 0.0  # smoothed time between its packets
    ack_handle: asyncio.TimerHandle | None = None


@dataclass
class _Outgoing:
    message: bytes
    reliable: bool
    tag: str = ""  # bulk messages: what they belong to (cancel_bulk drops them)
    seq: int = 0  # order queued (a cancel drops only what was queued before it)


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
        self.stats = LinkStats(rate=self.max_rate * START_RATE, rtt=0.5)
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
        self._ack_events: dict[int, asyncio.Event] = {}
        self._incoming: dict[int, _Incoming] = {}
        self._delivered: OrderedDict[int, int] = OrderedDict()  # message id -> count
        self._write_lock = asyncio.Lock()
        # The line is taken in turns (a burst of packets and the wait for its
        # answer); urgent messages get the next turn before bulk ones.
        self._turn = asyncio.Condition()
        self._turn_busy = False
        self._urgent_waiting = 0
        self._heard_at = 0.0  # monotonic time any byte last arrived

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
        for inc in self._incoming.values():
            if inc.ack_handle:
                inc.ack_handle.cancel()

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
        while len(self._cancelled) > 64:
            self._cancelled.popitem(last=False)
        return dropped

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

    async def _wait_quiet(self) -> None:
        """Listen before talk: wait while the other side is transmitting (at
        most about one of its turns), before a turn of ours."""
        gap = max(QUIET_MIN, 2.5 * PACKET / self.max_rate)
        hold = min(QUIET_MAX_HOLD, max(0.5, 1.5 * WINDOW * PACKET / self.max_rate))
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

    async def _burst(self, packets: list[bytes]) -> None:
        """Write packets in batches (one write per BATCH packets: each write
        costs a USB transfer), paced at the current rate."""
        for i in range(0, len(packets), BATCH):
            data = b"".join(packets[i : i + BATCH])
            started = time.monotonic()
            async with self._write_lock:
                await self._write(data)
            self.stats.bytes_out += len(data)
            pause = len(data) / self.stats.rate - (time.monotonic() - started)
            if pause > 0:  # (a write that blocked at the line's speed already took its time)
                await asyncio.sleep(pause)

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
            try:
                await self._send_message(item, urgent)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a write error: the transport notices
                await asyncio.sleep(0.2)

    async def _send_message(self, item: _Outgoing, urgent: bool) -> None:
        msg_id = self._next_id
        self._next_id = (self._next_id + 1) & 0xFFFF
        message = item.message
        parts = [message[i : i + FRAG] for i in range(0, len(message), FRAG)] or [b""]
        count = len(parts)
        if not item.reliable:
            await self._take_turn(urgent)
            try:
                await self._wait_quiet()
                await self._burst([
                    self._data_packet(K_DATA_NOACK, msg_id, idx, count, part)
                    for idx, part in enumerate(parts)
                ])
            finally:
                await self._end_turn()
            self.stats.messages_out += 1
            return
        missing = set(range(count))
        self._acked[msg_id] = set()
        answer = self._ack_events[msg_id] = asyncio.Event()
        started = time.monotonic()
        sent_once: set[int] = set()
        try:
            while missing and time.monotonic() - started < GIVE_UP:
                if self._is_cancelled(item):
                    return  # (the file was cancelled or rewound: the rest isn't wanted)
                await self._take_turn(urgent)
                try:
                    await self._wait_quiet()
                    turn = sorted(missing)[:WINDOW]
                    answer.clear()
                    await self._burst([
                        self._data_packet(
                            K_DATA_LAST if idx == turn[-1] else K_DATA, msg_id, idx, count,
                            parts[idx],
                        )
                        for idx in turn
                    ])
                    self.stats.resent += sum(1 for idx in turn if idx in sent_once)
                    sent_once.update(turn)
                    sent_at = time.monotonic()
                    wait = min(ACK_MAX_WAIT, max(ACK_MIN_WAIT, 2.0 * self.stats.rtt + 0.2))
                    answered = False
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(answer.wait(), wait)
                        answered = True
                finally:
                    await self._end_turn()
                missing -= self._acked.get(msg_id, set())
                self._adapt(answered, sent_at, got=len(set(turn) - missing), sent=len(turn))
            if missing:
                self.stats.given_up += 1
            else:
                self.stats.messages_out += 1
        finally:
            self._acked.pop(msg_id, None)
            self._ack_events.pop(msg_id, None)

    def _adapt(self, answered: bool, sent_at: float, *, got: int, sent: int) -> None:
        """Pace below what makes the radio lose packets (its buffer overflows
        when we're faster than its air rate); wait for answers as long as
        they take."""
        s = self.stats
        if not answered:  # nothing came back: slow down a little, wait longer next time
            if sent >= 8:
                s.rate = max(MIN_RATE, s.rate * 0.8)
            s.rtt = min(ACK_MAX_WAIT, s.rtt * 1.5)
            return
        s.rtt = 0.75 * s.rtt + 0.25 * (time.monotonic() - sent_at)
        lost = 1 - got / max(sent, 1)
        if lost == 0:
            s.rate = min(self.max_rate, s.rate * 1.1)
        elif lost > 0.1:  # (a stray loss is the air, not us being too fast)
            s.rate = max(MIN_RATE, s.rate * (1 - lost / 2))

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
                limit = MAX_DATAGRAM if kind == K_DATAGRAM else FRAG
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
                total = ACK_HDR.size + blen + CRC.size
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
                bitmap = body[ACK_HDR.size :]
                have = {i for i in range(count) if bitmap[i // 8] >> (i % 8) & 1}
                if msg_id in self._acked:
                    self._acked[msg_id] |= have
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
        if msg_id in self._delivered:  # a resend of something we have: confirm again
            if reliable:
                self._schedule_ack(msg_id, now=kind == K_DATA_LAST)
            return
        now = time.monotonic()
        inc = self._incoming.get(msg_id)
        if inc is None or inc.count != count:
            inc = self._incoming[msg_id] = _Incoming(count, last_at=now)
            while len(self._incoming) > 8:  # abandoned partial messages
                old = next(iter(self._incoming))
                stale = self._incoming.pop(old)
                if stale.ack_handle:
                    stale.ack_handle.cancel()
        else:
            gap = now - inc.last_at
            inc.gap = gap if not inc.gap else 0.8 * inc.gap + 0.2 * gap
            inc.last_at = now
        inc.parts[idx] = payload
        if len(inc.parts) == count:
            self._incoming.pop(msg_id, None)
            if inc.ack_handle:
                inc.ack_handle.cancel()
            self._delivered[msg_id] = count
            while len(self._delivered) > 512:
                self._delivered.popitem(last=False)
            if reliable:
                await self._send_ack(msg_id)
            self.stats.messages_in += 1
            await self._deliver(b"".join(inc.parts[i] for i in range(count)))
        elif reliable:
            self._schedule_ack(msg_id, now=kind == K_DATA_LAST)

    def _schedule_ack(self, msg_id: int, *, now: bool) -> None:
        """Answer at the end of the sender's turn (its last packet says so).
        If that packet is lost, answer once the sender has gone quiet for a
        while: an answer sent while it's still transmitting would be lost on a
        half-duplex radio, and would make it resend what is still on the way."""
        loop = asyncio.get_running_loop()
        inc = self._incoming.get(msg_id)
        if inc is not None and inc.ack_handle:
            inc.ack_handle.cancel()
            inc.ack_handle = None
        if now or inc is None:
            asyncio.ensure_future(self._send_ack(msg_id))
            return
        delay = min(3.0, max(0.3, 4.0 * inc.gap, 4 * PACKET / self.max_rate))
        inc.ack_handle = loop.call_later(
            delay, lambda: asyncio.ensure_future(self._send_ack(msg_id))
        )

    async def _send_ack(self, msg_id: int) -> None:
        if msg_id in self._delivered:
            count = self._delivered[msg_id]
            have = set(range(count))
        else:
            inc = self._incoming.get(msg_id)
            if inc is None:
                return
            count, have = inc.count, set(inc.parts)
        bitmap = bytearray((count + 7) // 8)
        for i in have:
            bitmap[i // 8] |= 1 << (i % 8)
        head = ACK_HDR.pack(MAGIC, K_ACK, msg_id, count, len(bitmap)) + bytes(bitmap)
        packet = head + CRC.pack(zlib.crc32(head))
        try:
            async with self._write_lock:  # (answers aren't paced: they're small)
                await self._write(packet)
            self.stats.bytes_out += len(packet)
        except Exception:  # noqa: BLE001 - the transport notices a dead line
            return
