"""Ask the home router to forward our UDP port (UPnP IGD or NAT-PMP).

Why it matters: a phone on a home Wi-Fi and one on mobile data. Carrier NAT
often gives a new public port for every destination, so the port the mobile
phone learnt from STUN isn't the one its packets come from; and most home
routers only let in replies from the exact address and port the phone inside
sent to. Neither side's packets get through, while two carriers (or two homes)
connect fine. With a port mapping on the home router, packets to our port are
let in from any port: the mobile phone's probes arrive, the first one that
authenticates fixes the path and the home phone answers it (as ICE does with
"peer-reflexive" candidates).

Only routers on our own Wi-Fi/LAN are asked, the mapping points only at our
UDP socket (which drops anything that isn't an authenticated packet from a
contact we're connecting to), it expires by itself (renewed while we run) and
is removed when the app stops. Off in Settings → Network when unwanted.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import re
import secrets
import socket
import struct
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

from srltcp.utils.logging import get_logger

log = get_logger(__name__)

LEASE = 3600  # seconds; renewed at half time
RETRY_AFTER = 600.0  # after finding no router that maps, try again this much later
NATPMP_PORT = 5351
SSDP_ADDR = ("239.255.255.250", 1900)
SSDP_WAIT = 2.0
HTTP_TIMEOUT = 4.0
MAX_XML = 64 * 1024
DESCRIPTION = "SRLTCP direct connections"
WAN_SERVICES = (
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)


@dataclass
class Mapping:
    method: str  # "UPnP" or "NAT-PMP"
    external: tuple[str, int]
    internal_port: int
    lifetime: int  # seconds; 0 = until removed
    obtained: float  # monotonic
    release: Callable[[], Awaitable[None]]

    def due(self, now: float) -> bool:
        return bool(self.lifetime) and now - self.obtained > self.lifetime / 2


def home_lan_ipv4() -> list[str]:
    """Our private IPv4 addresses on a Wi-Fi/Ethernet LAN (not mobile data, not
    a VPN): where a home router can be."""
    from srltcp.utils.network import is_cellular_interface, is_vpn_interface, list_interfaces

    out = []
    for iface in list_interfaces():
        ip = str(iface.get("ip", ""))
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if (
            addr.version == 4 and addr.is_private and not addr.is_loopback
            and not addr.is_link_local and not is_cellular_interface(iface)
            and not is_vpn_interface(iface) and addr not in ipaddress.ip_network("100.64.0.0/10")
        ):
            out.append(ip)
    return out


def _on_our_lan(host: str) -> bool:
    """A router address we may talk to: private, on one of our LANs."""
    from srltcp.utils.network import same_network

    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.version == 4 and addr.is_private and not addr.is_loopback and same_network(host)


def _local_ip_towards(host: str) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((host, 9))  # (no packet is sent: picks the outgoing interface)
        return str(s.getsockname()[0])


def _public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.version == 4 and addr.is_global


# ── NAT-PMP (RFC 6886) ──


class _Datagram(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.replies: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue()

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.replies.put_nowait((data, (str(addr[0]), int(addr[1]))))


async def _natpmp_request(gateway: str, request: bytes, size: int) -> bytes | None:
    loop = asyncio.get_running_loop()
    transport, proto = await loop.create_datagram_endpoint(
        _Datagram, remote_addr=(gateway, NATPMP_PORT)
    )
    try:
        for wait in (0.25, 0.5, 1.0):  # the RFC's retry schedule, shortened
            transport.sendto(request)
            deadline = loop.time() + wait
            while (left := deadline - loop.time()) > 0:
                try:
                    data, addr = await asyncio.wait_for(proto.replies.get(), left)
                except TimeoutError:
                    break
                if addr[0] == gateway and len(data) >= size and data[1] == request[1] + 128:
                    return data
        return None
    finally:
        transport.close()


async def natpmp_map(gateway: str, internal_port: int, lifetime: int = LEASE) -> Mapping | None:
    reply = await _natpmp_request(gateway, b"\x00\x00", 12)
    if reply is None or struct.unpack_from(">H", reply, 2)[0] != 0:
        return None
    external_ip = socket.inet_ntoa(reply[8:12])
    request = struct.pack(">BBHHHI", 0, 1, 0, internal_port, internal_port, lifetime)
    reply = await _natpmp_request(gateway, request, 16)
    if reply is None:
        return None
    _v, _op, result, _epoch, _internal, external_port, granted = struct.unpack(
        ">BBHIHHI", reply[:16]
    )
    if result != 0 or not external_port:
        return None

    async def release() -> None:
        with contextlib.suppress(OSError):
            await _natpmp_request(
                gateway, struct.pack(">BBHHHI", 0, 1, 0, internal_port, 0, 0), 16
            )

    return Mapping("NAT-PMP", (external_ip, external_port), internal_port, granted,
                   time.monotonic(), release)


# ── UPnP Internet Gateway Device ──


async def _ssdp_locations() -> list[str]:
    """Ask the LAN for Internet gateways; their description URLs."""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.setblocking(False)
    sock.bind(("0.0.0.0", 0))
    transport, proto = await loop.create_datagram_endpoint(_Datagram, sock=sock)
    try:
        for target in ("urn:schemas-upnp-org:device:InternetGatewayDevice:1",
                       "urn:schemas-upnp-org:device:InternetGatewayDevice:2"):
            transport.sendto((
                "M-SEARCH * HTTP/1.1\r\n"
                f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
                'MAN: "ssdp:discover"\r\nMX: 1\r\n'
                f"ST: {target}\r\n\r\n"
            ).encode(), SSDP_ADDR)
        found: list[str] = []
        deadline = loop.time() + SSDP_WAIT
        while (left := deadline - loop.time()) > 0:
            try:
                data, addr = await asyncio.wait_for(proto.replies.get(), left)
            except TimeoutError:
                break
            match = re.search(rb"(?im)^location:\s*(\S+)\s*$", data[:4096])
            if not match:
                continue
            url = match.group(1).decode("ascii", "replace")
            parts = urlsplit(url)
            # Only the router that answered, on our LAN, over plain HTTP.
            if (
                parts.scheme == "http" and parts.hostname == addr[0] and _on_our_lan(addr[0])
                and url not in found
            ):
                found.append(url)
        return found
    finally:
        transport.close()


async def _http(method: str, url: str, body: bytes = b"", headers: dict[str, str] | None = None
                ) -> tuple[int, bytes]:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session, session.request(
        method, url, data=body or None, headers=headers, allow_redirects=False
    ) as resp:
        data = await resp.content.read(MAX_XML + 1)
        if len(data) > MAX_XML:
            raise ValueError("router answer too large")
        return resp.status, data


def _strip_ns(root: ElementTree.Element) -> None:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


async def _wan_service(location: str) -> tuple[str, str] | None:
    """(control URL, service type) of the router's WAN connection service."""
    status, data = await _http("GET", location)
    if status != 200:
        return None
    root = ElementTree.fromstring(data)
    _strip_ns(root)
    base = root.findtext("URLBase") or location
    services = {
        (svc.findtext("serviceType") or "").strip(): (svc.findtext("controlURL") or "").strip()
        for svc in root.iter("service")
    }
    for kind in WAN_SERVICES:
        control = services.get(kind)
        if control:
            url = urljoin(base, control)
            if urlsplit(url).hostname == urlsplit(location).hostname:  # same router only
                return url, kind
    return None


async def _soap(control: str, service: str, action: str, args: dict[str, Any]
                ) -> tuple[int, dict[str, str]]:
    params = "".join(f"<{k}>{v}</{k}>" for k, v in args.items())
    body = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action} xmlns:u="{service}">{params}</u:{action}>'
        "</s:Body></s:Envelope>"
    ).encode()
    status, data = await _http("POST", control, body, {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPAction": f'"{service}#{action}"',
    })
    fields: dict[str, str] = {}
    with contextlib.suppress(ElementTree.ParseError):
        root = ElementTree.fromstring(data)
        _strip_ns(root)
        fields = {el.tag: (el.text or "").strip() for el in root.iter() if isinstance(el.tag, str)}
    return status, fields


async def _add_port_mapping(
    control: str, service: str, external_port: int, internal_port: int, client: str, lease: int
) -> tuple[bool, str]:
    """(mapped, UPnP error code)."""
    status, info = await _soap(control, service, "AddPortMapping", {
        "NewRemoteHost": "", "NewExternalPort": external_port, "NewProtocol": "UDP",
        "NewInternalPort": internal_port, "NewInternalClient": client, "NewEnabled": 1,
        "NewPortMappingDescription": DESCRIPTION, "NewLeaseDuration": lease,
    })
    return status == 200, info.get("errorCode", "")


async def _upnp_map_at(location: str, internal_port: int, lifetime: int) -> Mapping | None:
    found = await _wan_service(location)
    if not found:
        return None
    control, service = found
    _status, info = await _soap(control, service, "GetExternalIPAddress", {})
    external_ip = info.get("NewExternalIPAddress", "")
    client = _local_ip_towards(urlsplit(location).hostname or "")
    lease = lifetime
    # Our own port number if it's free on the router, else a random one.
    for external_port in (internal_port, *(20000 + secrets.randbelow(40000) for _ in range(2))):
        ok, code = await _add_port_mapping(
            control, service, external_port, internal_port, client, lease
        )
        if not ok and code == "725" and lease:  # OnlyPermanentLeasesSupported
            lease = 0
            ok, code = await _add_port_mapping(
                control, service, external_port, internal_port, client, lease
            )
        if ok:

            async def release(port: int = external_port) -> None:
                await _soap(control, service, "DeletePortMapping", {
                    "NewRemoteHost": "", "NewExternalPort": port, "NewProtocol": "UDP",
                })

            return Mapping("UPnP", (external_ip, external_port), internal_port, lease,
                           time.monotonic(), release)
        if code != "718":  # anything but "that port is taken": this router won't
            return None
    return None


async def upnp_map(internal_port: int, lifetime: int = LEASE) -> Mapping | None:
    for location in await _ssdp_locations():
        try:
            mapping = await _upnp_map_at(location, internal_port, lifetime)
        except Exception as exc:  # noqa: BLE001 - HTTP/XML trouble: a router that doesn't play
            log.debug("UPnP at %s failed: %s", location, exc)
            continue
        if mapping:
            return mapping
    return None


# ── Keeping one mapping ──


class PortMapper:
    """Keeps one UDP port mapping on the home router while we're on a LAN."""

    def __init__(self) -> None:
        self.mapping: Mapping | None = None
        self.method = ""  # how the current mapping was made, for the connection report
        self._lan: tuple[str, ...] = ()
        self._failed_at = -RETRY_AFTER
        self._lock = asyncio.Lock()

    @property
    def external(self) -> tuple[str, int] | None:
        mapping = self.mapping
        if mapping and _public(mapping.external[0]):
            return mapping.external
        return None

    async def ensure(self, internal_port: int) -> tuple[str, int] | None:
        """A public address that forwards to ``internal_port``, if the router
        gives one. Cheap when nothing changed (call it often)."""
        async with self._lock:
            lan = tuple(home_lan_ipv4())
            now = time.monotonic()
            current = self.mapping
            if lan != self._lan:  # another network: forget the old router
                self._lan, self._failed_at = lan, -RETRY_AFTER
                await self._drop()
                current = None
            if not lan or not internal_port:
                return None
            if current and current.internal_port == internal_port and not current.due(now):
                return self.external
            if current is None and now - self._failed_at < RETRY_AFTER:
                return None
            await self._drop()
            self.mapping = await self._create(internal_port, lan)
            if self.mapping is None:
                self._failed_at = now
                return None
            self.method = self.mapping.method
            log.info("Router port mapping (%s): %s:%d -> UDP %d", self.method,
                     *self.mapping.external, internal_port)
            return self.external

    async def _create(self, internal_port: int, lan: tuple[str, ...]) -> Mapping | None:
        gateways = []
        for ip in lan:
            gateway = str(ipaddress.ip_network(f"{ip}/24", strict=False).network_address + 1)
            if gateway != ip and gateway not in gateways:
                gateways.append(gateway)
        for gateway in gateways:  # (most home routers sit at .1)
            with contextlib.suppress(OSError):
                mapping = await natpmp_map(gateway, internal_port)
                if mapping:
                    return mapping
        with contextlib.suppress(OSError):
            return await upnp_map(internal_port)
        return None

    async def _drop(self) -> None:
        mapping, self.mapping = self.mapping, None
        if mapping:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(mapping.release(), HTTP_TIMEOUT)

    async def close(self) -> None:
        async with self._lock:
            await self._drop()
            self.method = ""
