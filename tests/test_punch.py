"""Hole punching: what a contact can and can't make us do."""

from __future__ import annotations

import asyncio
import os
import struct
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from srltcp.core.messaging.punch import (
    MAX_CANDIDATES,
    PunchMixin,
    _clean_candidates,
    _same_family,
    punchable_candidate,
)
from srltcp.core.protocol.messages import encode_payload
from tests.test_e2e_nodes import make_node, trust, wait_for


def test_punchable_candidates_are_public_addresses_only() -> None:
    assert punchable_candidate("198.51.100.2", 7828)
    assert punchable_candidate("8.8.8.8", 443)
    assert punchable_candidate("2a01:4f8::1", 7828)
    for host in (
        "192.168.1.1", "10.0.0.1", "172.16.5.4", "100.64.1.1",  # LAN / carrier NAT
        "127.0.0.1", "169.254.1.1", "224.0.0.1", "0.0.0.0", "255.255.255.255",
        "fd00::1", "fe80::1", "::1", "::ffff:192.168.1.1",
        "example.com", "",  # names are never dialed from a contact's hint
    ):
        assert not punchable_candidate(host, 7828), host
    assert not punchable_candidate("8.8.8.8", 0)
    assert not punchable_candidate("8.8.8.8", 70000)
    assert not punchable_candidate("8.8.8.8", "443")


def test_candidate_lists_are_bounded_and_cleaned() -> None:
    raw: list[Any] = [[f"203.0.113.{i}", 7828] for i in range(1, 50)]
    raw += [["192.168.1.1", 80], "junk", [1, 2, 3], None]
    out = _clean_candidates(raw)
    assert len(out) == MAX_CANDIDATES
    assert all(h.startswith("203.0.113.") for h, _ in out)
    assert _clean_candidates("not a list") == []
    assert _clean_candidates([["8.8.8.8", 1]] * 5) == [("8.8.8.8", 1)]


class _Node(PunchMixin):
    """Just enough backend for _handle_punch."""

    def __init__(
        self,
        me: str,
        trusted: set[str],
        transport: str = "relay",
        *,
        tor: bool = False,
        public: tuple[str, int] = ("198.51.100.2", 7828),
    ) -> None:
        self._init_punch()
        self.me = me
        self.trusted_ids = trusted
        self.link = SimpleNamespace(transport=transport, handshake_complete=True, tor=tor)
        self.public = public
        self.sent: list[tuple[str, str]] = []
        self.rounds: list[tuple[str, bool]] = []

    def is_trusted(self, hash_id: str) -> bool:
        return hash_id in self.trusted_ids

    def get_link(self, hash_id: str) -> Any:
        return self.link

    def _me_hash(self) -> str:
        return self.me

    def _link_on_tor(self, link: Any) -> bool:
        return bool(link.tor)

    def public_candidates(self) -> list[tuple[str, int]]:
        return [self.public]

    async def _send_punch(self, hash_id: str, phase: str, cands: Any, extra: Any = None) -> None:  # type: ignore[override]
        self.sent.append((hash_id, phase))

    async def _punch(self, hash_id: str, targets: Any, *, leader: bool, **_udp: Any) -> None:  # type: ignore[override]
        self.rounds.append((hash_id, leader))

    async def _prime(self, targets: Any) -> None:  # type: ignore[override]
        self.sent.append(("prime", str(len(targets))))


def _offer(cands: list[list[Any]]) -> bytes:
    return encode_payload({"ph": "o", "c": cands})


def test_offer_flood_starts_one_round() -> None:
    async def run() -> None:
        node = _Node(me="f" * 32, trusted={"a" * 32})
        for _ in range(50):
            await node._handle_punch("a" * 32, _offer([["198.51.101.2", 7828]]))  # type: ignore[arg-type]
        await asyncio.sleep(0)
        assert node.sent == [("prime", "1"), ("a" * 32, "a")]  # primed before answering
        assert node.rounds == [("a" * 32, False)]

    asyncio.run(run())


def test_offers_from_strangers_or_the_wrong_side_are_ignored() -> None:
    async def run() -> None:
        node = _Node(me="f" * 32, trusted=set())
        await node._handle_punch("a" * 32, _offer([["198.51.101.2", 7828]]))  # type: ignore[arg-type]
        # Only the lower hash offers: an offer from a higher hash is dropped.
        node2 = _Node(me="0" * 32, trusted={"a" * 32})
        await node2._handle_punch("a" * 32, _offer([["198.51.101.2", 7828]]))  # type: ignore[arg-type]
        # Already direct: nothing to punch.
        node3 = _Node(me="f" * 32, trusted={"a" * 32}, transport="tcp")
        await node3._handle_punch("a" * 32, _offer([["198.51.101.2", 7828]]))  # type: ignore[arg-type]
        await asyncio.sleep(0)
        for n in (node, node2, node3):
            assert n.sent == [] and n.rounds == []

    asyncio.run(run())


def test_contact_on_tor_punches_over_ipv6() -> None:
    async def run() -> None:
        node = _Node(me="f" * 32, trusted={"a" * 32}, transport="tcp", tor=True,
                     public=("2a01:4f8::b", 7828))
        await node._handle_punch("a" * 32, _offer([["198.51.101.2", 7828], ["2a01:4f8::a", 7828]]))  # type: ignore[arg-type]
        await asyncio.sleep(0)
        assert node.rounds == [("a" * 32, False)]

    asyncio.run(run())


def test_no_round_without_a_shared_address_family() -> None:
    async def run() -> None:
        # We only have IPv6, they only IPv4: answer (so they back off) but don't dial.
        node = _Node(me="f" * 32, trusted={"a" * 32}, transport="tcp", tor=True,
                     public=("2a01:4f8::b", 7828))
        await node._handle_punch("a" * 32, _offer([["198.51.101.2", 7828]]))  # type: ignore[arg-type]
        await asyncio.sleep(0)
        assert node.rounds == [] and node.sent == [("a" * 32, "a")]

    asyncio.run(run())
    v4, v6 = ("198.51.100.2", 1), ("2a01:4f8::1", 1)
    assert _same_family([v4, v6], [v6]) == [v6]
    assert _same_family([v4, v6], [v4]) == [v4]
    assert _same_family([v4], []) == []


def test_offer_with_only_lan_targets_dials_nothing() -> None:
    async def run() -> None:
        node = _Node(me="f" * 32, trusted={"a" * 32})
        await node._handle_punch("a" * 32, _offer([["192.168.1.1", 80], ["10.0.0.1", 22]]))  # type: ignore[arg-type]
        await asyncio.sleep(0)
        assert node.rounds == []

    asyncio.run(run())


def test_answer_without_our_offer_is_ignored() -> None:
    async def run() -> None:
        node = _Node(me="0" * 32, trusted={"a" * 32})
        body = encode_payload({"ph": "a", "c": [["198.51.101.2", 7828]]})
        await node._handle_punch("a" * 32, body)  # type: ignore[arg-type]
        await asyncio.sleep(0)
        assert node.rounds == []

    asyncio.run(run())


def test_mac_derived_ipv6_is_never_shared(monkeypatch: Any, tmp_path: Any) -> None:
    from srltcp.utils import network

    lines = [
        # global, EUI-64 (…02ff:fe…): must be skipped
        "2a010db80000000102163efffe123456 02 40 00 00 wlan0",
        # global, temporary (flag 0x01): preferred
        "2a010db8000000019a7c1d2e3f405162 02 40 00 01 wlan0",
        # global, deprecated (0x20): skipped
        "2a010db800000001aaaaaaaaaaaaaaaa 02 40 00 20 wlan0",
        # global, stable privacy address
        "2a010db8000000011234567812345678 02 40 00 00 wlan0",
    ]
    fake = tmp_path / "if_inet6"
    fake.write_text("\n".join(lines) + "\n")
    real_exists, real_open = network.os.path.exists, open

    monkeypatch.setattr(network, "os", SimpleNamespace(path=SimpleNamespace(
        exists=lambda p: True if p == "/proc/net/if_inet6" else real_exists(p))))
    def fake_open(p: Any, *a: Any, **k: Any) -> Any:
        return real_open(fake if p == "/proc/net/if_inet6" else p, *a, **k)

    monkeypatch.setattr("builtins.open", fake_open)
    got = network.global_ipv6_addresses()
    assert got == ["2a01:db8:0:1:9a7c:1d2e:3f40:5162", "2a01:db8:0:1:1234:5678:1234:5678"]


async def test_contact_on_tor_moves_to_a_punched_direct_path(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Two contacts talking over Tor, both with IPv6: the lower hash offers a
    punch round inside the Tor session, the connection that opens gets the
    full signed handshake and replaces the Tor link on both sides."""
    a, b = make_node(tmp_path, "alice"), make_node(tmp_path, "bob")
    await a.start()
    await b.start()
    try:
        trust(a, b)
        trust(b, a)
        ah, bh = a.identities["tcp"].hash_id, b.identities["tcp"].hash_id
        leader, follower = (a, b) if ah < bh else (b, a)
        lh, fh = (ah, bh) if leader is a else (bh, ah)
        await leader.connect_to_peer(fh)
        assert await leader.wait_for_handshake(fh, timeout=8)
        assert await wait_for(lambda: follower.get_link(lh) is not None
                              and follower.get_link(lh).handshake_complete)
        # Pretend that session runs over Tor (onion service on one side).
        tor_l, tor_f = leader.get_link(fh), follower.get_link(lh)
        assert leader.tcp_transport and follower.tcp_transport
        leader.tcp_transport._tor_peers.add(tor_l.transport_peer_id)
        follower.tcp_transport._tor_peers.add(tor_f.transport_peer_id)
        assert leader._link_on_tor(tor_l)

        for node, ip in ((leader, "2a01:4f8::a"), (follower, "2a01:4f8::b")):
            monkeypatch.setattr(node, "public_candidates", lambda ip=ip: [(ip, 7828)])
            monkeypatch.setattr(node, "punch_port", lambda: 7828)
            monkeypatch.setattr(node, "_direct_candidates", lambda _h: [])  # not on one LAN
            monkeypatch.setattr(node, "_prime", AsyncMock())
        leader._peer_public[fh] = [("2a01:4f8::b", 7828)]  # from their RENDEZVOUS

        dialed: list[list[tuple[str, int]]] = []

        async def leader_dial(targets: Any, port: int, *, delay: float = 0.0) -> str:
            # Stand-in for the simultaneous open: one TCP connection between the two.
            dialed.append(targets)
            return await leader.tcp_transport.connect("127.0.0.1", follower.config.tcp_port)

        async def follower_dial(targets: Any, port: int, *, delay: float = 0.0) -> None:
            dialed.append(targets)

        monkeypatch.setattr(leader, "_race_dials", leader_dial)
        monkeypatch.setattr(follower, "_race_dials", follower_dial)

        await leader._try_direct_upgrade(fh)
        assert await wait_for(lambda: (
            (link := leader.get_link(fh)) is not None and link.handshake_complete
            and link.transport_peer_id != tor_l.transport_peer_id
            and not leader._link_on_tor(link)
        ), timeout=8)
        assert await wait_for(lambda: (
            (link := follower.get_link(lh)) is not None and link.handshake_complete
            and link.transport_peer_id != tor_f.transport_peer_id
        ), timeout=8)
        assert sorted(map(tuple, dialed)) == [  # each side dialed the other's IPv6
            (("2a01:4f8::a", 7828),), (("2a01:4f8::b", 7828),)
        ]
        # The Tor connection is closed and messages flow on the new path.
        assert await wait_for(lambda: not leader.tcp_transport.has_peer(tor_l.transport_peer_id))
        await leader.send_message(fh, "over IPv6 now")
        assert await wait_for(
            lambda: any(m["text"] == "over IPv6 now" for m in follower.get_messages())
        )
    finally:
        await a.stop()
        await b.stop()


async def test_a_file_over_tor_looks_for_a_direct_path_at_once(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Tor is many times slower than a direct path: when a file starts over
    Tor, both sides try for a direct path right away (and forget earlier
    failed hole-punching rounds) instead of waiting for the next check."""
    a, b = make_node(tmp_path, "alice"), make_node(tmp_path, "bob")
    await a.start()
    await b.start()
    try:
        trust(a, b)
        trust(b, a)
        ah, bh = a.identities["tcp"].hash_id, b.identities["tcp"].hash_id
        await a.connect_to_peer(bh)
        assert await a.wait_for_handshake(bh, timeout=8)
        assert await wait_for(lambda: b.get_link(ah) is not None
                              and b.get_link(ah).handshake_complete)
        for node, other in ((a, bh), (b, ah)):
            link = node.get_link(other)
            node.tcp_transport._tor_peers.add(link.transport_peer_id)  # "over Tor"
        calls: list[tuple[str, str, bool]] = []
        for name, node in (("alice", a), ("bob", b)):
            monkeypatch.setattr(
                node, "request_direct_upgrade",
                lambda h, *, punch=False, name=name: calls.append((name, h, punch)),
            )
        src = tmp_path / "f.bin"
        src.write_bytes(os.urandom(300_000))
        await a.send_file(bh, src)
        assert await wait_for(lambda: {c[0] for c in calls} == {"alice", "bob"}, timeout=8)
        assert all(punch for _n, _h, punch in calls)
    finally:
        await a.stop()
        await b.stop()


def test_speed_advice_names_the_missing_piece() -> None:
    """Check connection explains why a contact on another network isn't direct."""
    peer = "a" * 32
    node = _Node(me="f" * 32, trusted={peer})
    node.config = SimpleNamespace(direct_internet=True)  # type: ignore[attr-defined]
    node.tor_circuits = lambda h: 9  # type: ignore[method-assign]
    node.public_candidates = lambda: []  # type: ignore[method-assign]
    sp = node._speed_advice(peer, "tor")  # type: ignore[arg-type]
    assert not sp["my_public"] and "STUN" in sp["reason"]
    assert any("relay" in t for t in sp["tips"]) and sp["tor_circuits"] == 9
    node.public_candidates = lambda: [("198.51.100.7", 7828)]  # type: ignore[method-assign]
    sp = node._speed_advice(peer, "tor")  # type: ignore[arg-type]
    assert sp["my_public"] and not sp["their_public"] and "contact" in sp["reason"]
    node._peer_udp[peer] = [("2a01:4f8::a", 7828)]
    node._punch_failures[peer] = 2
    sp = node._speed_advice(peer, "tor")  # type: ignore[arg-type]
    assert "block" in sp["reason"] and sp["their_ipv6"] and any("APN" in t for t in sp["tips"])
    assert node._speed_advice(peer, "direct (udp)")["reason"].startswith("Direct")  # type: ignore[arg-type]
    node.config.direct_internet = False  # type: ignore[attr-defined]
    assert "off" in node._speed_advice(peer, "tor")["reason"]  # type: ignore[arg-type]


async def test_contacts_on_tor_move_to_direct_udp(tmp_path: Path, monkeypatch: Any) -> None:
    """The fast path: two contacts on Tor swap UDP candidates and key shares
    inside the session, punch a real UDP connection (here on this machine),
    and the session moves onto it: messages, a file and call media flow."""
    import socket

    import srltcp.core.messaging.punch as punch_mod

    monkeypatch.setattr(punch_mod, "punchable_candidate", lambda h, p: True)  # loopback here
    a, b = make_node(tmp_path, "alice"), make_node(tmp_path, "bob")
    await a.start()
    await b.start()
    try:
        trust(a, b)
        trust(b, a)
        ah, bh = a.identities["tcp"].hash_id, b.identities["tcp"].hash_id
        leader, follower = (a, b) if ah < bh else (b, a)
        lh, fh = (ah, bh) if leader is a else (bh, ah)
        await leader.connect_to_peer(fh)
        assert await leader.wait_for_handshake(fh, timeout=8)
        assert await wait_for(lambda: follower.get_link(lh) is not None
                              and follower.get_link(lh).handshake_complete)
        tor_l, tor_f = leader.get_link(fh), follower.get_link(lh)
        leader.tcp_transport._tor_peers.add(tor_l.transport_peer_id)  # "over Tor"
        follower.tcp_transport._tor_peers.add(tor_f.transport_peer_id)
        for node in (leader, follower):
            assert node.udp is not None
            node.udp.mapped[socket.AF_INET] = ("127.0.0.1", node.udp.ports[socket.AF_INET])
            monkeypatch.setattr(node, "_refresh_udp", AsyncMock())  # no STUN from tests
            monkeypatch.setattr(node, "public_candidates", lambda: [])  # UDP only
            monkeypatch.setattr(node, "_direct_candidates", lambda _h: [])
        await follower.send_rendezvous(lh)  # carries its UDP candidates ("w")
        assert await wait_for(lambda: bool(leader._peer_udp.get(fh)))

        await leader._try_direct_upgrade(fh)
        for node, other, old in ((leader, fh, tor_l), (follower, lh, tor_f)):
            assert await wait_for(lambda node=node, other=other, old=old: (
                (link := node.get_link(other)) is not None and link.handshake_complete
                and link.transport_peer_id != old.transport_peer_id
                and link.address.startswith("udp:")
            ), timeout=10)
        await leader.send_message(fh, "over UDP now")
        assert await wait_for(
            lambda: any(m["text"] == "over UDP now" for m in follower.get_messages())
        )
        src = tmp_path / "big.bin"
        src.write_bytes(os.urandom(12_000_000))
        sent = await leader.send_file(fh, src)
        tid = sent["id"]
        assert await wait_for(lambda: tid in follower._transfers
                              and follower._transfers[tid].state == "complete", timeout=30)
        assert follower._incoming_paths[tid].read_bytes() == src.read_bytes()
        # Call media goes as datagrams, outside the ordered stream.
        got: list[tuple[str, bytes]] = []
        monkeypatch.setattr(follower, "_handle_call_media",
                            lambda h, body: got.append((h, body)))
        await leader._send_call_media(fh, 5000, 6000, b"rtp")
        assert await wait_for(lambda: got == [(lh, struct.pack(">HH", 5000, 6000) + b"rtp")])
    finally:
        await a.stop()
        await b.stop()
