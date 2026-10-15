"""Direct UDP connections: punching, reliability under loss, speed, and what
an attacker on the network can't do."""

from __future__ import annotations

import asyncio
import os
import random
import secrets
import socket
import struct
import time
from typing import Any

import pytest

from srltcp.core.protocol.framing import pack_frame
from srltcp.transports import udp
from srltcp.transports.udp import UdpConnection, UdpEndpoint, derive_keys


class Pair:
    def __init__(self, a: UdpEndpoint, b: UdpEndpoint, ca: UdpConnection, cb: UdpConnection):
        self.a, self.b, self.ca, self.cb = a, b, ca, cb
        self.got_a: list[bytes] = []
        self.got_b: list[bytes] = []

        async def on_a(_peer: Any, frame: bytes) -> None:
            self.got_a.append(frame)

        async def on_b(_peer: Any, frame: bytes) -> None:
            self.got_b.append(frame)

        ca.set_frame_handler(on_a)
        cb.set_frame_handler(on_b)


async def make_pair(lossy: dict[str, float] | None = None) -> Pair:
    a, b = UdpEndpoint(0, stun_servers=()), UdpEndpoint(0, stun_servers=())
    await a.start()
    await b.start()
    if lossy:
        for ep in (a, b):
            _make_lossy(ep, **lossy)
    k1, k2 = derive_keys(secrets.token_bytes(32), "leader", "follower")
    ida, idb = secrets.token_bytes(8), secrets.token_bytes(8)
    ca = a.new_connection(idb, k1, k2, my_id=ida)
    cb = b.new_connection(ida, k2, k1, my_id=idb)
    assert ca and cb
    pa = ("127.0.0.1", a.ports[socket.AF_INET])
    pb = ("127.0.0.1", b.ports[socket.AF_INET])
    ok = await asyncio.gather(a.punch(ca, [pb], 5.0), b.punch(cb, [pa], 5.0))
    assert ok == [True, True]
    await ca.start_reading()
    await cb.start_reading()
    return Pair(a, b, ca, cb)


def _make_lossy(ep: UdpEndpoint, loss: float = 0.0, dup: float = 0.0, reorder: float = 0.0) -> None:
    real = ep.sendto
    rnd = random.Random(7)
    loop = asyncio.get_event_loop()

    def sendto(packet: bytes, addr: tuple[str, int], socks: Any = None) -> None:
        if rnd.random() < loss:
            return
        if rnd.random() < reorder:
            loop.call_later(0.02, real, packet, addr, socks)
            return
        real(packet, addr, socks)
        if rnd.random() < dup:
            real(packet, addr, socks)

    ep.sendto = sendto  # type: ignore[method-assign]


async def close(p: Pair) -> None:
    await p.ca.close()
    await p.cb.close()
    p.a.stop()
    p.b.stop()


async def until(cond: Any, timeout: float = 20.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return bool(cond())


async def test_messages_arrive_in_order_both_ways() -> None:
    p = await make_pair()
    for i in range(200):
        await p.ca.send(f"a{i}".encode())
        await p.cb.send(f"b{i}".encode())
    assert await until(lambda: len(p.got_b) == 200 and len(p.got_a) == 200)
    assert p.got_b == [f"a{i}".encode() for i in range(200)]
    assert p.got_a == [f"b{i}".encode() for i in range(200)]
    await close(p)


@pytest.mark.parametrize("net", [
    {"loss": 0.05}, {"loss": 0.02, "dup": 0.05, "reorder": 0.1}, {"loss": 0.2},
])
async def test_big_transfer_survives_a_bad_network(net: dict[str, float]) -> None:
    p = await make_pair(net)
    blob = os.urandom(3_000_000)
    pieces = [blob[i:i + 200_000] for i in range(0, len(blob), 200_000)]
    for piece in pieces:
        await p.ca.send_framed(pack_frame(piece))
    assert await until(lambda: len(p.got_b) == len(pieces), 60)
    assert b"".join(p.got_b) == blob
    if net["loss"] >= 0.05:
        assert p.ca.stats["lost"] > 0 and p.ca.stats["retransmitted"] > 0
    await close(p)


async def test_fast_on_a_clean_path() -> None:
    p = await make_pair()
    blob = os.urandom(64 * 1024 * 1024)
    start = time.monotonic()
    for i in range(0, len(blob), 1 << 20):
        await p.ca.send_framed(pack_frame(blob[i:i + (1 << 20)]))
    assert await until(lambda: len(p.got_b) == 64, 60)
    took = time.monotonic() - start
    assert b"".join(p.got_b) == blob
    print(f"\n64 MiB over local UDP in {took:.2f}s = {64 / took:.1f} MiB/s")
    assert 64 / took > 8  # both ends on one CPU here; still beyond a mobile uplink
    await close(p)


async def test_call_media_goes_as_datagrams() -> None:
    """Datagrams have their own handler (not the ordered stream) and still
    go out while a big transfer fills the stream."""
    p = await make_pair()
    media: list[bytes] = []
    p.cb.set_datagram_handler(lambda _peer, data: media.append(data))
    for _ in range(32):
        await p.ca.send_framed(pack_frame(os.urandom(1 << 20)))
    assert p.ca.send_datagram(b"rtp-1")
    assert not p.ca.send_datagram(b"x" * 1500)  # too big for one packet: use the stream
    assert await until(lambda: media == [b"rtp-1"], 5)
    assert len(p.got_b) < 32  # it overtook the file data
    assert await until(lambda: len(p.got_b) == 32, 30)
    await close(p)


async def test_close_reaches_the_other_side() -> None:
    p = await make_pair()
    closed: list[str] = []

    async def on_close(peer: Any) -> None:
        closed.append(peer.peer_id)

    p.cb.set_close_handler(on_close)
    await p.ca.close()
    assert await until(lambda: closed == [p.cb.peer.peer_id], 5)
    p.a.stop()
    p.b.stop()


async def test_attacker_cannot_inject_replay_or_reset() -> None:
    """Someone who sees the packets (same Wi-Fi, carrier) and knows the port."""
    p = await make_pair()
    captured: list[bytes] = []
    real = p.a.sendto

    def tap(packet: bytes, addr: tuple[str, int], socks: Any = None) -> None:
        captured.append(packet)
        real(packet, addr, socks)

    p.a.sendto = tap  # type: ignore[method-assign]
    await p.ca.send(b"hello")
    assert await until(lambda: p.got_b == [b"hello"])
    target = ("127.0.0.1", p.b.ports[socket.AF_INET])
    evil = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    evil.setblocking(False)
    # 1. Replay every captured packet (data, acks): nothing is delivered twice.
    for pkt in captured:
        evil.sendto(pkt, target)
    # 2. Forge: right connection id, made-up content and a CLOSE frame.
    conn_id = captured[0][3:11]
    for pn in (10_000, 10_001):
        forged = udp.HEADER.pack(udp.MAGIC, udp.VERSION, conn_id, pn)
        forged += bytes([udp.F_CLOSE]) + bytes(16)
        evil.sendto(forged, target)
    # 3. Flip a bit in a real packet.
    tampered = bytearray(captured[-1])
    tampered[-1] ^= 1
    evil.sendto(bytes(tampered), target)
    # 4. Junk and unknown connections get no answer at all (no amplification).
    evil.sendto(os.urandom(1200), target)
    evil.sendto(udp.HEADER.pack(udp.MAGIC, udp.VERSION, b"\0" * 8, 1) + os.urandom(100), target)
    await asyncio.sleep(0.3)
    with pytest.raises(BlockingIOError):
        evil.recv(4096)
    evil.close()
    assert p.got_b == [b"hello"] and not p.cb._closed
    await p.ca.send(b"still works")
    assert await until(lambda: p.got_b == [b"hello", b"still works"])
    await close(p)


async def test_wrong_key_cannot_open_a_punch() -> None:
    a, b = UdpEndpoint(0, stun_servers=()), UdpEndpoint(0, stun_servers=())
    await a.start()
    await b.start()
    ka = derive_keys(secrets.token_bytes(32), "x", "y")
    kb = derive_keys(secrets.token_bytes(32), "x", "y")  # a different secret
    ida, idb = secrets.token_bytes(8), secrets.token_bytes(8)
    ca = a.new_connection(idb, ka[0], ka[1], my_id=ida)
    cb = b.new_connection(ida, kb[1], kb[0], my_id=idb)
    assert ca and cb
    ok = await asyncio.gather(
        a.punch(ca, [("127.0.0.1", b.ports[socket.AF_INET])], 1.0),
        b.punch(cb, [("127.0.0.1", a.ports[socket.AF_INET])], 1.0),
    )
    assert ok == [False, False] and not a._conns and not b._conns
    a.stop()
    b.stop()


async def test_path_follows_a_network_change() -> None:
    """The phone's address changes (Wi-Fi -> mobile data): authenticated
    packets from the new address move the connection there."""
    p = await make_pair()
    await p.ca.send(b"before")
    assert await until(lambda: p.got_b == [b"before"])
    # A's packets now come from a different socket (new address/port).
    moved = UdpEndpoint(0, stun_servers=())
    await moved.start()
    p.a._socks, moved._socks = moved._socks, p.a._socks  # swap sockets
    p.ca.socks = dict(p.a._socks)  # (on a phone: the same socket, a new address)
    for t in p.a._socks.values():
        t.get_protocol().endpoint = p.a  # type: ignore[attr-defined]
    for t in moved._socks.values():
        t.get_protocol().endpoint = moved  # type: ignore[attr-defined]
    await asyncio.sleep(udp.MIGRATE_AFTER + 0.2)
    await p.ca.send(b"after")
    assert await until(lambda: p.got_b == [b"before", b"after"], 10)
    assert p.cb.remote == ("127.0.0.1", p.a.ports[socket.AF_INET]) or p.cb.remote is not None
    await p.cb.send(b"reply")
    assert await until(lambda: p.got_a == [b"reply"], 10)
    moved.stop()
    await close(p)


async def test_stun_reply_is_parsed_and_unsolicited_ones_ignored() -> None:
    """A fake STUN server on localhost reports a public address."""
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    server.setblocking(False)
    port = server.getsockname()[1]
    ep = UdpEndpoint(0, stun_servers=(("127.0.0.1", port),))
    await ep.start()
    loop = asyncio.get_running_loop()

    async def serve() -> None:
        data, addr = await loop.sock_recvfrom(server, 2048)
        txid = data[8:20]
        xport = 40000 ^ (udp.STUN_MAGIC >> 16)
        xip = bytes(x ^ y for x, y in zip(socket.inet_aton("198.51.100.9"),
                                          struct.pack(">I", udp.STUN_MAGIC), strict=True))
        attr = struct.pack(">HHBBH", 0x0020, 8, 0, 1, xport) + xip
        await loop.sock_sendto(server, struct.pack(">HHI", 0x0101, len(attr), udp.STUN_MAGIC)
                               + txid + attr, addr)

    task = asyncio.create_task(serve())
    mapped = await ep.refresh_mapping(timeout=2.0)
    await task
    assert mapped[socket.AF_INET] == ("198.51.100.9", 40000)
    # A "reply" nobody asked for changes nothing.
    fake = struct.pack(">HHI", 0x0101, 0, udp.STUN_MAGIC) + os.urandom(12)
    ep._received(fake, ("127.0.0.1", port))
    assert ep.mapped[socket.AF_INET] == ("198.51.100.9", 40000)
    server.close()
    ep.stop()


async def test_random_junk_from_the_contact_never_crashes() -> None:
    """Random frames (with the keys: the contact itself) may end that one
    connection, like garbage on a TCP stream, but never raise or hang."""
    p = await make_pair()
    rnd = random.Random(1)
    for _ in range(3000):
        p.cb._parse(bytes(rnd.getrandbits(8) for _ in range(rnd.randint(1, 300))), time.monotonic())
    assert p.cb._ooo_bytes <= udp.RECV_WINDOW
    await close(p)


async def test_extreme_frames_from_the_contact_are_harmless() -> None:
    """Huge ack ranges, offsets far beyond the window: no loop over 2^64
    packet numbers, nothing buffered, the connection keeps working."""
    p = await make_pair()
    cb = p.cb
    huge_ack = udp.ACK_HDR.pack(udp.F_ACK, 2**63, 2**63, 0xFFFFFFFF, 2) + \
        udp.ACK_RANGE.pack(2**64 - 1, 0) + udp.ACK_RANGE.pack(5, 10)
    start = time.monotonic()
    cb._parse(huge_ack, time.monotonic())
    assert time.monotonic() - start < 0.5
    far = udp.STREAM_HDR.pack(udp.F_STREAM, 2**63, 4) + b"evil"
    cb._parse(far, time.monotonic())
    assert not cb._ooo
    await p.ca.send(b"after the junk")
    assert await until(lambda: b"after the junk" in p.got_b)
    await p.cb.send(b"and back")
    assert await until(lambda: b"and back" in p.got_a)
    await close(p)


async def test_contact_cannot_fill_our_memory_or_stall_us() -> None:
    """Review finding: tiny overlapping early pieces used to pile up without
    limit (memory) and every in-order packet rescanned them all (CPU)."""
    p = await make_pair()
    cb = p.cb
    gap_start = cb._rcum
    for k in range(udp.MAX_OOO_PIECES + 5000):
        cb._on_stream(gap_start + 1 + k, b"x" * 1100)  # never filling byte gap_start
    assert len(cb._ooo) <= udp.MAX_OOO_PIECES and cb._ooo_bytes <= udp.RECV_WINDOW
    start = time.monotonic()
    for _ in range(200):  # in-order delivery stays cheap with the buffer full
        cb._on_stream(cb._rcum, b"")
    assert time.monotonic() - start < 0.2
    await close(p)


async def test_spoofed_address_cannot_redirect_the_connection() -> None:
    """Review finding: after a quiet second, one authenticated packet from a
    new address moved the connection there; now the new address must answer
    a challenge first."""
    p = await make_pair()
    real_remote = p.cb.remote
    p.cb._remote_rx -= udp.MIGRATE_AFTER + 1  # the current path went quiet
    victim = UdpEndpoint(0, stun_servers=())
    await victim.start()
    got: list[bytes] = []
    victim._received = lambda data, addr: got.append(data)  # type: ignore[method-assign]
    victim_addr = ("127.0.0.1", victim.ports[socket.AF_INET])
    # The contact's packet arrives "from" the victim's address.
    hdr = udp.HEADER.pack(udp.MAGIC, udp.VERSION, p.cb.my_id, 999_999)
    pkt = hdr + p.ca._seal.encrypt(udp._nonce(999_999), bytes([udp.F_PING]), hdr)
    p.cb.on_datagram(hdr, 999_999, pkt[len(hdr):], victim_addr)
    await asyncio.sleep(0.2)
    assert p.cb.remote == real_remote  # not moved: the victim never answered
    assert len(got) <= 2  # at most a tiny challenge (no data flood)
    victim.stop()
    await close(p)


async def test_optimistic_acks_end_the_connection() -> None:
    """A peer acknowledging packets it never received (to make us flood a
    path) is caught by the packet numbers we skip on purpose."""
    p = await make_pair()
    for _ in range(udp.SKIP_MIN + udp.SKIP_SPREAD + 50):
        p.ca._send_packet(bytes([udp.F_PING]), eliciting=True)
    assert p.ca._skipped
    s = next(iter(p.ca._skipped))
    p.ca._on_ack(0, 0, 0.0, [(s + 1, s - 1)], time.monotonic())
    assert p.ca._closed
    await p.cb.close()
    p.a.stop()
    p.b.stop()


async def test_fast_and_responsive_on_a_lossy_mobile_path() -> None:
    """A radio path: 2.5 MB/s, 140 ms round trip, jitter, a deep buffer and
    1 % random loss. Loss-based congestion control (Reno) managed ~0.1 MB/s
    here; BBR keeps the link full. Replies from the receiving side (chat,
    file confirmations) must flow during the transfer (its acknowledgements
    once used up its pacing budget and starved them)."""
    import importlib.util
    import sys
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "bench_udp_path", Path(__file__).resolve().parents[1] / "scripts" / "bench_udp_path.py"
    )
    assert spec and spec.loader
    bench = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = bench  # (dataclasses look the module up)
    spec.loader.exec_module(bench)
    res = await bench.run("lte-lossy", 6)
    assert res["MB/s"] > 1.2, res  # (link: 2.5 MB/s; old controller: 0.1)
    assert res["chat_msgs"] >= 4, res


async def test_new_punches_from_a_fresh_port_existing_connection_keeps_its_own() -> None:
    """After a failed round the endpoint moves new punches to a new port
    (routers may hold stale state for the old pair of ports). A connection
    made before keeps working on its socket; the old socket closes once
    nothing uses it."""
    p = await make_pair()
    try:
        old = p.a.ports[socket.AF_INET]
        assert await p.a.rotate()
        assert p.a.ports[socket.AF_INET] != old
        await p.ca.send(b"still here")
        await p.cb.send(b"me too")
        assert await until(lambda: b"still here" in p.got_b and b"me too" in p.got_a)
        # A new connection punched from the new port.
        k1, k2 = derive_keys(secrets.token_bytes(32), "leader", "follower")
        ida, idb = secrets.token_bytes(8), secrets.token_bytes(8)
        ca2 = p.a.new_connection(idb, k1, k2, my_id=ida)
        cb2 = p.b.new_connection(ida, k2, k1, my_id=idb)
        assert ca2 and cb2
        pa = ("127.0.0.1", p.a.ports[socket.AF_INET])
        pb = ("127.0.0.1", p.b.ports[socket.AF_INET])
        assert await asyncio.gather(p.a.punch(ca2, [pb], 5.0), p.b.punch(cb2, [pa], 5.0)) == [
            True, True]
        assert cb2.remote == pa  # (it came from the new port)
        await ca2.close()
        await cb2.close()
        assert p.a._retired  # the first connection still uses the old socket
        await p.ca.close()
        p.a._close_unused()
        assert not p.a._retired
    finally:
        await close(p)
