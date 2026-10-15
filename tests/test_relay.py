"""Internet relay: two nodes that can't reach each other talk through a relay."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest

from srltcp.core.messaging.relay import RelayConnection, RelayPinError
from srltcp.core.relay_proto import R_SEND, R_SUB, RelayCode
from srltcp.core.relay_server import RelayServer
from tests.test_e2e_nodes import make_node, texts, trust, wait_for


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def h(node) -> str:  # type: ignore[no-untyped-def]
    return node.identities["tcp"].hash_id


@pytest.fixture
async def relay(tmp_path: Path):  # type: ignore[no-untyped-def]
    server = RelayServer("127.0.0.1", 0, tmp_path / "relay")
    await server.start()
    yield server
    await server.close()


async def paired_over_lan(tmp_path: Path, relay: RelayServer):  # type: ignore[no-untyped-def]
    """Alice and Bob pair on the local network; only Alice configured a relay."""
    a, b = make_node(tmp_path, "alice"), make_node(tmp_path, "bob")
    a.config.relay_code = relay.code("127.0.0.1")
    await a.start()
    await b.start()
    trust(a, b)
    trust(b, a)
    await a.connect_to_peer(h(b))
    assert await a.wait_for_handshake(h(b))
    # Inside the encrypted session they swap pair-secret halves and Bob learns
    # Alice's relay; both connect to it.
    assert await wait_for(lambda: b.relay_available_for(h(a)) and a.relay_available_for(h(b)))
    return a, b


async def cut_direct_path(a, b) -> None:  # type: ignore[no-untyped-def]
    """Like leaving the VPN: the saved addresses stop answering."""
    for x, y in ((a, b), (b, a)):
        x.trusted.update(h(y), tcp_host="127.0.0.1", tcp_port=closed_port(), alt_hosts=[])
    await a.disconnect_peer(h(b))
    await b.disconnect_peer(h(a))


async def test_messages_move_to_the_relay_and_back(tmp_path: Path, relay: RelayServer) -> None:
    a, b = await paired_over_lan(tmp_path, relay)
    try:
        real_ports = {h(a): a.config.tcp_port, h(b): b.config.tcp_port}
        await cut_direct_path(a, b)
        await a.send_message(h(b), "over the internet")
        assert await wait_for(lambda: "over the internet" in texts(b), timeout=15)
        assert a.get_link(h(b)).transport == "relay"
        assert await wait_for(lambda: a.get_messages()[-1]["status"] == "delivered")
        await b.send_message(h(a), "and back")
        assert await wait_for(lambda: "and back" in texts(a), timeout=15)

        # Direct path works again (VPN back on): the lower hash moves the session.
        low, high = (a, b) if h(a) < h(b) else (b, a)
        low.trusted.update(h(high), tcp_host="127.0.0.1", tcp_port=real_ports[h(high)])
        low._upgrade_attempts.clear()
        await low.maintain_paths()
        assert await wait_for(lambda: low.get_link(h(high)).transport == "tcp", timeout=15)
        assert await wait_for(lambda: high.get_link(h(low)).transport == "tcp", timeout=15)
        await high.send_message(h(low), "direct again")
        assert await wait_for(lambda: "direct again" in texts(low), timeout=10)
    finally:
        await a.stop()
        await b.stop()


async def test_file_on_the_relay_moves_to_the_direct_path(
    tmp_path: Path, relay: RelayServer
) -> None:
    import os

    a, b = await paired_over_lan(tmp_path, relay)
    try:
        real_ports = {h(a): a.config.tcp_port, h(b): b.config.tcp_port}
        await cut_direct_path(a, b)
        await a.send_message(h(b), "on the relay")
        assert await wait_for(lambda: "on the relay" in texts(b), timeout=15)
        assert a.get_link(h(b)).transport == "relay"
        import dataclasses

        params = a._path_params
        a._path_params = lambda link: dataclasses.replace(params(link), delay=0.02)  # type: ignore[method-assign]
        src = tmp_path / "big.bin"
        payload = os.urandom(6 * 1024 * 1024)
        src.write_bytes(payload)
        tid = (await a.send_file(h(b), src))["id"]
        assert await wait_for(lambda: tid in b._transfers and b._transfers[tid].offset > 0)
        assert b._transfers[tid].state != "complete"
        # Mid-transfer the direct path works again: the session moves and the
        # file carries on over it, intact.
        low, high = (a, b) if h(a) < h(b) else (b, a)
        low.trusted.update(h(high), tcp_host="127.0.0.1", tcp_port=real_ports[h(high)])
        low._upgrade_attempts.clear()
        await low.maintain_paths()
        assert await wait_for(lambda: low.get_link(h(high)).transport == "tcp", timeout=15)
        assert b._transfers[tid].state != "complete", "should switch mid-transfer"
        assert await wait_for(lambda: b._transfers[tid].state == "complete", timeout=60)
        assert b._incoming_paths[tid].read_bytes() == payload
    finally:
        await a.stop()
        await b.stop()


async def test_relay_sees_no_names_identities_or_text(tmp_path: Path, relay: RelayServer) -> None:
    seen: list[bytes] = []
    original = relay._on_frame

    def spy(conn, frame):  # type: ignore[no-untyped-def]
        seen.append(bytes(frame))
        return original(conn, frame)

    relay._on_frame = spy  # type: ignore[method-assign]
    a, b = await paired_over_lan(tmp_path, relay)
    try:
        await cut_direct_path(a, b)
        await a.send_message(h(b), "SECRET-PLAINTEXT-MARKER")
        assert await wait_for(lambda: "SECRET-PLAINTEXT-MARKER" in texts(b), timeout=15)
        blob = b"".join(seen)
        assert any(f[0] == R_SEND for f in seen)
        for needle in (
            b"SECRET-PLAINTEXT-MARKER",
            b"alice",
            b"bob",
            h(a).encode(),
            h(b).encode(),
            bytes.fromhex(h(a)),
            bytes.fromhex(h(b)),
            a.identities["tcp"].public_bytes(),
            a.identities["tcp"].public_bytes().hex().encode(),
            b"handshake",
            b"ephemeral",
        ):
            assert needle not in blob, needle
        # Relayed packets are padded to fixed buckets (sizes reveal little).
        sends = [len(f) - 1 - 16 for f in seen if f[0] == R_SEND]
        assert all((n - 12 - 16) % 256 == 0 for n in sends), sends
    finally:
        await a.stop()
        await b.stop()


async def test_relay_rejects_wrong_pin_and_wrong_key(relay: RelayServer) -> None:
    good = RelayCode.parse(relay.code("127.0.0.1"))

    async def noop(*_a):  # type: ignore[no-untyped-def]
        return None

    wrong_pin = RelayCode(good.host, good.port, b"\x00" * 32, good.access_key)
    with pytest.raises(RelayPinError):
        await RelayConnection(wrong_pin, noop, noop).connect()
    wrong_key = RelayCode(good.host, good.port, good.cert_sha256, b"\x00" * 16)
    with pytest.raises(ConnectionError):
        await RelayConnection(wrong_key, noop, noop).connect()
    ok = RelayConnection(good, noop, noop)
    await ok.connect()
    assert ok.connected
    await ok.close()


async def test_injected_replayed_or_foreign_packets_are_dropped(
    tmp_path: Path, relay: RelayServer
) -> None:
    captured: list[bytes] = []
    original = relay._on_frame

    def spy(conn, frame):  # type: ignore[no-untyped-def]
        if frame[0] == R_SEND:
            captured.append(bytes(frame))
        return original(conn, frame)

    relay._on_frame = spy  # type: ignore[method-assign]
    a, b = await paired_over_lan(tmp_path, relay)
    try:
        await cut_direct_path(a, b)
        await a.send_message(h(b), "once")
        assert await wait_for(lambda: "once" in texts(b), timeout=15)
        good = RelayCode.parse(relay.code("127.0.0.1"))

        async def noop(*_a):  # type: ignore[no-untyped-def]
            return None

        # A relay insider (has the access key) replays every captured packet and
        # sends junk to the same mailboxes: nothing new appears, nothing breaks.
        evil = RelayConnection(good, noop, noop)
        await evil.connect()
        before = len(b.get_messages(limit=1000))
        for frame in captured:
            await evil.send(R_SEND, frame[1:])
            token = frame[1:17]
            await evil.send(R_SEND, token + b"\x00" * 300)
        await asyncio.sleep(1.0)
        assert len(b.get_messages(limit=1000)) == before
        assert texts(b).count("once") == 1
        # Squatting: subscribing random mailboxes doesn't intercept anything.
        await evil.send(R_SUB, b"\x11" * 32 * 10)
        await a.send_message(h(b), "still private")
        assert await wait_for(lambda: "still private" in texts(b), timeout=15)
        await evil.close()
    finally:
        await a.stop()
        await b.stop()


async def test_relay_limits(relay: RelayServer) -> None:
    good = RelayCode.parse(relay.code("127.0.0.1"))

    async def noop(*_a):  # type: ignore[no-untyped-def]
        return None

    conn = RelayConnection(good, noop, noop)
    await conn.connect()
    # Too many subscriptions is a protocol violation: the relay hangs up.
    await conn.send(R_SUB, b"\x22" * 32 * 2000)
    assert await wait_for(lambda: not conn.connected, timeout=5)
    # Talking without TLS gets nowhere.
    reader, writer = await asyncio.open_connection("127.0.0.1", relay.port)
    writer.write(b"\x00" * 4096)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(100), 5)
    assert data == b"" or not data.startswith(b"SRL")
    writer.close()


async def test_relay_first_on_a_restricted_network(tmp_path: Path, relay: RelayServer) -> None:
    """*Relay first on restricted networks* (opt-in): once this Wi-Fi is known
    to keep the phones apart, connecting goes straight to the relay (no wait
    for a dead direct dial) and no direct tries are made; a new network
    brings direct back."""
    a, b = await paired_over_lan(tmp_path, relay)
    try:
        real_ports = {h(a): a.config.tcp_port, h(b): b.config.tcp_port}
        low, high = (a, b) if h(a) < h(b) else (b, a)
        for n in (a, b):
            n.config.relay_first_restricted = True
        await cut_direct_path(a, b)
        await low.send_message(h(high), "plane wifi")
        assert await wait_for(lambda: "plane wifi" in texts(high), timeout=15)
        assert low.get_link(h(high)).transport == "relay"
        assert h(high) in low._lan_unreachable  # the dial on "our Wi-Fi" went unanswered
        # Reconnecting skips the direct dial entirely.
        dials: list[object] = []
        real_dial = low._dial_first

        async def counting(*args, **kw):  # type: ignore[no-untyped-def]
            dials.append(args)
            return await real_dial(*args, **kw)

        low._dial_first = counting  # type: ignore[method-assign]
        await low.disconnect_peer(h(high))
        await low.connect_to_peer(h(high))
        assert await wait_for(lambda: low.get_link(h(high)) is not None
                              and low.get_link(h(high)).handshake_complete, timeout=15)
        assert low.get_link(h(high)).transport == "relay" and not dials
        # The direct address would work, but this network is restricted: no tries.
        low.trusted.update(h(high), tcp_host="127.0.0.1", tcp_port=real_ports[h(high)])
        low._upgrade_attempts.clear()
        await low.maintain_paths()
        await asyncio.sleep(2.0)
        assert low.get_link(h(high)).transport == "relay" and not dials
        # Another network: back to normal, the direct path takes over.
        low.network_changed_for_paths()
        low._upgrade_attempts.clear()
        await low.maintain_paths()
        assert await wait_for(lambda: low.get_link(h(high)).transport == "tcp", timeout=15)
    finally:
        await a.stop()
        await b.stop()
