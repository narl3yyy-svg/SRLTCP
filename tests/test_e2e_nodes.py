"""End-to-end tests: two (or three) real nodes talking over loopback TCP."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from srltcp.core.messaging.backend import MessagingBackend, NodeConfig
from srltcp.core.trusted import TrustedPeer


def make_node(tmp_path: Path, name: str, **cfg: object) -> MessagingBackend:
    data = tmp_path / name
    data.mkdir(parents=True, exist_ok=True)
    old = os.environ.get("SRLTCP_DATA_DIR")
    os.environ["SRLTCP_DATA_DIR"] = str(data)
    try:
        config = NodeConfig(
            name=name,
            bind_host="127.0.0.1",
            tcp_port=int(cfg.pop("tcp_port", 0)),  # type: ignore[call-overload]
            discovery_port=0,
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
        await b.notify_contact_accepted(a.identities["tcp"].hash_id)
        assert await wait_for(lambda: "let me in" in texts(b))
    finally:
        await a.stop()
        await b.stop()


async def test_adding_a_peer_prompts_them_to_add_back(tmp_path: Path) -> None:
    """Trusting someone (no message sent) asks them to add us back; declining sticks."""
    a = make_node(tmp_path, "alice")
    b = make_node(tmp_path, "bob")
    await a.start()
    await b.start()
    events: list[dict] = []

    async def on_event(evt: dict) -> None:
        events.append(evt)

    b.set_callbacks(on_event=on_event)
    try:
        ah, bh = a.identities["tcp"].hash_id, b.identities["tcp"].hash_id
        trust(a, b)
        await a.connect_to_peer(bh)  # link-up tells bob "alice added you"
        assert await wait_for(
            lambda: any(e.get("kind") == "contact_request" and e["hash_id"] == ah for e in events)
        )
        assert b.discovery.get(ah) is not None
        b.decline_contact_request(ah)
        assert b.discovery.get(ah) is None
        events.clear()
        b._contact_requests.clear()
        await a.notify_contact_accepted(bh)
        await asyncio.sleep(0.5)
        assert not any(e.get("kind") == "contact_request" for e in events)
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


async def test_qr_pairing_is_mutual(tmp_path: Path) -> None:
    """Bob scans Alice's card; both sides end up trusting each other."""
    a = make_node(tmp_path, "alice", lan_ip="127.0.0.1")
    b = make_node(tmp_path, "bob")
    await a.start()
    await b.start()
    events: list[dict] = []

    async def on_event(evt: dict) -> None:
        events.append(evt)

    a.set_callbacks(on_event=on_event)
    try:
        ah, bh = a.identities["tcp"].hash_id, b.identities["tcp"].hash_id
        # Cards only advertise non-loopback addresses; patch for loopback tests.
        a._card_endpoints = lambda: [("127.0.0.1", a.config.tcp_port)]  # type: ignore[method-assign]
        card = a.create_pairing_card()
        peer = await b.import_pairing_card(card["uri"])
        assert peer["hash_id"] == ah
        assert b.is_trusted(ah)
        assert await wait_for(lambda: a.is_trusted(bh), timeout=10)
        assert any(e.get("kind") == "paired" for e in events)
        await b.send_message(ah, "paired!")
        assert await wait_for(lambda: "paired!" in texts(a))
        # The token is single-use.
        assert not a._pairing_tokens
    finally:
        await a.stop()
        await b.stop()


def test_card_tampering_detected(tmp_path: Path) -> None:
    from srltcp.core.messaging.pairing import (
        CARD_PREFIX,
        PairingError,
        _b64d,
        _b64e,
        decode_card,
    )

    a = make_node(tmp_path, "alice")
    a.identities["tcp"] = a.identity_store.load_or_create("alice", "tcp")
    a._card_endpoints = lambda: [("192.168.1.20", 7825)]  # type: ignore[method-assign]
    uri = a.create_pairing_card()["uri"]
    card = decode_card(uri)
    assert card.hash_id == a.identities["tcp"].hash_id
    assert card.endpoints == [("192.168.1.20", 7825)]
    import json

    fields = json.loads(_b64d(uri[len(CARD_PREFIX):]))
    fields["a"] = [["6.6.6.6", 7825]]
    forged = CARD_PREFIX + _b64e(json.dumps(fields).encode())
    with pytest.raises(PairingError):
        decode_card(forged)
    with pytest.raises(PairingError):
        decode_card("srltcp://pair#garbage")


async def test_noise_xx_mode_end_to_end(tmp_path: Path) -> None:
    a = make_node(tmp_path, "alice", handshake_protocol="noise_xx")
    b = make_node(tmp_path, "bob", handshake_protocol="noise_xx")
    await a.start()
    await b.start()
    try:
        trust(a, b)
        trust(b, a)
        ah, bh = a.identities["tcp"].hash_id, b.identities["tcp"].hash_id
        await a.send_message(bh, "noise hello")
        assert await wait_for(lambda: "noise hello" in texts(b), timeout=10)
        assert await wait_for(lambda: a.get_messages()[-1]["status"] == "delivered")
        await b.send_message(ah, "noise reply")
        assert await wait_for(lambda: "noise reply" in texts(a))
        # Keys must not be derivable from the (public) handshake hash.
        link = a.get_link(bh)
        assert link is not None and link.crypto.ready
    finally:
        await a.stop()
        await b.stop()


async def test_stale_noise_setting_still_links(tmp_path: Path) -> None:
    """A device with the retired Noise XX setting saved must still connect
    (it used to be unable to talk to every default device)."""
    a = make_node(tmp_path, "alice", handshake_protocol="noise_xx")
    b = make_node(tmp_path, "bob")
    await a.start()
    await b.start()
    try:
        trust(a, b)
        trust(b, a)
        await a.connect_to_peer(b.identities["tcp"].hash_id)
        assert await a.wait_for_handshake(b.identities["tcp"].hash_id, timeout=8.0)
    finally:
        await a.stop()
        await b.stop()


async def test_unicast_announce_gets_reply_both_discover(pair) -> None:  # type: ignore[no-untyped-def]
    """VPN case: no broadcast. A unicast announce to B makes B reply, so both see each other."""
    a, b = pair
    ah, bh = a.identities["tcp"].hash_id, b.identities["tcp"].hash_id
    a.trusted.remove(bh)
    b.trusted.remove(ah)
    b.config.announce = True  # B is discoverable
    payload = a.build_announce_payload("tcp")
    port = b.tcp_transport.discovery_port
    await a.tcp_transport.unicast_discovery(payload, ["127.0.0.1"], {port})
    assert await wait_for(lambda: b.discovery.get(ah) is not None)
    assert await wait_for(lambda: a.discovery.get(bh) is not None)
    # Replies are never answered: exactly one reply was recorded on each side.
    await asyncio.sleep(0.3)
    assert len(b._announce_replies) == 1
    assert len(a._announce_replies) == 0


async def test_announce_updates_trusted_contact_address(pair) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    ah = a.identities["tcp"].hash_id
    b.trusted.update(ah, tcp_host="10.9.9.9", tcp_port=1234)
    payload = a.build_announce_payload("tcp")
    port = b.tcp_transport.discovery_port
    await a.tcp_transport.unicast_discovery(payload, ["127.0.0.1"], {port})
    # Loopback sources are not trusted as addresses; tcp_host comes from the payload.
    assert await wait_for(lambda: b.trusted.get(ah).tcp_port == a.config.tcp_port)


async def test_connect_falls_back_to_alternate_address(pair) -> None:  # type: ignore[no-untyped-def]
    a, b = pair
    bh = b.identities["tcp"].hash_id
    # Primary address is dead (TEST-NET-1, never routable); the alternate works.
    a.trusted.update(bh, tcp_host="192.0.2.123", alt_hosts=["127.0.0.1"])
    orig = a.tcp_transport.connect

    async def fast(host: str, port: int, *, timeout: float = 8.0) -> str:
        return await orig(host, port, timeout=0.5)

    a.tcp_transport.connect = fast  # type: ignore[method-assign]
    assert await a.connect_to_peer(bh)
    assert await a.wait_for_handshake(bh)
    assert a.trusted.get(bh).tcp_host == "127.0.0.1"


async def test_qr_pairing_survives_both_apps_restarting(tmp_path: Path) -> None:
    """Scanner can't reach the scanned device at first (other network) and both
    apps restart before they meet: a single scan still pairs both ways."""
    a = make_node(tmp_path, "alice")
    b = make_node(tmp_path, "bob")
    await a.start()
    await b.start()
    a._card_endpoints = lambda: [("127.0.0.1", 9)]  # type: ignore[method-assign]
    card = a.create_pairing_card()
    ah, bh = a.identities["tcp"].hash_id, b.identities["tcp"].hash_id
    await b.import_pairing_card(card["uri"], connect=False)
    assert b.is_trusted(ah) and not a.is_trusted(bh)
    await a.stop()
    await b.stop()
    a2 = make_node(tmp_path, "alice")
    b2 = make_node(tmp_path, "bob")
    await a2.start()
    await b2.start()
    try:
        b2.trusted.update(ah, tcp_host="127.0.0.1", tcp_port=a2.config.tcp_port)
        await b2.connect_to_peer(ah)
        assert await wait_for(lambda: a2.is_trusted(bh), timeout=10)
        assert not b2.trusted.get(ah).pair_token_out  # delivered once
        assert not a2._pairing_tokens  # single use, also on disk
    finally:
        await a2.stop()
        await b2.stop()


async def test_overlapping_connects_keep_one_session(tmp_path: Path) -> None:
    """Auto-reconnect and a user action dialing at the same time must not send
    two handshakes on one connection (keys diverged, nothing decrypted)."""
    a = make_node(tmp_path, "alice")
    b = make_node(tmp_path, "bob")
    await a.start()
    await b.start()
    try:
        trust(a, b)
        trust(b, a)
        ah, bh = a.identities["tcp"].hash_id, b.identities["tcp"].hash_id
        await asyncio.gather(*(b.connect_to_peer(ah) for _ in range(3)))
        assert await b.wait_for_handshake(ah, timeout=8.0)
        await asyncio.sleep(0.3)
        await b.send_message(ah, "one")
        assert await wait_for(lambda: "one" in texts(a))
        await a.send_message(bh, "two")
        assert await wait_for(lambda: "two" in texts(b))
    finally:
        await a.stop()
        await b.stop()
