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

One message is in flight at a time. Both ends must speak this format (SRLTCP
0.13.4 and later).
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import struct
import time
import zlib
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

MAGIC = b"\x5a\xa6"
K_DATA, K_ACK, K_DATA_NOACK, K_DATA_LAST = 1, 2, 3, 4  # LAST: end of a turn, answer now
DATA_HDR = struct.Struct(">2sBHHHB")  # magic, kind, message id, index, count, payload length
ACK_HDR = struct.Struct(">2sBHHB")  # magic, kind, message id, count, bitmap length
CRC = struct.Struct(">I")
FRAG = 50  # payload bytes per packet: 64 bytes on the line with header and CRC
PACKET = DATA_HDR.size + FRAG + CRC.size
MAX_PARTS = 1024
MAX_MESSAGE = MAX_PARTS * FRAG
GIVE_UP = 120.0  # a message not through by then is dropped (the session retries)
MIN_RATE = 60.0  # bytes/s (the slowest LoRa settings)
START_RATE = 0.5  # of the line's speed, until the radio's real rate is measured
ACK_MIN_WAIT, ACK_MAX_WAIT = 0.6, 20.0
QUIET_MIN = 0.03  # s of silence before we talk (plus a few packet times)
QUIET_MAX_HOLD = 20.0  # never wait longer than this for the other side to finish
WINDOW = 32  # packets per turn: then we wait for the answer and the other side may talk
QUEUE_MAX = 256


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
        self.max_rate = max(MIN_RATE, baudrate / 10 * 0.9)  # 10 bits per byte on the line
        self.stats = LinkStats(rate=self.max_rate * START_RATE, rtt=1.0)
        self._buf = bytearray()
        self._queue: asyncio.Queue[tuple[bytes, bool]] = asyncio.Queue(QUEUE_MAX)
        self._next_id = random.getrandbits(16)
        self._acked: dict[int, set[int]] = {}
        self._ack_event = asyncio.Event()
        self._incoming: dict[int, _Incoming] = {}
        self._delivered: OrderedDict[int, int] = OrderedDict()  # message id -> count
        self._task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._heard_at = 0.0  # monotonic time any byte last arrived

    # ── Sending ──

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._send_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for inc in self._incoming.values():
            if inc.ack_handle:
                inc.ack_handle.cancel()

    async def send(self, message: bytes, *, reliable: bool = True) -> None:
        if len(message) > MAX_MESSAGE:
            raise ValueError(f"message too large for the serial link ({len(message)} bytes)")
        await self._queue.put((message, reliable))

    def _packet_time(self) -> float:
        return PACKET / self.stats.rate

    async def _wait_quiet(self) -> None:
        """Listen before talk: wait while the other side is transmitting (at
        most about one of its turns), before a turn of ours."""
        gap = max(QUIET_MIN, 2.5 * PACKET / self.max_rate + self._packet_time())
        hold = min(QUIET_MAX_HOLD, max(1.0, 1.5 * WINDOW * self._packet_time()))
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

    async def _raw(self, data: bytes) -> None:
        async with self._write_lock:
            await self._write(data)
        self.stats.bytes_out += len(data)
        await asyncio.sleep(len(data) / self.stats.rate)  # pacing

    @staticmethod
    def _data_packet(kind: int, msg_id: int, idx: int, count: int, payload: bytes) -> bytes:
        head = DATA_HDR.pack(MAGIC, kind, msg_id, idx, count, len(payload)) + payload
        return head + CRC.pack(zlib.crc32(head))

    async def _send_loop(self) -> None:
        while True:
            message, reliable = await self._queue.get()
            try:
                await self._send_message(message, reliable)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a write error: the transport notices
                await asyncio.sleep(0.2)

    async def _send_message(self, message: bytes, reliable: bool) -> None:
        msg_id = self._next_id
        self._next_id = (self._next_id + 1) & 0xFFFF
        parts = [message[i : i + FRAG] for i in range(0, len(message), FRAG)] or [b""]
        count = len(parts)
        if not reliable:
            await self._wait_quiet()
            for idx, part in enumerate(parts):
                await self._raw(self._data_packet(K_DATA_NOACK, msg_id, idx, count, part))
            self.stats.messages_out += 1
            return
        missing = set(range(count))
        self._acked[msg_id] = set()
        started = time.monotonic()
        sent_once: set[int] = set()
        try:
            while missing and time.monotonic() - started < GIVE_UP:
                await self._wait_quiet()
                turn = sorted(missing)[:WINDOW]
                for idx in turn:
                    kind = K_DATA_LAST if idx == turn[-1] else K_DATA
                    await self._raw(self._data_packet(kind, msg_id, idx, count, parts[idx]))
                    if idx in sent_once:
                        self.stats.resent += 1
                    sent_once.add(idx)
                sent_at = time.monotonic()
                self._ack_event.clear()
                wait = min(ACK_MAX_WAIT, max(ACK_MIN_WAIT, 2.0 * self.stats.rtt + 0.2))
                answered = False
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._ack_event.wait(), wait)  # (any answer)
                    answered = True
                missing -= self._acked.get(msg_id, set())
                self._adapt(answered, sent_at, got=len(set(turn) - missing), sent=len(turn))
            if missing:
                self.stats.given_up += 1
            else:
                self.stats.messages_out += 1
        finally:
            self._acked.pop(msg_id, None)

    def _adapt(self, answered: bool, sent_at: float, *, got: int, sent: int) -> None:
        """Pace below what makes the radio lose packets (its buffer overflows
        when we're faster than its air rate); wait for answers as long as
        they take."""
        s = self.stats
        if not answered:  # nothing came back: slow down, wait longer next time
            s.rate = max(MIN_RATE, s.rate * 0.7)
            s.rtt = min(ACK_MAX_WAIT, s.rtt * 1.5)
            return
        s.rtt = 0.75 * s.rtt + 0.25 * (time.monotonic() - sent_at)
        lost = 1 - got / max(sent, 1)
        if lost == 0:
            s.rate = min(self.max_rate, s.rate * 1.15)
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
            if kind in (K_DATA, K_DATA_NOACK, K_DATA_LAST):
                if len(buf) < DATA_HDR.size:
                    return
                _m, _k, msg_id, idx, count, ln = DATA_HDR.unpack_from(buf)
                total = DATA_HDR.size + ln + CRC.size
                if ln > FRAG or not 0 < count <= MAX_PARTS or idx >= count:
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
                    self._ack_event.set()
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
