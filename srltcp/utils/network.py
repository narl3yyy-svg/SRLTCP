"""Network interface enumeration."""

from __future__ import annotations

import ipaddress
import socket
from typing import Any


def broadcast_targets(prefix_len: int = 24) -> list[str]:
    """Return UDP broadcast addresses for LAN discovery."""
    targets: set[str] = {"255.255.255.255"}
    for iface in list_interfaces():
        ip = iface.get("ip", "")
        if not ip or ip.startswith("127."):
            continue
        try:
            net = ipaddress.ip_network(f"{ip}/{prefix_len}", strict=False)
            targets.add(str(net.broadcast_address))
        except ValueError:
            continue
    return sorted(targets)


def list_interfaces() -> list[dict[str, Any]]:
    """Return IPv4 addresses (stdlib only — no extra dependencies)."""
    return _fallback_interfaces()


def _fallback_interfaces() -> list[dict[str, Any]]:
    """Socket-based fallback when netifaces is unavailable."""
    ips: set[str] = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    if not ips:
        ips.add("127.0.0.1")
    return [{"interface": "default", "ip": ip, "label": ip} for ip in sorted(ips)]