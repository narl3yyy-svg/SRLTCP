"""End-to-end tests: two (or three) real nodes talking over loopback TCP."""

from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path

import pytest

from srltcp.core.messaging.backend import MessagingBackend, NodeConfig
from srltcp.core.trusted import TrustedPeer


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def make_node(tmp_path: Path, name: str, **cfg: object) -> MessagingBackend:
    data = tmp_path / name
    data.mkdir(parents=True, exist_ok=True)
    old = os.environ.get("SRLTCP_DATA_DIR")
    os.environ["SRLTCP_DATA_DIR"] = str(data)
    try:
        config = NodeConfig(
            name=name,
            bind_host="127.0.0.1",
            tcp_port=int(cfg.pop("tcp_port", _free_port())),  # type: ignore[call-overload]
            discovery_port=_free_port(),
            strict_ports=True,
            incoming_dir=str(data / "incoming"),
            **cfg,  # type: ignore[arg-type]
        )
        return MessagingBackend(config)
    finally:
        if old is None:
            os.environ.pop("SRLTCP_DATA_DIR", None)
        else:
            os.environ["SRLTCP_DATA_DIR"] = old


def trust(a: MessagingBackend, b: MessagingBackend) -> None:
    ident = b.identities["tcp"]
    a.trusted.add(
        TrustedPeer(
            hash_id=ident.hash_id,
            name=ident.name,
            public_key=ident.public_bytes().hex(),
            tcp_host="127.0.0.1",
            tcp_port=b.config.tcp_port,
        )
    )


async def wait_for(predicate, timeout: float = 5.0) -> bool:  # type: ignore[no-untyped-def]
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return bool(predicate())


def texts(node: MessagingBackend) -> list[str]:
    return [m["text"] for m in node.get_messages()]


@pytest.fixture
async def pair(tmp_path: Path):  # type: ignore[no-untyped-def]
    a = make_node(tmp_path, "alice")
    b = make_node(tmp_path, "bob")
    await a.start()
    await b.start()
    trust(a, b)
    trust(b, a)
    yield a, b
    await a.stop()
    await b.stop()


async def test_message_roundtrip_with_receipt(pair) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    bob = b.identities["tcp"].hash_id
    msg = await a.send_message(bob, "hello bob")
    assert msg is not None
    assert await wait_for(lambda: "hello bob" in texts(b))
    assert await wait_for(lambda: a.get_messages()[-1]["status"] == "delivered")


async def test_offline_message_delivered_after_peer_restart(tmp_path: Path) -> None:
    a = make_node(tmp_path, "alice")
    b = make_node(tmp_path, "bob")
    await a.start()
    await b.start()
    trust(a, b)
    trust(b, a)
    bob_hash = b.identities["tcp"].hash_id
    bob_port = b.config.tcp_port
    await a.send_message(bob_hash, "first")
    assert await wait_for(lambda: "first" in texts(b))
    await b.stop()

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    msg = await a.send_message(bob_hash, "while you were away")
    assert msg is not None
    assert loop.time() - t0 < 1.0, "send must not block when the peer is offline"
    assert msg.status in ("pending", "sent")

    b2 = make_node(tmp_path, "bob", tcp_port=bob_port)
    await b2.start()
    try:
        assert b2.identities["tcp"].hash_id == bob_hash
        # History survived the restart.
        assert "first" in texts(b2)
        assert await wait_for(lambda: "while you were away" in texts(b2), timeout=20)
        assert await wait_for(
            lambda: a.get_messages()[-1]["status"] == "delivered", timeout=5
        )
        assert texts(b2).count("while you were away") == 1
    finally:
        await a.stop()
        await b2.stop()


async def test_simultaneous_dial_converges(pair) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    ah, bh = a.identities["tcp"].hash_id, b.identities["tcp"].hash_id
    await asyncio.gather(a.connect_to_peer(bh), b.connect_to_peer(ah))
    assert await wait_for(
        lambda: bool(a.get_link(bh) and a.get_link(bh).handshake_complete)
        and bool(b.get_link(ah) and b.get_link(ah).handshake_complete)
    )
    await asyncio.sleep(0.3)
    await a.send_message(bh, "ping from a")
    await b.send_message(ah, "ping from b")
    assert await wait_for(lambda: "ping from a" in texts(b))
    assert await wait_for(lambda: "ping from b" in texts(a))


async def test_impersonation_rejected(tmp_path: Path) -> None:
    """Mallory claims Bob's hash but cannot prove Bob's key."""
    a = make_node(tmp_path, "alice")
    b = make_node(tmp_path, "bob")
    m = make_node(tmp_path, "mallory")
    for n in (a, b, m):
        await n.start()
    try:
        trust(a, b)
        bob = b.identities["tcp"]
        real = m.identities["tcp"]
        # Mallory sends handshakes carrying Bob's hash with her own key.
        from srltcp.core.messaging.links import PeerLink

        m.trusted.add(
            TrustedPeer(hash_id=a.identities["tcp"].hash_id, name="alice",
                        tcp_host="127.0.0.1", tcp_port=a.config.tcp_port)
        )
        object.__setattr__(real, "hash_id", bob.hash_id)
        peer_id = await m.tcp_transport.connect("127.0.0.1", a.config.tcp_port)
        m.register_link(PeerLink(hash_id=a.identities["tcp"].hash_id,
                                 transport_peer_id=peer_id, transport="tcp",
                                 address="", public_key=b""))
        await m._initiate_handshake(a.identities["tcp"].hash_id)
        await asyncio.sleep(0.5)
        link = a.get_link(bob.hash_id)
        assert link is None or not link.handshake_complete
    finally:
        for n in (a, b, m):
            await n.stop()


async def test_untrusted_peer_messages_not_shown(tmp_path: Path) -> None:
    a = make_node(tmp_path, "alice")
    b = make_node(tmp_path, "bob")
    await a.start()
    await b.start()
    events: list[dict] = []

    async def on_event(evt: dict) -> None:
        events.append(evt)

    b.set_callbacks(on_event=on_event)
    try:
        trust(a, b)  # alice trusts bob; bob has never heard of alice
        bh = b.identities["tcp"].hash_id
        await a.send_message(bh, "let me in")
        assert await wait_for(lambda: any(e.get("kind") == "contact_request" for e in events))
        assert texts(b) == []
        assert await wait_for(lambda: a.get_messages()[-1]["status"] == "not_trusted")
        # Bob accepts the request -> the queued message is delivered on retry.
        trust(b, a)
        await a.flush_outbox(bh)
        assert await wait_for(lambda: "let me in" in texts(b))
    finally:
        await a.stop()
        await b.stop()


async def test_file_transfer_end_to_end(pair, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    bh = b.identities["tcp"].hash_id
    src = tmp_path / "photo.jpg"
    payload = os.urandom(3 * 1024 * 1024 + 123)
    src.write_bytes(payload)
    result = await a.send_file(bh, src)
    assert result is not None
    tid = result["id"]
    assert await wait_for(
        lambda: b._transfers.get(tid) is not None
        and b._transfers[tid].state.value == "complete",
        timeout=15,
    )
    received = b._incoming_paths[tid]
    assert received.read_bytes() == payload
    assert src.read_bytes() == payload


async def test_peer_cannot_overwrite_outgoing_file(pair, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A malicious peer replays chunk writes against our *outgoing* transfer id."""
    a, b = pair
    bh = b.identities["tcp"].hash_id
    src = tmp_path / "secret.txt"
    src.write_bytes(b"original contents")
    result = await a.send_file(bh, src)
    assert result is not None
    from srltcp.core.protocol.messages import MessageType, pack_file_chunk

    await wait_for(lambda: a._transfers[result["id"]].state.value == "complete")
    ah = a.identities["tcp"].hash_id
    link = b.get_link(ah)
    assert link is not None
    evil = pack_file_chunk(result["id"], 0, b"PWNED")
    packet = b._seal(link, MessageType.FILE_CHUNK, evil)
    await b._send_raw(link.transport_peer_id, link.transport, packet)
    await asyncio.sleep(0.3)
    assert src.read_bytes() == b"original contents"


async def test_hub_relay_end_to_end(tmp_path: Path) -> None:
    hub = make_node(tmp_path, "hub", hub_mode=True)
    await hub.start()
    hub_port = hub.config.tcp_port
    a = make_node(tmp_path, "alice", hub_enabled=True, hub_host="127.0.0.1", hub_port=hub_port)
    b = make_node(tmp_path, "bob", hub_enabled=True, hub_host="127.0.0.1", hub_port=hub_port)
    await a.start()
    await b.start()
    try:
        ah, bh = a.identities["tcp"].hash_id, b.identities["tcp"].hash_id
        # Presence is registered automatically on connect.
        assert await wait_for(lambda: a.discovery.get(bh, "hub") is not None)
        assert await wait_for(lambda: b.discovery.get(ah, "hub") is not None)
        trust(a, b)
        trust(b, a)
        await a.send_message(bh, "via hub")
        assert await wait_for(lambda: "via hub" in texts(b), timeout=10)
        assert a.get_link(bh).transport == "hub"
        assert await wait_for(lambda: a.get_messages()[-1]["status"] == "delivered")
    finally:
        await a.stop()
        await b.stop()
        await hub.stop()
