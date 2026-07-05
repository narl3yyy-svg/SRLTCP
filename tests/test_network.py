"""Network helper tests."""

from __future__ import annotations

from srltcp.utils import network


def test_broadcast_targets_includes_limited_broadcast() -> None:
    targets = network.broadcast_targets()
    assert "255.255.255.255" in targets


def test_primary_ipv4_not_loopback_when_interfaces_exist(monkeypatch) -> None:
    monkeypatch.setattr(
        network,
        "list_interfaces",
        lambda: [{"interface": "eth0", "ip": "192.168.1.50", "prefix_len": 24}],
    )
    assert network.primary_ipv4() == "192.168.1.50"


def test_broadcast_uses_interface_prefix(monkeypatch) -> None:
    monkeypatch.setattr(
        network,
        "list_interfaces",
        lambda: [{"interface": "wlan0", "ip": "192.168.50.10", "prefix_len": 24}],
    )
    targets = network.broadcast_targets()
    assert "192.168.50.255" in targets