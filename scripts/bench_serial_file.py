"""Measure a file transfer between two SRLTCP nodes over simulated serial radios.

The simulation is close to a pair of USB serial radio modems on phones:

- each phone writes to its adapter at the serial line's speed (57600 baud:
  5760 bytes/s; a write blocks while the adapter's buffer is full) and each
  write costs a little extra (USB transfer, the Android bridge);
- the radios carry the bytes with a small delay and lose a few frames;
- the receiving adapter hands bytes over in USB packets of at most 62 bytes,
  at the serial line's speed.

Usage: .venv/bin/python scripts/bench_serial_file.py [size_kb] [baud] [loss]
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import srltcp.core  # noqa: E402,F401  (import order: core before transports)
from srltcp.core.trusted import TrustedPeer  # noqa: E402
from srltcp.transports import android_serial  # noqa: E402

PORTS = ["usb:0403:6015#0", "usb:303a:1001#0"]


class RadioBridge:
    """Both phones' adapters and radios (stands in for UsbSerialBridge)."""

    def __init__(self, baud: int, loss: float, latency: float = 0.015,
                 write_cost: float = 0.002, seed: int = 7) -> None:
        self.byte_time = 10.0 / baud
        self.loss = loss
        self.latency = latency
        self.write_cost = write_cost
        self.rnd = random.Random(seed)
        self.gens: dict[int, str] = {}
        self.rx: dict[str, list[tuple[float, bytes]]] = {p: [] for p in PORTS}
        self.tx_free: dict[str, float] = dict.fromkeys(PORTS, 0.0)  # when each wire is free
        self.rx_free: dict[str, float] = dict.fromkeys(PORTS, 0.0)
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)

    def listPorts(self) -> str:  # noqa: N802 - Java names
        return json.dumps([{"device": p, "description": p, "accessible": True, "attached": i}
                           for i, p in enumerate(PORTS)])

    def open(self, port: str, baud: int, ask: bool) -> int:
        gen = len(self.gens) + 1
        self.gens[gen] = port
        return gen

    def getLastError(self) -> str:  # noqa: N802
        return ""

    def isOpen(self, gen: int) -> bool:  # noqa: N802
        return gen in self.gens

    def close(self, gen: int) -> None:
        self.gens.pop(gen, None)

    def write(self, gen: int, data: bytes, timeout_ms: int) -> None:
        me = self.gens[gen]
        other = next(p for p in PORTS if p != me)
        time.sleep(self.write_cost)
        with self.cond:
            now = time.monotonic()
            start = max(now, self.tx_free[me])
            done = start + len(data) * self.byte_time  # out of the adapter at line speed
            self.tx_free[me] = done
            for i in range(0, len(data), 64):  # radio frames
                frame = data[i : i + 64]
                if self.rnd.random() < self.loss:
                    continue
                sent = start + (i + len(frame)) * self.byte_time
                arrive = max(sent + self.latency, self.rx_free[other])
                arrive += len(frame) * self.byte_time  # into the other adapter at line speed
                self.rx_free[other] = arrive
                self.rx[other].append((arrive, bytes(frame)))
            self.cond.notify_all()
        # The adapter buffers ~512 bytes: a write returns once the rest fits.
        wait = done - time.monotonic() - 512 * self.byte_time
        if wait > 0:
            time.sleep(wait)

    def read(self, gen: int, size: int, timeout_ms: int) -> bytes:
        me = self.gens[gen]
        deadline = time.monotonic() + timeout_ms / 1000
        with self.cond:
            while True:
                now = time.monotonic()
                ready = self.rx[me]
                if ready and ready[0][0] <= now:
                    out = bytearray()
                    while ready and ready[0][0] <= now and len(out) < 62:
                        arrive, frame = ready[0]
                        take = frame[: 62 - len(out)]
                        out += take
                        if len(take) == len(frame):
                            ready.pop(0)
                        else:
                            ready[0] = (arrive, frame[len(take):])
                    return bytes(out)
                if now >= deadline:
                    return b""
                wake = deadline if not ready else min(deadline, ready[0][0])
                self.cond.wait(max(0.0005, wake - now))


async def run(size_kb: int, baud: int, loss: float) -> dict[str, Any]:
    from tests.test_e2e_nodes import make_node, wait_for

    bridge = RadioBridge(baud, loss)
    android_serial._bridge = lambda: bridge  # type: ignore[assignment]
    tmp = Path(tempfile.mkdtemp())
    a = make_node(tmp, "alice", enable_serial=True, serial_port=PORTS[0], serial_baud=baud,
                  router_port_mapping=False)
    b = make_node(tmp, "bob", enable_serial=True, serial_port=PORTS[1], serial_baud=baud,
                  router_port_mapping=False)
    await a.start()
    await b.start()
    try:
        for x, y in ((a, b), (b, a)):
            ident = y.identities["tcp"]
            x.trusted.add(TrustedPeer(hash_id=ident.hash_id, name=ident.name,
                                      public_key=ident.public_bytes().hex()))
        ah, bh = a.identities["tcp"].hash_id, b.identities["tcp"].hash_id
        t0 = time.monotonic()
        await a.send_radio_beacon()
        ok = await wait_for(lambda: (la := a.get_link(bh)) is not None and la.up
                            and (lb := b.get_link(ah)) is not None and lb.up, timeout=120)
        connect_s = time.monotonic() - t0
        if not ok:
            return {"error": "no session"}
        await asyncio.sleep(3)  # (link-up chatter settles)
        src = tmp / "file.bin"
        src.write_bytes(os.urandom(size_kb * 1024))
        pings: list[float] = []
        t1 = time.monotonic()
        result = await a.send_file(bh, src)
        assert result
        tid = result["id"]

        async def watch_ping() -> None:
            while True:
                await asyncio.sleep(2)
                link = a.get_link(bh)
                if link and link.rtt_ms:
                    pings.append(link.rtt_ms)

        watcher = asyncio.create_task(watch_ping())
        done = await wait_for(lambda: b._transfers.get(tid) is not None
                              and b._transfers[tid].state.value == "complete", timeout=900)
        secs = time.monotonic() - t1
        watcher.cancel()
        assert a.serial_transport and b.serial_transport
        return {
            "complete": done, "connect_s": round(connect_s, 1), "file_s": round(secs, 1),
            "payload_Bps": round(size_kb * 1024 / secs), "line_Bps": round(baud / 10),
            "ping_ms_max": round(max(pings)) if pings else None,
            "a": a.serial_transport.link_stats(), "b": b.serial_transport.link_stats(),
        }
    finally:
        await a.stop()
        await b.stop()


if __name__ == "__main__":
    args = sys.argv[1:]
    out = asyncio.run(run(int(args[0]) if args else 64, int(args[1]) if len(args) > 1 else 57600,
                          float(args[2]) if len(args) > 2 else 0.03))
    for side in ("a", "b"):
        if side in out:
            s = out[side]
            out[side] = {k: (round(v) if isinstance(v, float) else v) for k, v in s.items()
                         if k in ("bytes_out", "bytes_in", "resent", "bad_packets", "messages_out",
                                  "messages_in", "given_up", "rate", "rtt")}
    print(json.dumps(out, indent=1))
