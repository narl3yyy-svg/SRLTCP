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


VPN_IFACE_PREFIXES = ("tun", "wg", "ppp", "utun", "tailscale", "zt", "ipsec", "nordlynx")
MAX_SWEEP_HOSTS = 1022  # never unicast-sweep more than a /22


def is_vpn_interface(iface: dict[str, Any]) -> bool:
    """WireGuard/OpenVPN/Tailscale-style tunnels: routed, no broadcast."""
    name = str(iface.get("interface", "")).lower()
    if name.startswith(VPN_IFACE_PREFIXES):
        return True
    return int(iface.get("prefix_len", 24) or 24) >= 31


def sweep_targets(own_ips: set[str] | None = None) -> list[str]:
    """Unicast addresses to announce to on VPN interfaces.

    VPNs do not carry broadcast packets, so discovery over WireGuard needs a
    direct packet to every address in the tunnel subnet. Tunnels configured
    with a /32 address get the surrounding /24 swept (the common setup).
    """
    targets: list[str] = []
    seen: set[str] = set(own_ips or ())
    for iface in list_interfaces():
        if not is_vpn_interface(iface):
            continue
        ip = str(iface.get("ip", ""))
        if not ip or ip.startswith("127."):
            continue
        plen = int(iface.get("prefix_len", 24) or 24)
        if plen >= 31 or plen < 22:
            plen = 24
        try:
            net = ipaddress.ip_network(f"{ip}/{plen}", strict=False)
        except ValueError:
            continue
        seen.add(ip)
        for host in net.hosts():
            h = str(host)
            if h not in seen:
                seen.add(h)
                targets.append(h)
            if len(targets) >= MAX_SWEEP_HOSTS:
                return targets
    return targets


def in_vpn_subnet(addr: str) -> bool:
    """True when addr lies inside one of our VPN tunnels' subnets."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    for iface in list_interfaces():
        if not is_vpn_interface(iface):
            continue
        plen = int(iface.get("prefix_len", 24) or 24)
        if plen >= 31 or plen < 16:
            plen = 24
        try:
            if ip in ipaddress.ip_network(f"{iface.get('ip')}/{plen}", strict=False):
                return True
        except ValueError:
            continue
    return False


def _android_interfaces() -> list[dict[str, Any]]:
    """Enumerate interfaces through Java on Android (sees tun0 / wlan0 / rmnet)."""
    from java.net import NetworkInterface

    results: list[dict[str, Any]] = []
    ifaces = NetworkInterface.getNetworkInterfaces()
    while ifaces is not None and ifaces.hasMoreElements():
        ni = ifaces.nextElement()
        try:
            if not ni.isUp() or ni.isLoopback():
                continue
            name = str(ni.getName())
            for ia in ni.getInterfaceAddresses():
                addr = ia.getAddress()
                if addr is None:
                    continue
                ip = str(addr.getHostAddress())
                if ":" in ip or ip.startswith("127."):
                    continue
                plen = int(ia.getNetworkPrefixLength())
                results.append(
                    {
                        "interface": name,
                        "ip": ip,
                        "prefix_len": plen,
                        "label": f"{name} ({ip})",
                    }
                )
        except Exception:  # noqa: BLE001 - keep enumerating other interfaces
            continue
    # VPN first: on a phone that is the address peers can actually reach.
    results.sort(key=lambda i: (0 if is_vpn_interface(i) else 1, i["interface"]))
    return results


_IFACE_CACHE: dict[str, Any] = {"at": 0.0, "value": []}
_IFACE_TTL = 5.0


def list_interfaces() -> list[dict[str, Any]]:
    """Interfaces, cached briefly (consulted on every incoming announce)."""
    import time

    now = time.monotonic()
    if _IFACE_CACHE["value"] and now - _IFACE_CACHE["at"] < _IFACE_TTL:
        return [dict(i) for i in _IFACE_CACHE["value"]]
    value = _list_interfaces_uncached()
    _IFACE_CACHE["at"] = now
    _IFACE_CACHE["value"] = value
    return [dict(i) for i in value]


def _list_interfaces_uncached() -> list[dict[str, Any]]:
    """Return IPv4 addresses with netmasks when available.

    Android: Java NetworkInterface (Python cannot read /sys/class/net there).
    Desktop (Arch/Ubuntu) keeps the fast ioctl method.
    """
    from srltcp.utils.platform import is_android

    if is_android():
        try:
            android = _android_interfaces()
            if android:
                return android
        except Exception:  # noqa: BLE001 - fall back to socket tricks
            pass
    if os.name == "posix":
        try:
            if os.path.isdir("/sys/class/net"):
                linux = _linux_interfaces()
                if linux:
                    return linux
        except (PermissionError, OSError):
            # On Android we usually can't read /sys/class/net
            pass

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


def global_ipv6_addresses() -> list[str]:
    """This device's public (global unicast) IPv6 addresses.

    IPv6 has no NAT: a phone on a carrier or a home network usually has one,
    and two devices can open a direct connection between them by dialing each
    other at the same time (see relay.py hole punching).
    """
    found: list[str] = []

    def add(raw: str) -> None:
        raw = raw.split("%", 1)[0]
        try:
            ip = ipaddress.IPv6Address(raw)
        except ValueError:
            return
        if ip.is_global and not ip.ipv4_mapped and str(ip) not in found:
            found.append(str(ip))

    from srltcp.utils.platform import is_android

    if is_android():
        try:
            from java.net import NetworkInterface

            ifaces = NetworkInterface.getNetworkInterfaces()
            while ifaces is not None and ifaces.hasMoreElements():
                ni = ifaces.nextElement()
                if not ni.isUp() or ni.isLoopback():
                    continue
                for ia in ni.getInterfaceAddresses():
                    addr = ia.getAddress()
                    if addr is not None and ":" in str(addr.getHostAddress()):
                        add(str(addr.getHostAddress()))
        except Exception:  # noqa: BLE001 - best effort
            pass
    elif os.path.exists("/proc/net/if_inet6"):
        try:
            with open("/proc/net/if_inet6", encoding="ascii") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 6 and parts[5] != "lo":
                        add(":".join(parts[0][i : i + 4] for i in range(0, 32, 4)))
        except OSError:
            pass
    if not found:
        try:  # which source address would reach the IPv6 internet (no packet sent)
            s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            s.connect(("2001:db8::1", 9))
            add(s.getsockname()[0])
            s.close()
        except OSError:
            pass
    return found[:4]
