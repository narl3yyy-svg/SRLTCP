"""Hole punching: what a contact can and can't make us do."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from srltcp.core.messaging.punch import (
    MAX_CANDIDATES,
    PunchMixin,
    _clean_candidates,
    punchable_candidate,
)
from srltcp.core.protocol.messages import encode_payload


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

    def __init__(self, me: str, trusted: set[str], transport: str = "relay") -> None:
        self._init_punch()
        self.me = me
        self.trusted_ids = trusted
        self.link = SimpleNamespace(transport=transport, handshake_complete=True)
        self.sent: list[tuple[str, str]] = []
        self.rounds: list[tuple[str, bool]] = []

    def is_trusted(self, hash_id: str) -> bool:
        return hash_id in self.trusted_ids

    def get_link(self, hash_id: str) -> Any:
        return self.link

    def _me_hash(self) -> str:
        return self.me

    def public_candidates(self) -> list[tuple[str, int]]:
        return [("198.51.100.2", 7828)]

    async def _send_punch(self, hash_id: str, phase: str, cands: Any) -> None:  # type: ignore[override]
        self.sent.append((hash_id, phase))

    async def _punch(self, hash_id: str, targets: Any, *, leader: bool) -> None:  # type: ignore[override]
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
