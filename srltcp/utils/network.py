"""Network interface enumeration."""

from __future__ import annotations

import ipaddress
import os
import socket
import struct
from typing import Any


def broadcast_targets(prefix_len: int | None = None) -> list[str]:
    """Return UDP broadcast addresses for LAN discovery."""
    targets: set[str] = {"255.255.255.255"}
    for iface in list_interfaces():
        ip = iface.get("ip", "")
        if not ip or ip.startswith("127."):
            continue
        plen = iface.get("prefix_len", prefix_len if prefix_len is not None else 24)
        try:
            net = ipaddress.ip_network(f"{ip}/{plen}", strict=False)
            targets.add(str(net.broadcast_address))
        except ValueError:
            continue
    return sorted(targets)


def primary_ipv4() -> str:
    """Best-effort primary LAN IPv4 for announce payloads."""
    for iface in list_interfaces():
        ip = str(iface.get("ip", "") or "")
        if ip and not ip.startswith("127."):
            return ip
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = str(s.getsockname()[0])
        s.close()
        if not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return "127.0.0.1"


def list_interfaces() -> list[dict[str, Any]]:
    """Return IPv4 addresses with netmasks when available."""
    if os.name == "posix" and os.path.isdir("/sys/class/net"):
        linux = _linux_interfaces()
        if linux:
            return linux
    return _fallback_interfaces()


def _netmask_to_prefix(netmask: str) -> int:
    return ipaddress.ip_network(f"0.0.0.0/{netmask}", strict=False).prefixlen


def _linux_interfaces() -> list[dict[str, Any]]:
    """Enumerate IPv4 interfaces via ioctl (Arch/Ubuntu LAN discovery)."""
    import fcntl

    SIOCGIFADDR = 0x8915
    SIOCGIFNETMASK = 0x891B
    SIOCGIFFLAGS = 0x8913
    IFF_UP = 0x1
    IFF_LOOPBACK = 0x8

    results: list[dict[str, Any]] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for iface in sorted(os.listdir("/sys/class/net")):
            if iface == "lo":
                continue
            try:
                ifr = struct.pack("256s", iface.encode("utf-8")[:15])
                flags = struct.unpack("H", fcntl.ioctl(sock, SIOCGIFFLAGS, ifr)[16:18])[0]
                if not (flags & IFF_UP) or (flags & IFF_LOOPBACK):
                    continue
                ifr = struct.pack("256s", iface.encode("utf-8")[:15])
                ip = socket.inet_ntoa(fcntl.ioctl(sock, SIOCGIFADDR, ifr)[20:24])
                ifr = struct.pack("256s", iface.encode("utf-8")[:15])
                netmask = socket.inet_ntoa(fcntl.ioctl(sock, SIOCGIFNETMASK, ifr)[20:24])
                if ip.startswith("127."):
                    continue
                results.append(
                    {
                        "interface": iface,
                        "ip": ip,
                        "netmask": netmask,
                        "prefix_len": _netmask_to_prefix(netmask),
                        "label": f"{iface} ({ip})",
                    }
                )
            except OSError:
                continue
    finally:
        sock.close()
    return results


def _fallback_interfaces() -> list[dict[str, Any]]:
    """Socket-based fallback when interface ioctl is unavailable."""
    ips: set[str] = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = str(info[4][0])
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
    return [{"interface": "default", "ip": ip, "prefix_len": 24, "label": ip} for ip in sorted(ips)]