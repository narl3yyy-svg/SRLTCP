"""The serial link layer alone over simulated radios (scripts/radio_sim.py):
bulk throughput one way, and how long a small urgent message takes meanwhile.

Usage: .venv/bin/python scripts/bench_link.py [radio ...] [--kb N] [--seed S]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import srltcp.core  # noqa: E402,F401  (import order: core before transports)
from scripts.radio_sim import PORTS, RADIOS, RadioBridge  # noqa: E402
from srltcp.transports.serial_link import SerialLink  # noqa: E402


class End:
    """One phone: the link layer on one adapter of the bridge."""

    def __init__(self, bridge: RadioBridge, port: str, loop: asyncio.AbstractEventLoop) -> None:
        self.bridge, self.loop = bridge, loop
        self.gen = bridge.open(port, bridge.radio.baud, False)
        self.got: list[tuple[float, bytes]] = []
        self.link = SerialLink(self._write, self._deliver, bridge.radio.baud)
        self._stop = False
        threading.Thread(target=self._reader, daemon=True).start()

    async def _write(self, data: bytes) -> None:
        await asyncio.to_thread(self.bridge.write, self.gen, data, 1000)

    async def _deliver(self, msg: bytes) -> None:
        self.got.append((time.monotonic(), msg))

    def _reader(self) -> None:
        while not self._stop:
            try:
                data = self.bridge.read(self.gen, 4096, 100)
            except OSError:
                return
            if data:
                asyncio.run_coroutine_threadsafe(self.link.feed(data), self.loop)

    async def stop(self) -> None:
        self._stop = True
        await self.link.stop()


async def run(radio: str, kb: int, seed: int, chunk_kb: int = 24) -> dict[str, object]:
    loop = asyncio.get_running_loop()
    bridge = RadioBridge(radio, seed=seed)
    a, b = End(bridge, PORTS[0], loop), End(bridge, PORTS[1], loop)
    a.link.start()
    b.link.start()
    try:
        chunk = chunk_kb * 1024
        total = kb * 1024
        started = time.monotonic()
        for i in range(max(1, total // chunk)):
            await a.link.send(i.to_bytes(4, "big") + os.urandom(chunk - 4), bulk=True)
        delays = []
        for i in range(6):  # small urgent messages both ways during the bulk
            await asyncio.sleep(1.5)
            sent = time.monotonic()
            tag = b"ping%d" % i
            await (b if i % 2 else a).link.send(tag)
            target = a if i % 2 else b
            for _ in range(600):
                if any(m == tag for _t, m in target.got):
                    delays.append(round(time.monotonic() - sent, 2))
                    break
                await asyncio.sleep(0.02)
        want = max(1, total // chunk)
        for _ in range(int(600 / 0.05)):
            if sum(1 for _t, m in b.got if len(m) == chunk) >= want:
                break
            await asyncio.sleep(0.05)
        done = [t for t, m in b.got if len(m) == chunk]
        elapsed = (max(done) if len(done) >= want else time.monotonic()) - started
        s = a.link.stats
        return {
            "radio": radio, "Bps": round(len(done) * chunk / elapsed),
            "line": bridge.radio.baud // 10, "complete": len(done) >= want,
            "urgent_s": delays, "rate": round(s.rate), "rtt": round(s.rtt, 2),
            "resent": s.resent, "given_up": s.given_up, "air": bridge.stats()[PORTS[0]],
        }
    finally:
        await a.stop()
        await b.stop()
        bridge.stop()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("radios", nargs="*", default=list(RADIOS))
    parser.add_argument("--kb", type=int, default=64)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--chunk", type=int, default=24, help="KiB per message (a file piece)")
    args = parser.parse_args()
    for radio in args.radios:
        print(json.dumps(await run(radio, args.kb, args.seed, args.chunk)), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
