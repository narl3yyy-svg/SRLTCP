"""Two full SRLTCP nodes over simulated radio modems (scripts/radio_sim.py):
time to connect, file speed, chat delay (idle and during a transfer), ping.

Usage: .venv/bin/python scripts/bench_radio.py [radio ...] [--kb N]
       radios: cable sik sik-ecc lora noisy (default: all)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import srltcp.core  # noqa: E402,F401  (import order: core before transports)
from scripts.radio_sim import PORTS, RADIOS, RadioBridge  # noqa: E402
from srltcp.core.trusted import TrustedPeer  # noqa: E402
from srltcp.transports import android_serial  # noqa: E402


async def wait_for(cond: Any, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.05)
    return bool(cond())


def on_radio(x: Any, y: Any) -> bool:
    link = x.get_link(y.identities["tcp"].hash_id)
    return bool(link and link.up and link.transport == "serial")


async def chat_delay(src: Any, dst: Any, text: str, timeout: float = 60) -> float | None:
    started = time.monotonic()
    await src.send_message(dst.identities["tcp"].hash_id, text)
    ok = await wait_for(lambda: any(m["text"] == text for m in dst.get_messages()), timeout)
    return round(time.monotonic() - started, 2) if ok else None


async def run(radio: str, kb: int, *, hotplug: bool = False, seed: int = 7) -> dict[str, Any]:
    from tests.test_e2e_nodes import make_node

    bridge = RadioBridge(radio, plugged=(True, not hotplug), seed=seed)
    android_serial._bridge = lambda: bridge  # type: ignore[assignment]
    tmp = Path(tempfile.mkdtemp())
    baud = RADIOS[radio].baud
    a = make_node(tmp, "alice", enable_serial=True, serial_port=PORTS[0], serial_baud=baud,
                  router_port_mapping=False)
    b = make_node(tmp, "bob", enable_serial=True, serial_port=PORTS[1], serial_baud=baud,
                  router_port_mapping=False)
    out: dict[str, Any] = {"radio": radio}
    await a.start()
    await b.start()
    try:
        for x, y in ((a, b), (b, a)):
            ident = y.identities["tcp"]
            x.trusted.add(TrustedPeer(hash_id=ident.hash_id, name=ident.name,
                                      public_key=ident.public_bytes().hex()))
        ah, bh = a.identities["tcp"].hash_id, b.identities["tcp"].hash_id
        t0 = time.monotonic()
        if hotplug:
            # a's radio is open; b's adapter is plugged in now (the app runs).
            await asyncio.sleep(1)
            t0 = time.monotonic()
            bridge.plug(PORTS[1])
            ticks = [a, b]
            # (the apps' own loops notice the adapter; nobody presses anything)
            ok = await wait_for(lambda: on_radio(a, b) and on_radio(b, a), 180)
            del ticks
        else:
            await a.radio_tick(now_open=True)
            await b.radio_tick(now_open=True)
            ok = await wait_for(lambda: on_radio(a, b) and on_radio(b, a), 180)
        out["connect_s"] = round(time.monotonic() - t0, 1) if ok else None
        if not ok:
            return out
        await asyncio.sleep(2)
        out["chat_idle_s"] = await chat_delay(a, b, "hello idle")
        src = tmp / "photo.bin"
        src.write_bytes(os.urandom(kb * 1024))
        started = time.monotonic()
        tid = (await a.send_file(bh, src))["id"]
        await wait_for(lambda: tid in b._transfers and b._transfers[tid].offset > 4096, 120)
        out["chat_busy_s"] = await chat_delay(b, a, "hello during the file")
        done = await wait_for(lambda: tid in b._transfers
                              and b._transfers[tid].state.value == "complete", 900)
        elapsed = time.monotonic() - started
        out["file_Bps"] = round(kb * 1024 / elapsed) if done else None
        out["line_Bps"] = baud // 10
        link = a.get_link(bh)
        out["ping_ms"] = round(link.rtt_ms) if link and link.rtt_ms else None
        stats = a.serial_transport.link_stats() if a.serial_transport else {}
        out["resent"] = stats.get("resent")
        out["air"] = bridge.stats()[PORTS[0]]
        del ah
        return out
    finally:
        await a.stop()
        await b.stop()
        bridge.stop()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("radios", nargs="*", default=list(RADIOS))
    parser.add_argument("--kb", type=int, default=64)
    parser.add_argument("--hotplug", action="store_true")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    for radio in args.radios:
        result = await run(radio, args.kb, hotplug=args.hotplug, seed=args.seed)
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
