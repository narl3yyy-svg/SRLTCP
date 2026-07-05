"""WAN endpoint validation tests."""

from __future__ import annotations

import pytest

from srltcp.utils.wan import (
    resolve_wan_endpoint,
    validate_wan_host,
    validate_wan_port,
)


def test_validate_wan_host_public_ip() -> None:
    assert validate_wan_host("8.8.8.8") == "8.8.8.8"


def test_validate_wan_host_domain() -> None:
    assert validate_wan_host("peer.example.com") == "peer.example.com"


def test_validate_wan_host_rejects_private() -> None:
    with pytest.raises(ValueError, match="private"):
        validate_wan_host("192.168.1.1")
    with pytest.raises(ValueError, match="private"):
        validate_wan_host("10.0.0.5")


def test_validate_wan_host_rejects_localhost() -> None:
    with pytest.raises(ValueError, match="localhost"):
        validate_wan_host("127.0.0.1")
    with pytest.raises(ValueError, match="localhost"):
        validate_wan_host("localhost")


def test_validate_wan_port_range() -> None:
    assert validate_wan_port(7825) == 7825
    with pytest.raises(ValueError):
        validate_wan_port(0)
    with pytest.raises(ValueError):
        validate_wan_port(70000)


def test_resolve_wan_endpoint_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "srltcp.utils.wan.socket.getaddrinfo",
        lambda host, port, **_: [(None, None, None, None, ("8.8.8.8", port))],
    )
    ep = resolve_wan_endpoint("8.8.8.8", 7825)
    assert ep.host == "8.8.8.8"
    assert ep.port == 7825
    assert ep.resolved_ip == "8.8.8.8"


def test_resolve_wan_endpoint_rejects_private_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "srltcp.utils.wan.socket.getaddrinfo",
        lambda host, port, **_: [(None, None, None, None, ("10.0.0.1", port))],
    )
    with pytest.raises(ValueError, match="private"):
        resolve_wan_endpoint("evil.example.com", 7825)