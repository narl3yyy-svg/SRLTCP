"""TCP/IP transport with listener and outbound dial."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import uuid
from typing import Any

from srltcp.core.messaging.constants import DISCOVERY_PORT
from srltcp.transports.base import (
    STREAM_LIMIT,
    Connection,
    Transport,
    TransportEvent,
    TransportPeer,
)
from srltcp.utils.logging import get_logger
from srltcp.utils.network import broadcast_targets
from srltcp.utils.ports import bind_udp_port, start_tcp_server

log = get_logger(__name__)

def bound_socket(family: int, local_port: int) -> socket.socket:
    """A TCP socket bound to a fixed local port that other outgoing sockets may
    share (SO_REUSEADDR, never listening): the relay connection and the hole
    punching dials all leave from the same port, so the NAT maps them the same
    way and the address the relay reports is the one contacts must dial."""
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.bind(("::" if family == socket.AF_INET6 else "0.0.0.0", local_port))
        sock.setblocking(False)
    except OSError:
        sock.close()
        raise
    return sock


# Tor connections keep little in flight: Tor reads them only as fast as the
# circuit moves, and anything queued there delays chat behind file data.
TOR_BUFFER = 256 * 1024

# Hard cap on open TCP connections (a phone has a handful of contacts; a hub
# a few hundred members).
MAX_CONNECTIONS = 512


class TCPTransport(Transport):
    name = "tcp"

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 7825,
        *,
        discovery_port: int = 7826,
        strict_ports: bool = False,
    ) -> None:
        super().__init__()
        self.host = host
        self.port = port
        self.discovery_port = discovery_port
        self.strict_ports = strict_ports
        self._server: asyncio.AbstractServer | None = None
        self._discovery_protocol: _DiscoveryProtocol | None = None
        self._discovery_transport: asyncio.DatagramTransport | None = None
        self._connections: dict[str, Connection] = {}
        self._announce_payload: bytes = b""
        self._running = False
        # Connections that came in through our onion service or were dialed
        # over Tor: only trusted contacts may use them (see backend).
        self._tor_peers: set[str] = set()
        self._onion_server: asyncio.Server | None = None
        self.onion_port = 0

    def set_announce_payload(self, payload: bytes) -> None:
        self._announce_payload = payload

    async def start(self) -> None:
        if self._running:
            return
        self._running = True

        async def _handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            await self._accept(reader, writer, via_tor=False)

        self._server, self.port = await start_tcp_server(
            _handle, self.host, self.port, strict=self.strict_ports, limit=STREAM_LIMIT
        )
        log.info("TCP transport listening on %s:%d", self.host, self.port)

        loop = asyncio.get_running_loop()
        self._discovery_protocol = _DiscoveryProtocol(self)
        requested_discovery = self.discovery_port
        self._discovery_transport, self.discovery_port = await bind_udp_port(
            loop,
            lambda: self._discovery_protocol,
            "0.0.0.0",
            requested_discovery,
            strict=self.strict_ports,
        )
        log.info("UDP discovery on port %d", self.discovery_port)

    async def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, via_tor: bool
    ) -> None:
        if len(self._connections) >= MAX_CONNECTIONS:
            log.warning("Too many TCP connections; refusing a new one")
            writer.close()
            return
        peer_addr = writer.get_extra_info("peername")
        address = f"{peer_addr[0]}:{peer_addr[1]}" if peer_addr else "unknown"
        if via_tor:
            address = "tor"  # Tor hides the caller's address (by design)
        peer_id = str(uuid.uuid4())
        if via_tor:
            self._tor_peers.add(peer_id)
        peer = TransportPeer(peer_id=peer_id, address=address, transport="tcp")
        conn = Connection(peer, reader, writer)
        conn.set_frame_handler(self._handle_frame)
        conn.set_close_handler(self._on_connection_closed)
        if via_tor:
            conn.tune_for_lane(TOR_BUFFER)
        self._connections[peer_id] = conn
        await conn.start_reading()
        await self._emit_event(TransportEvent(kind="connected", peer=peer))
        log.info("TCP peer connected: %s (%s)", peer_id[:8], address)

    async def start_onion_listener(self, port: int = 0) -> int:
        """Listen on 127.0.0.1 for connections Tor forwards from our onion
        service. Separate from the LAN listener so we know they came via Tor."""
        if self._onion_server:
            return self.onion_port

        async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await self._accept(reader, writer, via_tor=True)

        self._onion_server = await asyncio.start_server(
            _handle, "127.0.0.1", port, limit=STREAM_LIMIT
        )
        self.onion_port = int(self._onion_server.sockets[0].getsockname()[1])
        return self.onion_port

    async def stop_onion_listener(self) -> None:
        if self._onion_server:
            self._onion_server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._onion_server.wait_closed(), timeout=3.0)
            self._onion_server = None

    def is_tor(self, peer_id: str) -> bool:
        return peer_id in self._tor_peers

    async def connect_socks(
        self,
        socks_port: int,
        host: str,
        port: int,
        *,
        timeout: float = 60.0,
        isolation: str = "",
    ) -> str:
        """Dial ``host`` (an .onion address) through Tor's SOCKS5 port.
        ``isolation`` becomes the SOCKS username, so each contact gets its own
        Tor circuit (IsolateSOCKSAuth)."""

        async def _dial() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", socks_port, limit=STREAM_LIMIT
            )
            try:
                user = (isolation or "srltcp").encode()[:255]
                writer.write(b"\x05\x01\x02")  # SOCKS5, one method: user/pass
                await writer.drain()
                if await reader.readexactly(2) != b"\x05\x02":
                    raise ConnectionError("Tor SOCKS refused authentication method")
                writer.write(b"\x01" + bytes([len(user)]) + user + b"\x01x")
                await writer.drain()
                if (await reader.readexactly(2))[1] != 0:
                    raise ConnectionError("Tor SOCKS authentication failed")
                name = host.encode("ascii")
                writer.write(
                    b"\x05\x01\x00\x03" + bytes([len(name)]) + name + port.to_bytes(2, "big")
                )
                await writer.drain()
                head = await reader.readexactly(4)
                if head[1] != 0:
                    raise ConnectionError(f"Tor could not reach the contact (SOCKS code {head[1]})")
                atyp = head[3]
                skip = {1: 4, 4: 16}.get(atyp)
                if skip is None:
                    skip = (await reader.readexactly(1))[0]
                await reader.readexactly(skip + 2)
            except BaseException:
                writer.close()
                raise
            return reader, writer

        reader, writer = await asyncio.wait_for(_dial(), timeout=timeout)
        peer_id = await self._adopt(reader, writer, f"{host}:{port}")
        self._tor_peers.add(peer_id)
        self.tune_for_lane(peer_id, TOR_BUFFER)
        return peer_id

    async def stop(self) -> None:
        await self.stop_onion_listener()
        self._running = False
        for conn in list(self._connections.values()):
            await conn.close()
        self._connections.clear()
        if self._discovery_transport:
            self._discovery_transport.close()
            self._discovery_transport = None
        if self._server:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=3.0)
            except TimeoutError:
                log.warning("TCP server close timed out")
            self._server = None

    async def _handle_frame(self, peer: TransportPeer, payload: bytes) -> None:
        await self._emit_frame(peer, payload)

    async def _on_connection_closed(self, peer: TransportPeer) -> None:
        self._connections.pop(peer.peer_id, None)
        self._tor_peers.discard(peer.peer_id)
        await self._emit_event(TransportEvent(kind="disconnected", peer=peer))

    async def disconnect(self, peer_id: str) -> None:
        conn = self._connections.pop(peer_id, None)
        if conn:
            await conn.close()

    async def connect(self, host: str, port: int, *, timeout: float = 8.0) -> str:
        # Without a timeout an unreachable VPN/LAN host blocks for ~2 minutes.
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, limit=STREAM_LIMIT), timeout=timeout
        )
        peer_id = str(uuid.uuid4())
        address = f"{host}:{port}"
        peer = TransportPeer(peer_id=peer_id, address=address, transport="tcp")
        conn = Connection(peer, reader, writer)
        conn.set_frame_handler(self._handle_frame)
        conn.set_close_handler(self._on_connection_closed)
        self._connections[peer_id] = conn
        await conn.start_reading()
        await self._emit_event(TransportEvent(kind="connected", peer=peer))
        return peer_id

    async def connect_from(
        self, host: str, port: int, local_port: int, *, timeout: float = 2.0
    ) -> str:
        """Dial from a fixed local port (hole punching: both sides dial each
        other at the same time from their punch port; the crossing SYNs open
        the path through NATs and firewalls — TCP simultaneous open)."""
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        sock = bound_socket(family, local_port)
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(loop.sock_connect(sock, (host, port)), timeout=timeout)
            reader, writer = await asyncio.open_connection(sock=sock, limit=STREAM_LIMIT)
        except BaseException:
            sock.close()
            raise
        return await self._adopt(reader, writer, f"{host}:{port}")

    @staticmethod
    async def prime_from(host: str, port: int, local_port: int, ttl: int = 2) -> None:
        """Send one short-lived SYN from the punch port that expires a couple of
        hops out: it opens our own router's NAT/firewall mapping towards the
        peer without reaching the peer's router, which could otherwise answer
        with a reset and spoil the mapping before the peer dials."""
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        try:
            sock = bound_socket(family, local_port)
        except OSError:
            return
        try:
            if family == socket.AF_INET6:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_UNICAST_HOPS, ttl)
            else:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, ttl)
            with contextlib.suppress(BlockingIOError, InterruptedError, OSError):
                sock.connect((host, port))
            await asyncio.sleep(0.03)
        finally:
            sock.close()

    async def _adopt(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, address: str
    ) -> str:
        peer_id = str(uuid.uuid4())
        peer = TransportPeer(peer_id=peer_id, address=address, transport="tcp")
        conn = Connection(peer, reader, writer)
        conn.set_frame_handler(self._handle_frame)
        conn.set_close_handler(self._on_connection_closed)
        self._connections[peer_id] = conn
        await conn.start_reading()
        await self._emit_event(TransportEvent(kind="connected", peer=peer))
        return peer_id

    async def send(self, peer_id: str, payload: bytes) -> None:
        conn = self._connections.get(peer_id)
        if not conn:
            raise KeyError(f"unknown peer: {peer_id}")
        await conn.send(payload)

    async def send_framed(self, peer_id: str, frame: bytes) -> None:
        """Send a frame already built with ``pack_frame`` (file data framed
        in a worker thread)."""
        conn = self._connections.get(peer_id)
        if not conn:
            raise KeyError(f"unknown peer: {peer_id}")
        await conn.send_framed(frame)

    def buffered(self, peer_id: str) -> int:
        conn = self._connections.get(peer_id)
        return conn.buffered() if conn else 0

    def tune_for_lane(self, peer_id: str, high: int) -> None:
        conn = self._connections.get(peer_id)
        if conn:
            conn.tune_for_lane(high)

    async def broadcast(self, payload: bytes) -> None:
        for conn in self._connections.values():
            await conn.send(payload)

    def peers(self) -> list[TransportPeer]:
        return [c.peer for c in self._connections.values()]

    def has_peer(self, peer_id: str) -> bool:
        return peer_id in self._connections

    def local_ip(self, peer_id: str) -> str:
        """Our address on this connection (which interface it uses)."""
        conn = self._connections.get(peer_id)
        if not conn:
            return ""
        sockname = conn.writer.get_extra_info("sockname")
        return str(sockname[0]) if sockname else ""

    def update_peer_id(self, old_id: str, new_id: str, metadata: dict[str, Any]) -> None:
        conn = self._connections.pop(old_id, None)
        if not conn:
            return
        conn.peer.peer_id = new_id
        conn.peer.metadata.update(metadata)
        self._connections[new_id] = conn

    async def broadcast_discovery(self, payload: bytes) -> bool:
        if not self._discovery_transport:
            return False
        import socket as sock_mod

        sock = self._discovery_transport.get_extra_info("socket")
        if sock:
            sock.setsockopt(sock_mod.SOL_SOCKET, sock_mod.SO_BROADCAST, 1)
            sock.setsockopt(sock_mod.SOL_SOCKET, sock_mod.SO_REUSEADDR, 1)
        dest_ports = {DISCOVERY_PORT, self.discovery_port}
        for target in broadcast_targets():
            for port in sorted(dest_ports):
                try:
                    self._discovery_transport.sendto(payload, (target, port))
                except OSError as exc:
                    # e.g. ENETUNREACH on an interface that just went away
                    log.debug("Broadcast to %s:%d failed: %s", target, port, exc)
        return True


    async def unicast_discovery(
        self, payload: bytes, hosts: list[str], ports: set[int] | None = None
    ) -> int:
        """Send a discovery packet to specific addresses (VPNs carry no broadcast)."""
        if not self._discovery_transport or not hosts:
            return 0
        dest_ports = sorted(ports or {DISCOVERY_PORT, self.discovery_port})
        sent = 0
        for i, host in enumerate(hosts):
            for port in dest_ports:
                try:
                    self._discovery_transport.sendto(payload, (host, port))
                    sent += 1
                except OSError:
                    continue
            if i % 64 == 63:
                await asyncio.sleep(0.02)  # pace large sweeps so the socket buffer keeps up
        return sent


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, transport: TCPTransport) -> None:
        self._tcp = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) > 8192:  # announces are ~400 bytes; ignore junk
            return
        peer = TransportPeer(
            peer_id=f"discovered-{addr[0]}:{addr[1]}",
            address=f"{addr[0]}:{addr[1]}",
            transport="tcp",
            metadata={"discovery": True, "payload": data},
        )
        asyncio.create_task(
            self._tcp._emit_event(TransportEvent(kind="discovered", peer=peer, data=data))
        )