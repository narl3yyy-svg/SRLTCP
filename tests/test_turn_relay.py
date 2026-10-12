"""Local TURN relay: a scripted TURN client exercises the browser flow."""

from __future__ import annotations

import asyncio
import os
import struct

from srltcp.core.messaging.turn import (
    A_CHANNEL_NUMBER,
    A_DATA,
    A_ERROR_CODE,
    A_LIFETIME,
    A_NONCE,
    A_REALM,
    A_REQUESTED_TRANSPORT,
    A_USERNAME,
    A_XOR_PEER_ADDRESS,
    A_XOR_RELAYED_ADDRESS,
    ALLOCATE,
    CHANNEL_BIND,
    CREATE_PERMISSION,
    DATA,
    ERROR,
    INDICATION,
    REFRESH,
    REQUEST,
    SEND,
    SUCCESS,
    LocalTurnServer,
    build_stun,
    check_integrity,
    long_term_key,
    parse_stun,
    parse_xor_address,
    virtual_ip,
    xor_address,
)

A_HASH = "a" * 32
B_HASH = "b" * 32


class Client(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):  # type: ignore[no-untyped-def]
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[no-untyped-def]
        self.queue.put_nowait(data)

    async def request(self, packet: bytes, server: tuple[str, int]) -> bytes:
        assert self.transport is not None
        self.transport.sendto(packet, server)
        return await asyncio.wait_for(self.queue.get(), 2)


async def make_client() -> Client:
    loop = asyncio.get_running_loop()
    _t, proto = await loop.create_datagram_endpoint(Client, local_addr=("127.0.0.1", 0))
    return proto


async def allocate(
    client: Client, server: tuple[str, int], creds: dict[str, str]
) -> tuple[bytes, int]:
    tid = os.urandom(12)
    transport_attr = (A_REQUESTED_TRANSPORT, bytes([17, 0, 0, 0]))
    resp = parse_stun(
        await client.request(build_stun(ALLOCATE, REQUEST, tid, [transport_attr]), server)
    )
    assert resp and resp.cls == ERROR and resp.get(A_ERROR_CODE)[2:4] == bytes([4, 1])
    realm, nonce = resp.get(A_REALM), resp.get(A_NONCE)
    key = long_term_key(creds["username"], creds["credential"])
    tid = os.urandom(12)
    attrs = [
        transport_attr,
        (A_USERNAME, creds["username"].encode()),
        (A_REALM, realm),
        (A_NONCE, nonce),
    ]
    resp = parse_stun(
        await client.request(build_stun(ALLOCATE, REQUEST, tid, attrs, key=key), server)
    )
    assert resp and resp.cls == SUCCESS, resp
    assert check_integrity(resp, key)
    relayed = parse_xor_address(resp.get(A_XOR_RELAYED_ADDRESS))
    assert relayed is not None
    return nonce, relayed[1]


def auth(creds: dict[str, str], nonce: bytes) -> list[tuple[int, bytes]]:
    return [(A_USERNAME, creds["username"].encode()), (A_REALM, b"srltcp"), (A_NONCE, nonce)]


async def test_relay_end_to_end_between_two_nodes() -> None:
    # Two relays wired together the way two nodes are (CALL_MEDIA messages).
    relays: dict[str, LocalTurnServer] = {}

    def wire(src_hash: str, dst: str):  # type: ignore[no-untyped-def]
        async def send(_peer: str, sp: int, dp: int, data: bytes) -> None:
            relays[dst].deliver(src_hash, sp, dp, data)

        return send

    relays["A"] = LocalTurnServer(lambda: A_HASH, wire(A_HASH, "B"))
    relays["B"] = LocalTurnServer(lambda: B_HASH, wire(B_HASH, "A"))
    pa = await relays["A"].start()
    pb = await relays["B"].start()
    try:
        ca, cb = await make_client(), await make_client()
        cred_a = relays["A"].issue_credentials(B_HASH)
        cred_b = relays["B"].issue_credentials(A_HASH)
        nonce_a, port_a = await allocate(ca, ("127.0.0.1", pa), cred_a)
        nonce_b, port_b = await allocate(cb, ("127.0.0.1", pb), cred_b)
        key_a = long_term_key(cred_a["username"], cred_a["credential"])
        key_b = long_term_key(cred_b["username"], cred_b["credential"])
        vip_a, vip_b = virtual_ip(A_HASH), virtual_ip(B_HASH)

        # B opens a permission for A; A binds a channel to B.
        tid = os.urandom(12)
        perm = build_stun(
            CREATE_PERMISSION,
            REQUEST,
            tid,
            [(A_XOR_PEER_ADDRESS, xor_address(vip_a, 0, tid)), *auth(cred_b, nonce_b)],
            key=key_b,
        )
        assert parse_stun(await cb.request(perm, ("127.0.0.1", pb))).cls == SUCCESS
        tid = os.urandom(12)
        bind = build_stun(
            CHANNEL_BIND,
            REQUEST,
            tid,
            [
                (A_CHANNEL_NUMBER, struct.pack(">HH", 0x4001, 0)),
                (A_XOR_PEER_ADDRESS, xor_address(vip_b, port_b, tid)),
                *auth(cred_a, nonce_a),
            ],
            key=key_a,
        )
        assert parse_stun(await ca.request(bind, ("127.0.0.1", pa))).cls == SUCCESS

        # A -> B over the channel: B gets a Data indication from A's relayed address.
        payload = b"rtp-packet-from-A"
        ca.transport.sendto(struct.pack(">HH", 0x4001, len(payload)) + payload, ("127.0.0.1", pa))
        got = parse_stun(await asyncio.wait_for(cb.queue.get(), 2))
        assert got.method == DATA and got.cls == INDICATION
        assert got.get(A_DATA) == payload
        assert parse_xor_address(got.get(A_XOR_PEER_ADDRESS)) == (vip_a, port_a)

        # B -> A with a Send indication: A has a channel for B, so it gets ChannelData.
        reply = b"rtp-packet-from-B"
        tid = os.urandom(12)
        cb.transport.sendto(
            build_stun(
                SEND,
                INDICATION,
                tid,
                [(A_XOR_PEER_ADDRESS, xor_address(vip_a, port_a, tid)), (A_DATA, reply)],
            ),
            ("127.0.0.1", pb),
        )
        raw = await asyncio.wait_for(ca.queue.get(), 2)
        ch, length = struct.unpack(">HH", raw[:4])
        assert ch == 0x4001 and raw[4 : 4 + length] == reply

        # Refresh keeps the allocation; lifetime 0 removes it.
        tid = os.urandom(12)
        refresh = build_stun(
            REFRESH,
            REQUEST,
            tid,
            [(A_LIFETIME, struct.pack(">I", 0)), *auth(cred_a, nonce_a)],
            key=key_a,
        )
        assert parse_stun(await ca.request(refresh, ("127.0.0.1", pa))).cls == SUCCESS
        assert port_a not in relays["A"]._by_port
    finally:
        relays["A"].close()
        relays["B"].close()


async def test_relay_rejects_bad_credentials_and_foreign_peers() -> None:
    sent: list[tuple[str, int, int, bytes]] = []

    async def record(p: str, sp: int, dp: int, d: bytes) -> None:
        sent.append((p, sp, dp, d))

    relay = LocalTurnServer(lambda: A_HASH, record)
    port = await relay.start()
    try:
        client = await make_client()
        cred = relay.issue_credentials(B_HASH)
        bad = {"username": cred["username"], "credential": "wrong"}
        tid = os.urandom(12)
        transport_attr = (A_REQUESTED_TRANSPORT, bytes([17, 0, 0, 0]))
        first = parse_stun(
            await client.request(
                build_stun(ALLOCATE, REQUEST, tid, [transport_attr]), ("127.0.0.1", port)
            )
        )
        nonce = first.get(A_NONCE)
        attrs = [transport_attr, *auth(bad, nonce)]
        resp = parse_stun(
            await client.request(
                build_stun(
                    ALLOCATE,
                    REQUEST,
                    os.urandom(12),
                    attrs,
                    key=long_term_key(bad["username"], bad["credential"]),
                ),
                ("127.0.0.1", port),
            )
        )
        assert resp.cls == ERROR
        # Valid allocation, but data addressed to anyone other than the call's peer is dropped.
        await allocate(client, ("127.0.0.1", port), cred)
        tid = os.urandom(12)
        client.transport.sendto(
            build_stun(
                SEND,
                INDICATION,
                tid,
                [(A_XOR_PEER_ADDRESS, xor_address("100.64.9.9", 1234, tid)), (A_DATA, b"x")],
            ),
            ("127.0.0.1", port),
        )
        await asyncio.sleep(0.2)
        assert sent == []
    finally:
        relay.close()


def test_virtual_ip_is_stable_and_in_cgnat_range() -> None:
    ip = virtual_ip(A_HASH)
    assert ip == virtual_ip(A_HASH)
    assert ip.startswith("100.") and ip != virtual_ip(B_HASH)
