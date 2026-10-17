"""The reliable serial link over a lossy, slow radio (simulated)."""

from __future__ import annotations

import asyncio
import os
import random

import srltcp.core  # noqa: F401  (import order: core before transports)
from srltcp.transports.serial_link import SerialLink


class Radio:
    """Two serial radios over the air: bytes arrive in packets, some get
    corrupted or lost, throughput is limited."""

    def __init__(self, loss: float, corrupt: float, rate: float, seed: int) -> None:
        self.loss, self.corrupt, self.rate = loss, corrupt, rate
        self.rnd = random.Random(seed)
        self.ends: list[SerialLink] = []

    def writer(self, to: int):  # type: ignore[no-untyped-def]
        async def write(data: bytes) -> None:
            await asyncio.sleep(len(data) / self.rate)
            for i in range(0, len(data), 64):  # the radio sends in small air packets
                chunk = bytearray(data[i : i + 64])
                if self.rnd.random() < self.loss:
                    continue
                if self.rnd.random() < self.corrupt:
                    chunk[self.rnd.randrange(len(chunk))] ^= 0x5A
                asyncio.get_running_loop().call_later(
                    0.02, lambda c=bytes(chunk): asyncio.ensure_future(self.ends[to].feed(c))
                )
        return write


async def _pair(loss: float, corrupt: float, rate: float = 6000.0, seed: int = 1):  # type: ignore[no-untyped-def]
    radio = Radio(loss, corrupt, rate, seed)
    got: list[list[bytes]] = [[], []]

    def deliver(i: int):  # type: ignore[no-untyped-def]
        async def d(msg: bytes) -> None:
            got[i].append(msg)
        return d

    a = SerialLink(radio.writer(1), deliver(0), 57600)
    b = SerialLink(radio.writer(0), deliver(1), 57600)
    radio.ends = [a, b]
    a.start()
    b.start()
    return a, b, got


async def _until(cond, timeout: float) -> bool:  # type: ignore[no-untyped-def]
    for _ in range(int(timeout / 0.05)):
        if cond():
            return True
        await asyncio.sleep(0.05)
    return cond()


async def test_handshake_sized_message_crosses_a_lossy_radio() -> None:
    a, b, got = await _pair(loss=0.05, corrupt=0.05)
    try:
        hello = os.urandom(9100)  # (the quantum-safe handshake is ~9 KiB)
        await a.send(hello)
        reply = os.urandom(9100)
        await b.send(reply)
        assert await _until(lambda: got[1] == [hello] and got[0] == [reply], 60)
        assert a.stats.resent > 0 and b.stats.bad_packets + a.stats.bad_packets > 0
    finally:
        await a.stop()
        await b.stop()


async def test_messages_arrive_once_and_in_order() -> None:
    a, b, got = await _pair(loss=0.03, corrupt=0.02, seed=7)
    try:
        msgs = [f"message {i} ".encode() * (1 + i % 5) for i in range(30)]
        for m in msgs:
            await a.send(m)
        assert await _until(lambda: len(got[1]) == len(msgs), 60)
        assert got[1] == msgs
    finally:
        await a.stop()
        await b.stop()


async def test_broadcasts_go_once_without_waiting_for_anyone() -> None:
    a, b, got = await _pair(loss=0.0, corrupt=0.0)
    try:
        await a.send(b"announce", reliable=False)
        assert await _until(lambda: got[1] == [b"announce"], 5)
        await asyncio.sleep(0.5)
        assert a.stats.resent == 0 and got[1] == [b"announce"]
    finally:
        await a.stop()
        await b.stop()


async def test_garbage_on_the_line_is_skipped() -> None:
    """Wrong baud rate or noise: skipped as junk, never delivered."""
    a, b, got = await _pair(loss=0.0, corrupt=0.0)
    try:
        await b.feed(os.urandom(4000))
        await a.send(b"after the noise")
        assert await _until(lambda: got[1] == [b"after the noise"], 10)
        assert b.stats.junk_bytes >= 3900
    finally:
        await a.stop()
        await b.stop()


class HalfDuplexRadio:
    """Closer to real radio modems: the air is slower than the serial line,
    each radio has a small buffer that overflows, frames go one way at a time
    (two transmissions at once destroy each other) and a few are lost."""

    def __init__(self, air_rate: float, buffer: int, loss: float, seed: int) -> None:
        self.air_rate, self.buffer, self.loss = air_rate, buffer, loss
        self.rnd = random.Random(seed)
        self.ends: list[SerialLink] = []
        self.tx: list[bytearray] = [bytearray(), bytearray()]
        self.busy: list[tuple[float, float]] = [(0.0, 0.0), (0.0, 0.0)]  # last air time
        self.overflow = 0
        self.collisions = 0
        self.tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        self.tasks = [asyncio.create_task(self._air(i)) for i in (0, 1)]

    def stop(self) -> None:
        for t in self.tasks:
            t.cancel()

    def writer(self, me: int):  # type: ignore[no-untyped-def]
        async def write(data: bytes) -> None:
            await asyncio.sleep(len(data) / 5760)  # 57600 baud serial line
            room = self.buffer - len(self.tx[me])
            if room < len(data):
                self.overflow += len(data) - max(room, 0)
            self.tx[me] += data[: max(room, 0)]
        return write

    async def _air(self, me: int) -> None:
        loop = asyncio.get_running_loop()
        other = 1 - me
        while True:
            if not self.tx[me]:
                await asyncio.sleep(0.005)
                continue
            frame = bytes(self.tx[me][:64])
            del self.tx[me][:64]
            start = loop.time()
            await asyncio.sleep(len(frame) / self.air_rate)
            end = loop.time()
            self.busy[me] = (start, end)
            o_start, o_end = self.busy[other]
            if o_start < end and o_end > start:
                self.collisions += 1
                continue  # both on the air at once
            if self.rnd.random() < self.loss:
                continue
            loop.call_later(0.03, lambda f=frame: asyncio.ensure_future(self.ends[other].feed(f)))


async def test_both_sides_talking_at_once_over_a_half_duplex_radio() -> None:
    """Each side sends a handshake at the same moment over a slow,
    half-duplex radio with a 1 KiB buffer: both get through (listening
    before talking, pacing at the radio's real rate)."""
    radio = HalfDuplexRadio(air_rate=2400, buffer=1024, loss=0.02, seed=5)
    got: list[list[bytes]] = [[], []]

    def deliver(i: int):  # type: ignore[no-untyped-def]
        async def d(msg: bytes) -> None:
            got[i].append(msg)
        return d

    a = SerialLink(radio.writer(0), deliver(0), 57600)
    b = SerialLink(radio.writer(1), deliver(1), 57600)
    radio.ends = [a, b]
    radio.start()
    a.start()
    b.start()
    try:
        hello, reply = os.urandom(9100), os.urandom(9100)
        await a.send(hello)
        await b.send(reply)
        assert await _until(lambda: got[1] == [hello] and got[0] == [reply], 60)
        assert a.stats.given_up == 0 and b.stats.given_up == 0
    finally:
        radio.stop()
        await a.stop()
        await b.stop()


async def test_datagrams_go_at_once_and_unacknowledged() -> None:
    """Call audio: one packet each, never acknowledged or resent, and a
    datagram too large for one packet is refused (sent reliably instead)."""
    a, b, got = await _pair(loss=0.0, corrupt=0.0)
    try:
        assert a.send_datagram(b"voice" * 40)
        assert not a.send_datagram(b"x" * 256)
        assert await _until(lambda: got[1] == [b"voice" * 40], 5)
        await asyncio.sleep(0.3)
        assert a.stats.datagrams_out == 1 and b.stats.datagrams_in == 1
        assert a.stats.messages_out == 0 and b.stats.bytes_out == 0  # no answer at all
    finally:
        await a.stop()
        await b.stop()


async def test_urgent_messages_overtake_queued_file_data() -> None:
    """A chat message sent while file data is queued goes out before it."""
    a, b, got = await _pair(loss=0.0, corrupt=0.0)
    try:
        for i in range(6):
            await a.send(bytes([i]) * 2048, bulk=True)
        await a.send(b"chat")
        assert await _until(lambda: len(got[1]) == 7, 30)
        assert got[1].index(b"chat") <= 1  # (behind the piece already on the air, at most)
    finally:
        await a.stop()
        await b.stop()


async def test_cancel_bulk_drops_queued_file_data() -> None:
    """A cancelled file's queued pieces never go out (they held the radio for
    minutes, and pings waited behind them)."""
    a, b, got = await _pair(loss=0.0, corrupt=0.0)
    try:
        for i in range(8):
            await a.send(bytes([i]) * 2048, bulk=True, tag="file-1")
        await a.send(b"other", bulk=True, tag="file-2")
        await asyncio.sleep(0.05)  # (the first piece is on its way)
        dropped = a.cancel_bulk("file-1")
        assert dropped >= 6
        await a.send(b"chat")
        assert await _until(lambda: b"chat" in got[1] and b"other" in got[1], 20)
        await asyncio.sleep(0.5)
        assert sum(1 for m in got[1] if len(m) == 2048) <= 2
    finally:
        await a.stop()
        await b.stop()


async def test_data_queued_after_a_cancel_still_goes_out() -> None:
    """A rewound or resumed file reuses its tag: only what was queued before
    the cancel is dropped, the resent data goes out."""
    a, b, got = await _pair(loss=0.0, corrupt=0.0)
    try:
        for i in range(4):
            await a.send(bytes([i]) * 1024, bulk=True, tag="file-1")
        a.cancel_bulk("file-1")
        await a.send(b"resent from the last confirmed byte", bulk=True, tag="file-1")
        assert await _until(lambda: b"resent from the last confirmed byte" in got[1], 15)
    finally:
        await a.stop()
        await b.stop()
