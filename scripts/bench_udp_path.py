"""Direct UDP speed over an emulated mobile path (no root, no netem needed).

    .venv/bin/python scripts/bench_udp_path.py                 # all profiles
    .venv/bin/python scripts/bench_udp_path.py lte-lossy --mb 20

Each direction of the path has a bottleneck rate, a drop-tail buffer (radio
networks have deep ones), a base one-way delay, jitter and random loss, like
a phone on mobile data talking to a home Wi-Fi. Prints throughput, the round
trip a small message sees during the transfer, and loss/retransmissions.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import secrets
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import contextlib

from srltcp.core.protocol.framing import pack_frame  # noqa: E402
from srltcp.transports.udp import UdpEndpoint, derive_keys  # noqa: E402


@dataclass
class Path_:
    rate_mbit: float  # bottleneck
    buffer_ms: float  # drop-tail buffer, in time at the bottleneck rate
    delay_ms: float  # one way
    jitter_ms: float  # extra random delay per packet (keeps order)
    loss: float  # random loss (radio), on top of buffer overflow


PROFILES = {
    # name: (phone -> home, home -> phone)
    "lan": (Path_(200, 20, 1, 0, 0), Path_(200, 20, 1, 0, 0)),
    "lte": (Path_(20, 300, 45, 15, 0.002), Path_(40, 300, 45, 15, 0.002)),
    "lte-lossy": (Path_(20, 400, 70, 30, 0.01), Path_(40, 400, 70, 30, 0.01)),
    "5g-far": (Path_(60, 200, 100, 10, 0.005), Path_(100, 200, 100, 10, 0.005)),
    "congested": (Path_(5, 1000, 50, 40, 0.02), Path_(8, 1000, 50, 40, 0.02)),
}


class Link:
    """One direction: serialisation at the bottleneck, buffer, delay, loss."""

    def __init__(self, path: Path_, seed: int) -> None:
        self.p = path
        self.rnd = random.Random(seed)
        self.busy_until = 0.0
        self.last_arrival = 0.0
        self.dropped = 0

    def wrap(self, ep: UdpEndpoint) -> None:
        real = ep.sendto
        loop = asyncio.get_running_loop()
        rate = self.p.rate_mbit * 1e6 / 8

        def sendto(packet: bytes, addr: tuple[str, int], socks: Any = None) -> None:
            now = time.monotonic()
            start = max(now, self.busy_until)
            if start - now > self.p.buffer_ms / 1000:
                self.dropped += 1
                return  # buffer full
            self.busy_until = start + (len(packet) + 28) / rate
            if self.rnd.random() < self.p.loss:
                return
            arrive = self.busy_until + self.p.delay_ms / 1000
            arrive += self.rnd.random() * self.p.jitter_ms / 1000
            arrive = max(arrive, self.last_arrival + 1e-6)  # radio links keep order
            self.last_arrival = arrive
            loop.call_at(loop.time() + (arrive - now), real, packet, addr, socks)

        ep.sendto = sendto  # type: ignore[method-assign]


async def run(name: str, mb: float) -> dict[str, Any]:
    up, down = PROFILES[name]
    a, b = UdpEndpoint(0, stun_servers=()), UdpEndpoint(0, stun_servers=())
    await a.start()
    await b.start()
    Link(up, 1).wrap(a)
    Link(down, 2).wrap(b)
    k1, k2 = derive_keys(secrets.token_bytes(32), "leader", "follower")
    ida, idb = secrets.token_bytes(8), secrets.token_bytes(8)
    ca = a.new_connection(idb, k1, k2, my_id=ida)
    cb = b.new_connection(ida, k2, k1, my_id=idb)
    assert ca and cb
    pa, pb = ("127.0.0.1", a.ports[socket.AF_INET]), ("127.0.0.1", b.ports[socket.AF_INET])
    ok = await asyncio.gather(a.punch(ca, [pb], 5.0), b.punch(cb, [pa], 5.0))
    assert ok == [True, True], ok
    got = 0
    pings: dict[bytes, float] = {}
    rtts: list[float] = []
    done = asyncio.Event()
    total = int(mb * 1024 * 1024)

    async def on_b(_p: Any, frame: bytes) -> None:
        nonlocal got
        if frame[:1] == b"P":
            await cb.send(b"Q" + frame[1:])
            return
        got += len(frame)
        if got >= total:
            done.set()

    async def on_a(_p: Any, frame: bytes) -> None:
        if frame[:1] == b"Q" and frame[1:] in pings:
            rtts.append(time.monotonic() - pings.pop(frame[1:]))

    ca.set_frame_handler(on_a)
    cb.set_frame_handler(on_b)
    await ca.start_reading()
    await cb.start_reading()

    async def chat() -> None:  # a message every half second during the file
        while not done.is_set():
            tag = secrets.token_bytes(6)
            pings[tag] = time.monotonic()
            await ca.send(b"P" + tag)
            await asyncio.sleep(0.5)

    start = time.monotonic()
    chatter = asyncio.create_task(chat())
    chunk = b"\0" * (256 * 1024 - 8)
    sent = 0
    while sent < total:
        await ca.send_framed(pack_frame(chunk))
        sent += len(chunk)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(done.wait(), 300)
    took = time.monotonic() - start
    chatter.cancel()
    rtts.sort()
    res = {
        "profile": name,
        "MB/s": round(got / took / 1e6, 2),
        "path_MB/s": round(up.rate_mbit / 8, 2),
        "secs": round(took, 1),
        "chat_rtt_p50_ms": round(1000 * rtts[len(rtts) // 2]) if rtts else None,
        "chat_rtt_p90_ms": round(1000 * rtts[int(len(rtts) * 0.9)]) if rtts else None,
        "chat_msgs": len(rtts),
        "base_rtt_ms": round(up.delay_ms + down.delay_ms),
        "lost": ca.stats["lost"],
        "sent": ca.stats["sent"],
        "cwnd_kB": round(ca._cwnd / 1000),
        "srtt_ms": round(ca._rtt.srtt * 1000),
    }
    await ca.close()
    await cb.close()
    a.stop()
    b.stop()
    return res


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("profiles", nargs="*", default=list(PROFILES))
    ap.add_argument("--mb", type=float, default=16)
    args = ap.parse_args()
    for name in args.profiles:
        print(await run(name, args.mb), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
