"""What strangers and the network see of SRLTCP (0.15): who may connect, how
much a connection may send before it has proven who it is, giving up on
contacts that can't be reached, and quiet networks."""

from __future__ import annotations

import asyncio
import struct
from pathlib import Path

import pytest

import srltcp.core  # noqa: F401  (import order: core before its parts)
from srltcp.core.protocol.framing import (
    FRAME_MAGIC,
    PREAUTH_MAX_FRAME,
    FrameReader,
    FrameTooLarge,
    pack_frame,
)
from srltcp.core.trusted import TrustedPeer
from srltcp.transports.tcp import MAX_PREAUTH_PER_IP
from tests.test_e2e_nodes import make_node, wait_for


def test_frames_over_the_limit_end_the_connection() -> None:
    reader = FrameReader(max_frame=PREAUTH_MAX_FRAME)
    small = pack_frame(b"x" * 100)
    assert list(reader.frames(small)) == [b"x" * 100]
    big_header = FRAME_MAGIC + struct.pack(">I", PREAUTH_MAX_FRAME + 1) + b"\0\0\0\0"
    with pytest.raises(FrameTooLarge):
        list(reader.frames(big_header))


def test_the_limit_is_raised_between_frames() -> None:
    """A handshake answer followed at once by file data, in one read: the
    data frame is checked only after the answer was handled."""
    reader = FrameReader(max_frame=PREAUTH_MAX_FRAME)
    data = pack_frame(b"answer") + pack_frame(b"d" * (PREAUTH_MAX_FRAME * 2))
    out = []
    for frame in reader.frames(data):
        out.append(len(frame))
        reader.max_frame = 4 * 1024 * 1024  # (the answer proved who it is)
    assert out == [6, PREAUTH_MAX_FRAME * 2]


async def test_a_stranger_cant_make_us_buffer_a_big_frame(tmp_path: Path) -> None:
    node = make_node(tmp_path, "alice")
    await node.start()
    try:
        assert node.tcp_transport is not None
        r, w = await asyncio.open_connection("127.0.0.1", node.tcp_transport.port)
        w.write(FRAME_MAGIC + struct.pack(">I", 4 * 1024 * 1024) + b"\0\0\0\0" + b"z" * 1000)
        await w.drain()
        assert await asyncio.wait_for(r.read(), 5) == b""  # closed on us
        w.close()
    finally:
        await node.stop()


async def test_few_unauthenticated_connections_per_address(tmp_path: Path) -> None:
    node = make_node(tmp_path, "alice")
    await node.start()
    try:
        assert node.tcp_transport is not None
        conns = [await asyncio.open_connection("127.0.0.1", node.tcp_transport.port)
                 for _ in range(MAX_PREAUTH_PER_IP)]
        assert await wait_for(lambda: len(node.tcp_transport._connections) == MAX_PREAUTH_PER_IP)
        r, w = await asyncio.open_connection("127.0.0.1", node.tcp_transport.port)
        assert await asyncio.wait_for(r.read(), 5) == b""  # one too many: closed
        for _r, cw in [*conns, (r, w)]:
            cw.close()
    finally:
        await node.stop()


def test_internet_strangers_are_refused_contacts_addresses_allowed(tmp_path: Path) -> None:
    node = make_node(tmp_path, "alice")
    assert node._inbound_allowed("192.168.1.20") and node._inbound_allowed("10.1.2.3")
    assert node._inbound_allowed("100.101.102.103")  # (carrier / Tailscale space)
    assert node._inbound_allowed("fe80::1") and node._inbound_allowed("127.0.0.1")
    assert not node._inbound_allowed("81.2.69.160")
    assert not node._inbound_allowed("2a00:1450:4001:80b::200e")
    node.trusted.add(TrustedPeer(hash_id="c" * 32, name="Edge",
                                 alt_hosts=["81.2.69.160", "2a00:1450:4001:80b::1"]))
    assert node._inbound_allowed("81.2.69.160")
    assert node._inbound_allowed("2a00:1450:4001:80b::abcd")  # (same /64: a privacy address)
    assert not node._inbound_allowed("81.2.69.161")
    node.config.accept_internet = True  # "I port-forward TCP 7825"
    assert node._inbound_allowed("81.2.69.161")


def test_a_contact_that_doesnt_answer_is_left_alone_until_touched(  # type: ignore[no-untyped-def]
    tmp_path: Path, monkeypatch
) -> None:
    import srltcp.core.messaging.backend as backend_mod

    node = make_node(tmp_path, "alice")
    now = [1000.0]
    monkeypatch.setattr(backend_mod.time, "monotonic", lambda: now[0])
    peer = "c" * 32
    assert node._reach_allowed(peer)
    now[0] += backend_mod.REACH_GIVE_UP - 1
    assert node._reach_allowed(peer)
    now[0] += 2
    assert not node._reach_allowed(peer)  # gave up
    now[0] += 3600
    assert not node._reach_allowed(peer)  # (stays given up by itself)
    node.resume_reaching(peer)  # you opened their chat
    assert node._reach_allowed(peer)
    now[0] += backend_mod.REACH_GIVE_UP + 1
    assert not node._reach_allowed(peer)
    node.resume_reaching()  # the network changed
    assert node._reach_allowed(peer)
    node.reached(peer)  # in touch: the clock starts over
    now[0] += backend_mod.REACH_GIVE_UP - 1
    assert node._reach_allowed(peer)


def test_quiet_networks_pause_tor_and_the_internet_features(  # type: ignore[no-untyped-def]
    tmp_path: Path, monkeypatch
) -> None:
    import srltcp.utils.network as net

    node = make_node(tmp_path, "alice", tor_enabled=True, router_port_mapping=True)
    node.tcp_transport = object()  # type: ignore[assignment]  (Tor needs the TCP side)
    monkeypatch.setattr(net, "list_interfaces",
                        lambda **_k: [{"ip": "10.20.3.4", "netmask": "255.255.0.0"}])
    assert node.tor_enabled() and node.direct_internet_enabled() and node.port_mapping_enabled()
    node.config.quiet_networks = "192.168.0.0/16, 10.20.0.0/16"
    assert node.on_quiet_network()
    assert not node.tor_enabled() and not node.direct_internet_enabled()
    assert not node.port_mapping_enabled()
    assert node.tor_status()["quiet"] is True
    assert net.current_networks() == ["10.20.0.0/16"]
    node.config.quiet_networks = "192.168.0.0/16, not a network"
    assert not node.on_quiet_network() and node.tor_enabled()


def test_filenames_from_contacts_are_made_harmless() -> None:
    from srltcp.utils.files import safe_filename

    assert safe_filename("../../etc/passwd") == ".._.._etc_passwd"
    assert safe_filename("..") == "unnamed" and safe_filename(" ") == "unnamed"
    assert safe_filename("CON") == "_CON" and safe_filename("com1.txt") == "_com1.txt"
    assert safe_filename("report.pdf. . ") == "report.pdf"
    assert safe_filename("photo‮gpj.exe") == "photogpj.exe"  # (would show as "photoexe.jpg")
    long = safe_filename("x" * 400 + ".jpg")
    assert long.endswith(".jpg") and len(long.encode()) <= 240
    assert safe_filename("Holiday 2026 (1).jpg") == "Holiday 2026 (1).jpg"


async def test_an_address_that_is_blocked_here_rests(tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """'Access is denied' (a firewall) or no answer: not dialed every few seconds."""
    node = make_node(tmp_path, "alice")
    calls: list[str] = []

    class Blocked:
        async def connect(self, host: str, port: int, timeout: float) -> str:
            calls.append(host)
            raise PermissionError(13, "Access is denied")

    node.tcp_transport = Blocked()  # type: ignore[assignment]
    assert (await node._dial_first([("10.0.30.107", 7825)], 1.0))[0] == ""
    assert (await node._dial_first([("10.0.30.107", 7825)], 1.0))[0] == ""
    assert calls == ["10.0.30.107"]  # (the second time it rested)
    node.resume_reaching()  # a new network: try again
    await node._dial_first([("10.0.30.107", 7825)], 1.0)
    assert len(calls) == 2


async def test_a_refused_address_is_retried(tmp_path: Path) -> None:
    node = make_node(tmp_path, "alice")
    calls: list[str] = []

    class Refused:
        async def connect(self, host: str, port: int, timeout: float) -> str:
            calls.append(host)
            raise ConnectionRefusedError(111, "refused")

    node.tcp_transport = Refused()  # type: ignore[assignment]
    await node._dial_first([("10.0.30.110", 7825)], 1.0)
    await node._dial_first([("10.0.30.110", 7825)], 1.0)
    assert len(calls) == 2  # (the device is there; SRLTCP will be back)
