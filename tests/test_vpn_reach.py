"""Two devices on the same VPN from two different networks (laptop at work,
phone on 5G, both on the home WireGuard): they must find and dial each other
without Tor (a quiet work network has none)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from srltcp.core.trusted import MAX_ALT_HOSTS, TrustedPeer
from srltcp.utils import network
from tests.test_e2e_nodes import make_node

IFACES = [
    {"interface": "rmnet_data0", "ip": "10.44.7.9", "prefix_len": 28},  # mobile data
    {"interface": "wlan0", "ip": "192.168.1.5", "prefix_len": 24},
    {"interface": "v4-rmnet_data0", "ip": "192.0.0.4", "prefix_len": 29},  # 464XLAT
    {"interface": "tun0", "ip": "10.0.100.3", "prefix_len": 32},  # WireGuard
]


@pytest.fixture(autouse=True)
def interfaces(monkeypatch: Any) -> None:
    monkeypatch.setattr(network, "list_interfaces", lambda **_: [dict(i) for i in IFACES])
    monkeypatch.setattr(network, "global_ipv6_addresses", lambda: [])


async def test_shared_addresses_put_the_vpn_first_and_skip_mobile_data(tmp_path: Path) -> None:
    node = make_node(tmp_path, "phone")
    assert node.local_candidates() == ["10.0.100.3", "192.168.1.5"]


async def test_a_contacts_vpn_address_is_not_pushed_out(tmp_path: Path) -> None:
    node = make_node(tmp_path, "laptop")
    node.trusted.add(TrustedPeer(hash_id="b" * 32, name="phone", public_key="",
                                 tcp_host="192.168.1.50", tcp_port=7825))
    node.store_peer_addresses("b" * 32, {"a": ["10.0.100.9"]})  # while on the VPN
    for i in range(3):  # later: other Wi-Fis
        node.store_peer_addresses("b" * 32, {"a": [f"172.20.{i}.5"]})
    alts = node.trusted.get("b" * 32).alt_hosts  # type: ignore[union-attr]
    assert "10.0.100.9" in alts and len(alts) <= MAX_ALT_HOSTS
    assert ("10.0.100.9", 7825) in node._direct_candidates("b" * 32)


async def test_announce_from_a_contact_dials_it_even_after_giving_up(
    tmp_path: Path, monkeypatch: Any
) -> None:
    laptop, phone = make_node(tmp_path, "laptop"), make_node(tmp_path, "phone")
    await laptop.start()
    await phone.start()
    try:
        ident = phone.identities["tcp"]
        laptop.trusted.add(TrustedPeer(hash_id=ident.hash_id, name="phone",
                                       public_key=ident.public_bytes().hex(),
                                       tcp_host="192.168.1.50", tcp_port=7825))
        laptop._reach_paused.add(ident.hash_id)  # tried for 2 minutes and stopped
        port = phone.config.tcp_port or 7825
        laptop.__dict__["_dial_held"] = {("10.0.100.9", port): 1e12}
        kicked: list[str] = []
        monkeypatch.setattr(laptop, "_kick_connect", lambda h, *a, **k: kicked.append(h))
        payload = phone.build_announce_payload("tcp")
        await laptop._handle_discovered("10.0.100.9:7826", "tcp", payload)
        assert kicked == [ident.hash_id]
        assert ident.hash_id not in laptop._reach_paused
        assert ("10.0.100.9", port) not in laptop.__dict__["_dial_held"]
        assert laptop.trusted.get(ident.hash_id).tcp_host == "10.0.100.9"  # type: ignore[union-attr]
    finally:
        await laptop.stop()
        await phone.stop()


async def test_announce_from_a_blocked_contact_dials_nothing(
    tmp_path: Path, monkeypatch: Any
) -> None:
    laptop, phone = make_node(tmp_path, "laptop"), make_node(tmp_path, "phone")
    await laptop.start()
    await phone.start()
    try:
        ident = phone.identities["tcp"]
        laptop.trusted.add(TrustedPeer(hash_id=ident.hash_id, name="phone",
                                       public_key=ident.public_bytes().hex(),
                                       tcp_host="192.168.1.50", tcp_port=7825))
        laptop.trusted.update(ident.hash_id, blocked=True)
        kicked: list[str] = []
        monkeypatch.setattr(laptop, "_kick_connect", lambda h, *a, **k: kicked.append(h))
        payload = phone.build_announce_payload("tcp")
        await laptop._handle_discovered("10.0.100.9:7826", "tcp", payload)
        assert kicked == []
    finally:
        await laptop.stop()
        await phone.stop()


async def test_find_by_ip_dials_the_contact_at_that_address(
    tmp_path: Path, monkeypatch: Any
) -> None:
    node = make_node(tmp_path, "laptop")
    await node.start()
    try:
        node.trusted.add(TrustedPeer(hash_id="b" * 32, name="phone", public_key="",
                                     tcp_host="192.168.1.50", tcp_port=7825,
                                     alt_hosts=["10.0.100.9"]))
        node._reach_paused.add("b" * 32)

        async def unicast(payload: bytes, hosts: list[str], ports: Any = None) -> int:
            return 1

        monkeypatch.setattr(node.tcp_transport, "unicast_discovery", unicast)
        kicked: list[str] = []
        monkeypatch.setattr(node, "_kick_connect", lambda h, *a, **k: kicked.append(h))
        await node.announce_to("10.0.100.9")
        assert kicked == ["b" * 32]
        assert "b" * 32 not in node._reach_paused
        kicked.clear()
        await node.announce_to("192.168.1.77")  # nobody we know there
        assert kicked == []
    finally:
        await node.stop()


def test_route_check_is_only_for_private_ipv4() -> None:
    import ipaddress

    for far in ("8.8.8.8", "127.0.0.1", "169.254.1.1", "2a01:4f8::1"):
        assert not network._routed_from_own_subnet(ipaddress.ip_address(far))
