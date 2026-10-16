"""A reliable link over a serial line or a serial radio modem.

Radio modems (SiK/3DR telemetry radios, LoRa and ESP32 serial bridges) pass
bytes through, but they lose some on the air and drop them when their small
buffer (a few hundred bytes to ~2 KiB) overflows. A whole SRLTCP message (the
quantum-safe handshake alone is ~9 KiB) sent in one burst therefore almost
never arrives intact. This layer makes the line reliable:

- every message is cut into small packets (``FRAG`` bytes) with a sequence
  number and a CRC-32; corrupt packets are dropped and the stream resyncs on
  the next packet start;
- the receiver answers with a bitmap of the packets it has, and only the
  missing ones are sent again (selective repeat), until the message is
  complete or ``GIVE_UP`` seconds pass;
- packets are paced at a rate below the line's speed, lowered on loss and
  raised again while everything gets through, so the radio's buffer never
  overflows;
- broadcasts (announces) go out once, unacknowledged: there may be nobody
  listening.

One message is in flight at a time (radios are half-duplex). Both ends must
speak this format (SRLTCP 0.13.2 and later).
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
import time
import zlib
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

MAGIC = b"\x5a\xa5"
K_DATA, K_ACK, K_DATA_NOACK = 1, 2, 3
DATA_HDR = struct.Struct(">2sBHBBH")  # magic, kind, message id, index, count, payload length
ACK_HDR = struct.Struct(">2sBHBB")  # magic, kind, message id, count, bitmap length
CRC = struct.Struct(">I")
FRAG = 200  # payload bytes per packet (fits the packet size of common radios)
MAX_MESSAGE = 255 * FRAG
ACK_QUIET = 0.08  # the receiver acknowledges this long after the last packet
GIVE_UP = 90.0  # a message not through by then is dropped (the session retries)
MIN_RATE = 150.0  # bytes/s
QUEUE_MAX = 256


@dataclass
class LinkStats:
    bytes_out: int = 0
    bytes_in: int = 0
    packets_in: int = 0
    bad_packets: int = 0  # CRC errors or garbage (wrong baud rate, noise)
    junk_bytes: int = 0  # bytes that weren't part of any packet
    resent: int = 0
    messages_out: int = 0
    messages_in: int = 0
    given_up: int = 0
    last_rx: float = 0.0  # time.time() of the last good packet
    rate: float = 0.0  # current pacing rate, bytes/s

    def to_dict(self) -> dict[str, float | int]:
        return dict(self.__dict__)


@dataclass
class _Incoming:
    count: int
    parts: dict[int, bytes] = field(default_factory=dict)
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
        self.stats = LinkStats(rate=self.max_rate * 0.7)
        self._buf = bytearray()
        self._queue: asyncio.Queue[tuple[bytes, bool]] = asyncio.Queue(QUEUE_MAX)
        self._next_id = int(time.time()) & 0xFFFF
        self._acked: dict[int, set[int]] = {}
        self._ack_event = asyncio.Event()
        self._incoming: dict[int, _Incoming] = {}
        self._delivered: OrderedDict[int, int] = OrderedDict()  # message id -> count
        self._task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()

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
            for idx, part in enumerate(parts):
                await self._raw(self._data_packet(K_DATA_NOACK, msg_id, idx, count, part))
            self.stats.messages_out += 1
            return
        missing = set(range(count))
        self._acked[msg_id] = set()
        started = time.monotonic()
        first = True
        try:
            while missing and time.monotonic() - started < GIVE_UP:
                round_bytes = 0
                for idx in sorted(missing):
                    packet = self._data_packet(K_DATA, msg_id, idx, count, parts[idx])
                    await self._raw(packet)
                    round_bytes += len(packet)
                    if not first:
                        self.stats.resent += 1
                first = False
                # Wait for the bitmap (the receiver answers ACK_QUIET after our
                # last packet; the radio adds its own delay).
                self._ack_event.clear()
                wait = min(4.0, 0.6 + round_bytes / self.stats.rate * 0.25)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wait_ack(msg_id, missing), wait)
                before = len(missing)
                missing -= self._acked.get(msg_id, set())
                if not missing:
                    break
                if len(missing) == before:  # nothing new got through: back off harder
                    self.stats.rate = max(MIN_RATE, self.stats.rate * 0.5)
                else:
                    self.stats.rate = max(MIN_RATE, self.stats.rate * 0.75)
            if missing:
                self.stats.given_up += 1
            else:
                self.stats.messages_out += 1
                self.stats.rate = min(self.max_rate, self.stats.rate * 1.15)
        finally:
            self._acked.pop(msg_id, None)

    async def _wait_ack(self, msg_id: int, missing: set[int]) -> None:
        while True:
            await self._ack_event.wait()
            self._ack_event.clear()
            if missing <= self._acked.get(msg_id, set()):
                return
            # A partial bitmap: send the rest now rather than waiting out the timer.
            if self._acked.get(msg_id):
                return

    # ── Receiving ──

    async def feed(self, data: bytes) -> None:
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
            if kind in (K_DATA, K_DATA_NOACK):
                if len(buf) < DATA_HDR.size:
                    return
                _m, _k, msg_id, idx, count, ln = DATA_HDR.unpack_from(buf)
                total = DATA_HDR.size + ln + CRC.size
                if ln > FRAG or count == 0 or idx >= count:
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
                if blen > 32:
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
            self.stats.bad_packets += 1

    def _bad(self, buf: bytearray) -> None:
        self.stats.bad_packets += 1
        del buf[:1]  # resync on the next magic

    def _good(self) -> None:
        self.stats.packets_in += 1
        self.stats.last_rx = time.time()

    async def _on_data(self, kind: int, msg_id: int, idx: int, count: int, payload: bytes) -> None:
        if msg_id in self._delivered:  # a resend of something we have: confirm again
            if kind == K_DATA:
                self._schedule_ack(msg_id)
            return
        inc = self._incoming.get(msg_id)
        if inc is None or inc.count != count:
            inc = self._incoming[msg_id] = _Incoming(count)
            while len(self._incoming) > 8:  # abandoned partial messages
                old = next(iter(self._incoming))
                stale = self._incoming.pop(old)
                if stale.ack_handle:
                    stale.ack_handle.cancel()
        inc.parts[idx] = payload
        if len(inc.parts) == count:
            self._incoming.pop(msg_id, None)
            if inc.ack_handle:
                inc.ack_handle.cancel()
            self._delivered[msg_id] = count
            while len(self._delivered) > 512:
                self._delivered.popitem(last=False)
            if kind == K_DATA:
                await self._send_ack(msg_id)
            self.stats.messages_in += 1
            await self._deliver(b"".join(inc.parts[i] for i in range(count)))
        elif kind == K_DATA:
            self._schedule_ack(msg_id)

    def _schedule_ack(self, msg_id: int) -> None:
        loop = asyncio.get_running_loop()
        inc = self._incoming.get(msg_id)
        if inc is not None:
            if inc.ack_handle:
                inc.ack_handle.cancel()
            inc.ack_handle = loop.call_later(
                ACK_QUIET, lambda: asyncio.ensure_future(self._send_ack(msg_id))
            )
        else:
            asyncio.ensure_future(self._send_ack(msg_id))

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
            async with self._write_lock:  # (acks aren't paced: they're tiny)
                await self._write(packet)
            self.stats.bytes_out += len(packet)
        except Exception:  # noqa: BLE001 - the transport notices a dead line
            return
