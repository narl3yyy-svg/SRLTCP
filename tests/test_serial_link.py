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
    """Wrong baud rate or noise: counted as bad packets, never delivered."""
    a, b, got = await _pair(loss=0.0, corrupt=0.0)
    try:
        await b.feed(os.urandom(4000))
        await a.send(b"after the noise")
        assert await _until(lambda: got[1] == [b"after the noise"], 10)
        assert b.stats.bad_packets > 0
    finally:
        await a.stop()
        await b.stop()
