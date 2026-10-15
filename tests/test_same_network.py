"""Discovery stays on our own networks: nobody on another network (mobile
carrier, the internet) can find this device, and it never looks there."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from srltcp.core.messaging.announce import AnnounceError
from srltcp.utils import network
from tests.test_e2e_nodes import make_node

IFACES = [
    {"interface": "wlan0", "ip": "192.168.1.5", "prefix_len": 24},
    {"interface": "tun0", "ip": "10.0.100.3", "prefix_len": 32},  # WireGuard
    {"interface": "rmnet_data0", "ip": "10.44.7.9", "prefix_len": 28},  # mobile data
]


@pytest.fixture(autouse=True)
def interfaces(monkeypatch: Any) -> None:
    monkeypatch.setattr(network, "list_interfaces", lambda **_: [dict(i) for i in IFACES])
    monkeypatch.setattr(network, "global_ipv6_addresses", lambda: ["2a01:4f8:1:2::abcd"])


def test_same_network() -> None:
    for ok in ("192.168.1.77", "10.0.100.9", "127.0.0.1", "fe80::1", "2a01:4f8:1:2::99",
               "::ffff:192.168.1.20"):
        assert network.same_network(ok), ok
    for far in ("10.44.7.1",  # the carrier's network (mobile data)
                "8.8.8.8", "198.51.100.2", "192.168.2.10", "2a01:4f8:9::1", "not-an-ip"):
        assert not network.same_network(far), far


def test_no_broadcast_on_mobile_data(monkeypatch: Any) -> None:
    assert "192.168.1.255" in network.broadcast_targets()
    assert not any(t.startswith("10.44.") for t in network.broadcast_targets())
    monkeypatch.setattr(network, "list_interfaces", lambda **_: [dict(IFACES[2])])
    assert network.broadcast_targets() == []  # only mobile data: nothing goes out


async def test_find_by_ip_only_on_our_networks(tmp_path: Path, monkeypatch: Any) -> None:
    node = make_node(tmp_path, "alice")
    await node.start()
    try:
        sent: list[list[str]] = []

        async def unicast(payload: bytes, hosts: list[str], ports: Any = None) -> int:
            sent.append(hosts)
            return 1

        monkeypatch.setattr(node.tcp_transport, "unicast_discovery", unicast)
        with pytest.raises(AnnounceError, match="own network"):
            await node.announce_to("8.8.8.8")
        assert sent == []
        await node.announce_to("192.168.1.40")
        assert sent and sent[0] == ["192.168.1.40"]
    finally:
        await node.stop()


async def test_announces_and_contact_requests_from_elsewhere_are_ignored(
    tmp_path: Path, monkeypatch: Any
) -> None:
    alice, bob = make_node(tmp_path, "alice"), make_node(tmp_path, "bob")
    await alice.start()
    await bob.start()
    try:
        payload = bob.build_announce_payload("tcp")
        await alice._handle_discovered("203.0.113.7:7826", "tcp", payload)
        assert not alice.discovery.list_peers()  # a stranger on the internet: nothing shown
        await alice._handle_discovered("192.168.1.40:7826", "tcp", payload)
        assert [p.hash_id for p in alice.discovery.list_peers()] == [bob.identities["tcp"].hash_id]
        alice.discovery.remove(bob.identities["tcp"].hash_id)
        far = SimpleNamespace(hash_id="b" * 32, peer_name="x", transport="tcp",
                              transport_peer_id="p", address="203.0.113.7:51000",
                              public_key=b"\0" * 32)
        await alice._raise_contact_request(far)  # type: ignore[arg-type]
        assert not alice.discovery.list_peers()
        near = SimpleNamespace(**{**far.__dict__, "address": "192.168.1.40:51000"})
        await alice._raise_contact_request(near)  # type: ignore[arg-type]
        assert [p.hash_id for p in alice.discovery.list_peers()] == ["b" * 32]
    finally:
        await alice.stop()
        await bob.stop()
