"""Home router port mapping (UPnP IGD, NAT-PMP) against fake routers."""

from __future__ import annotations

import asyncio
import struct
import time
from typing import Any

import pytest
from aiohttp import web

import srltcp.core  # noqa: F401  (import order: core before transports)
from srltcp.transports import portmap

# ── NAT-PMP ──


class FakeNatPmp(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.mapped: dict[int, int] = {}  # internal port -> lifetime
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        assert self.transport is not None
        if data == b"\x00\x00":
            self.transport.sendto(struct.pack(">BBHI4s", 0, 128, 0, 7, bytes([203, 0, 113, 9])),
                                  addr)
        elif data[1] == 1:
            _v, _op, _r, internal, _ext, lifetime = struct.unpack(">BBHHHI", data)
            if lifetime:
                self.mapped[internal] = lifetime
            else:
                self.mapped.pop(internal, None)
            self.transport.sendto(struct.pack(">BBHIHHI", 0, 129, 0, 7, internal,
                                              internal if lifetime else 0, lifetime), addr)


async def test_natpmp_maps_and_releases(monkeypatch: Any) -> None:
    loop = asyncio.get_running_loop()
    transport, router = await loop.create_datagram_endpoint(
        FakeNatPmp, local_addr=("127.0.0.1", 0)
    )
    monkeypatch.setattr(portmap, "NATPMP_PORT", transport.get_extra_info("sockname")[1])
    try:
        mapping = await portmap.natpmp_map("127.0.0.1", 7828)
        assert mapping is not None
        assert mapping.method == "NAT-PMP"
        assert mapping.external == ("203.0.113.9", 7828)
        assert router.mapped == {7828: portmap.LEASE}
        await mapping.release()
        assert router.mapped == {}
    finally:
        transport.close()


async def test_natpmp_without_a_router_gives_up_quickly(monkeypatch: Any) -> None:
    loop = asyncio.get_running_loop()
    silent, _ = await loop.create_datagram_endpoint(
        asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0)
    )
    monkeypatch.setattr(portmap, "NATPMP_PORT", silent.get_extra_info("sockname")[1])
    try:
        started = time.monotonic()
        assert await portmap.natpmp_map("127.0.0.1", 7828) is None
        assert time.monotonic() - started < 3
    finally:
        silent.close()


# ── UPnP IGD ──

DESCRIPTION = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0"><device><deviceList><device><deviceList><device>
<serviceList><service>
<serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>
<controlURL>/ctl/IPConn</controlURL>
</service></serviceList></device></deviceList></device></deviceList></device></root>"""


SERVICE = "urn:schemas-upnp-org:service:WANIPConnection:1"
ENVELOPE = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/'
            'envelope/"><s:Body>')


class FakeIgd:
    def __init__(self, taken: set[int] | None = None, permanent_only: bool = False) -> None:
        self.taken = taken or set()
        self.permanent_only = permanent_only
        self.mappings: dict[int, dict[str, str]] = {}

    @staticmethod
    def _field(body: str, name: str) -> str:
        return body.split(f"<{name}>", 1)[1].split(f"</{name}>", 1)[0]

    def _reply(self, action: str, inner: str = "", status: int = 200) -> web.Response:
        if status != 200:
            body = (f"{ENVELOPE}<s:Fault><detail><UPnPError xmlns=\"urn:schemas-upnp-org:"
                    f"control-1-0\"><errorCode>{inner}</errorCode></UPnPError></detail>"
                    "</s:Fault></s:Body></s:Envelope>")
        else:
            body = (f"{ENVELOPE}<u:{action}Response xmlns:u=\"{SERVICE}\">{inner}"
                    f"</u:{action}Response></s:Body></s:Envelope>")
        return web.Response(status=status, text=body, content_type="text/xml")

    async def control(self, request: web.Request) -> web.Response:
        action = request.headers["SOAPAction"].strip('"').split("#", 1)[1]
        body = await request.text()
        if action == "GetExternalIPAddress":
            return self._reply(action, "<NewExternalIPAddress>198.51.100.7</NewExternalIPAddress>")
        port = int(self._field(body, "NewExternalPort"))
        if action == "AddPortMapping":
            if port in self.taken:
                return self._reply(action, "718", 500)
            lease = self._field(body, "NewLeaseDuration")
            if self.permanent_only and lease != "0":
                return self._reply(action, "725", 500)
            self.mappings[port] = {
                "internal": self._field(body, "NewInternalPort"), "lease": lease,
                "proto": self._field(body, "NewProtocol"),
            }
            return self._reply(action)
        if action == "DeletePortMapping":
            self.mappings.pop(port, None)
            return self._reply(action)
        return self._reply(action, "401", 500)


async def _serve(igd: FakeIgd) -> tuple[web.AppRunner, str]:
    async def description(_request: web.Request) -> web.Response:
        return web.Response(text=DESCRIPTION, content_type="text/xml")

    app = web.Application()
    app.router.add_get("/desc.xml", description)
    app.router.add_post("/ctl/IPConn", igd.control)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, f"http://127.0.0.1:{port}/desc.xml"


@pytest.mark.parametrize(("taken", "permanent_only"), [(set(), False), ({7828}, False),
                                                       (set(), True)])
async def test_upnp_maps_and_releases(
    monkeypatch: Any, taken: set[int], permanent_only: bool
) -> None:
    igd = FakeIgd(taken, permanent_only)
    runner, location = await _serve(igd)

    async def locations() -> list[str]:
        return [location]

    monkeypatch.setattr(portmap, "_ssdp_locations", locations)
    try:
        mapping = await portmap.upnp_map(7828)
        assert mapping is not None
        assert mapping.method == "UPnP"
        assert mapping.external[0] == "198.51.100.7"
        assert (mapping.external[1] == 7828) == (not taken)  # a free port when ours is taken
        assert igd.mappings == {mapping.external[1]: {
            "internal": "7828", "lease": "0" if permanent_only else str(portmap.LEASE),
            "proto": "UDP",
        }}
        await mapping.release()
        assert igd.mappings == {}
    finally:
        await runner.cleanup()


async def test_ssdp_only_trusts_the_router_that_answered(monkeypatch: Any) -> None:
    """An answer pointing somewhere else (another host, the internet) is ignored."""
    loop = asyncio.get_running_loop()
    real = loop.create_datagram_endpoint

    async def fake_endpoint(factory: Any, **kwargs: Any) -> Any:
        transport, proto = await real(factory, **kwargs)
        proto.datagram_received(b"HTTP/1.1 200 OK\r\nLOCATION: http://8.8.8.8/x.xml\r\n\r\n",
                                ("192.168.1.1", 1900))
        proto.datagram_received(b"HTTP/1.1 200 OK\r\nLOCATION: http://192.168.1.1:5000/d.xml"
                                b"\r\n\r\n", ("192.168.1.1", 1900))
        return transport, proto

    monkeypatch.setattr(loop, "create_datagram_endpoint", fake_endpoint)
    monkeypatch.setattr(portmap, "SSDP_WAIT", 0.2)
    monkeypatch.setattr(portmap, "_on_our_lan", lambda host: host == "192.168.1.1")
    assert await portmap._ssdp_locations() == ["http://192.168.1.1:5000/d.xml"]


# ── Keeping the mapping ──


PUBLIC = "81.2.69.160"


class Router:
    def __init__(self) -> None:
        self.created: list[int] = []
        self.released: list[int] = []
        self.works = True

    async def create(self, internal_port: int, _lan: Any) -> portmap.Mapping | None:
        if not self.works:
            return None
        self.created.append(internal_port)

        async def release() -> None:
            self.released.append(internal_port)

        return portmap.Mapping("NAT-PMP", (PUBLIC, internal_port), internal_port,
                               portmap.LEASE, time.monotonic(), release)


@pytest.fixture
def router(monkeypatch: Any) -> Router:
    fake = Router()
    monkeypatch.setattr(portmap.PortMapper, "_create", fake.create)
    monkeypatch.setattr(portmap, "home_lan_ipv4", lambda: ["192.168.1.20"])
    return fake


async def test_mapper_keeps_renews_and_follows_the_port(router: Router) -> None:
    mapper = portmap.PortMapper()
    assert await mapper.ensure(7828) == (PUBLIC, 7828)
    assert await mapper.ensure(7828) == (PUBLIC, 7828)
    assert router.created == [7828]  # current: nothing asked
    assert mapper.mapping is not None
    mapper.mapping.obtained -= portmap.LEASE  # half the lease gone: renew
    await mapper.ensure(7828)
    assert router.created == [7828, 7828]
    await mapper.ensure(40001)  # new local port (after a rotation): move the mapping
    assert router.created[-1] == 40001 and router.released == [7828, 7828]
    await mapper.close()
    assert router.released[-1] == 40001 and mapper.mapping is None


async def test_mapper_waits_after_a_router_without_support(
    router: Router, monkeypatch: Any
) -> None:
    router.works = False
    mapper = portmap.PortMapper()
    assert await mapper.ensure(7828) is None
    router.works = True
    assert await mapper.ensure(7828) is None  # not asked again right away
    monkeypatch.setattr(portmap, "home_lan_ipv4", lambda: ["10.0.0.8"])  # another Wi-Fi
    assert await mapper.ensure(7828) == (PUBLIC, 7828)


async def test_mapper_does_nothing_on_mobile_data(router: Router, monkeypatch: Any) -> None:
    monkeypatch.setattr(portmap, "home_lan_ipv4", lambda: [])
    assert await portmap.PortMapper().ensure(7828) is None
    assert router.created == []


async def test_forwarded_address_is_offered_first(tmp_path: Any, router: Router) -> None:
    import socket

    from tests.test_e2e_nodes import make_node

    node = make_node(tmp_path, "home", tcp_port=0)
    await node.start()
    try:
        assert node.udp is not None
        await node._ensure_port_mapping()
        port = node.udp.ports[socket.AF_INET]
        node.udp.mapped[socket.AF_INET] = (PUBLIC, 61000)  # STUN's answer
        assert node.udp_candidates()[:2] == [(PUBLIC, port), (PUBLIC, 61000)]
        node.config.router_port_mapping = False
        await node._ensure_port_mapping()
        assert (PUBLIC, port) not in node.udp_candidates()
        assert router.released == [port]
    finally:
        await node.stop()
