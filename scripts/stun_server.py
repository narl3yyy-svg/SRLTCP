#!/usr/bin/env python3
"""Minimal STUN server (RFC 5389 Binding only), for tests and for anyone who
wants their own instead of Google's or Cloudflare's.

    python scripts/stun_server.py [--host 0.0.0.0] [--port 3478]

Answers each Binding request with the address it came from
(XOR-MAPPED-ADDRESS). Stores nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import struct

MAGIC = 0x2112A442


def answer(data: bytes, addr: tuple[str, int]) -> bytes | None:
    if len(data) < 20 or data[0:2] != b"\x00\x01":
        return None
    if struct.unpack(">I", data[4:8])[0] != MAGIC:
        return None
    txid = data[8:20]
    ip = ipaddress.ip_address(addr[0])
    port = addr[1] ^ (MAGIC >> 16)
    if ip.version == 4:
        raw = bytes(a ^ b for a, b in zip(ip.packed, struct.pack(">I", MAGIC), strict=True))
        value = struct.pack(">BBH", 0, 1, port) + raw
    else:
        key = struct.pack(">I", MAGIC) + txid
        raw = bytes(a ^ b for a, b in zip(ip.packed, key, strict=True))
        value = struct.pack(">BBH", 0, 2, port) + raw
    attr = struct.pack(">HH", 0x0020, len(value)) + value
    return struct.pack(">HHI", 0x0101, len(attr), MAGIC) + txid + attr


class Stun(asyncio.DatagramProtocol):
    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        reply = answer(data, addr)
        if reply:
            self.transport.sendto(reply, addr)  # type: ignore[attr-defined]


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=3478)
    a = p.parse_args()
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(Stun, local_addr=(a.host, a.port))
    print(f"STUN on {a.host}:{a.port}", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
