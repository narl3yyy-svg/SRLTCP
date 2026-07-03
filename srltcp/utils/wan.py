"""WAN endpoint validation and resolution for manual peer connections."""

from __future__ import annotations

import ipaddress
import re
import socket
from typing import NamedTuple

_HOST_RE = re.compile(
    r"^(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$"
)


class WanEndpoint(NamedTuple):
    host: str
    port: int
    resolved_ip: str


def _is_private_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
    )


def validate_wan_host(host: str) -> str:
    """Normalize and validate a manual WAN host (domain or public IP)."""
    cleaned = host.strip().lower().rstrip(".")
    if not cleaned or len(cleaned) > 253:
        raise ValueError("invalid host")
    if cleaned in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        raise ValueError("localhost not allowed for WAN peers")
    try:
        ip = ipaddress.ip_address(cleaned)
        if _is_private_ip(str(ip)):
            raise ValueError("private/reserved addresses are not valid WAN endpoints")
        return str(ip)
    except ValueError as exc:
        msg = str(exc)
        if "does not appear to be an IPv4 or IPv6 address" not in msg and (
            "private" in msg or "localhost" in msg
        ):
            raise
    if not _HOST_RE.match(cleaned):
        raise ValueError("invalid hostname")
    return cleaned


def validate_wan_port(port: int) -> int:
    if port < 1 or port > 65535:
        raise ValueError("port must be 1-65535")
    return port


def resolve_wan_endpoint(host: str, port: int) -> WanEndpoint:
    """Resolve hostname and ensure result is suitable for outbound WAN dial."""
    host = validate_wan_host(host)
    port = validate_wan_port(port)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"cannot resolve {host}: {exc}") from exc
    if not infos:
        raise ValueError(f"cannot resolve {host}")
    resolved = infos[0][4][0]
    if _is_private_ip(resolved):
        raise ValueError("resolved address is private — use LAN mode instead")
    return WanEndpoint(host=host, port=port, resolved_ip=resolved)