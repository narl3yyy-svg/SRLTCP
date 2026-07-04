"""TCP/IP transport with listener and outbound dial."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from srltcp.core.messaging.constants import DISCOVERY_PORT
from srltcp.transports.base import Connection, Transport, TransportEvent, TransportPeer
from srltcp.utils.logging import get_logger
from srltcp.utils.network import broadcast_targets
from srltcp.utils.ports import bind_udp_port, start_tcp_server

log = get_logger(__name__)


class TCPTransport(Transport):
    name = "tcp"

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 7825,
        *,
        discovery_port: int = 7826,
    ) -> None:
        super().__init__()
        self.host = host
        self.port = port
        self.discovery_port = discovery_port
        self._server: asyncio.AbstractServer | None = None
        self._discovery_protocol: _DiscoveryProtocol | None = None
        self._discovery_transport: asyncio.DatagramTransport | None = None
        self._connections: dict[str, Connection] = {}
        self._peer_by_writer_id: dict[int, str] = {}
        self._announce_payload: bytes = b""
        self._running = False

    def set_announce_payload(self, payload: bytes) -> None:
        self._announce_payload = payload

    async def start(self) -> None:
        if self._running:
            return
        self._running = True

        async def _handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            peer_addr = writer.get_extra_info("peername")
            address = f"{peer_addr[0]}:{peer_addr[1]}" if peer_addr else "unknown"
            peer_id = str(uuid.uuid4())
            peer = TransportPeer(peer_id=peer_id, address=address, transport="tcp")
            conn = Connection(peer, reader, writer)
            conn.set_frame_handler(self._handle_frame)
            conn.set_close_handler(self._on_connection_closed)
            self._connections[peer_id] = conn
            self._peer_by_writer_id[id(writer)] = peer_id
            await conn.start_reading()
            await self._emit_event(TransportEvent(kind="connected", peer=peer))
            log.info("TCP peer connected: %s (%s)", peer_id[:8], address)

        self._server, self.port = await start_tcp_server(_handle, self.host, self.port)
        log.info("TCP transport listening on %s:%d", self.host, self.port)

        loop = asyncio.get_running_loop()
        self._discovery_protocol = _DiscoveryProtocol(self)
        self._discovery_transport, self.discovery_port = await bind_udp_port(
            loop,
            lambda: self._discovery_protocol,
            "0.0.0.0",
            self.discovery_port,
        )
        log.info("UDP discovery on port %d", self.discovery_port)

    async def stop(self) -> None:
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
        await self._emit_event(TransportEvent(kind="disconnected", peer=peer))

    async def disconnect(self, peer_id: str) -> None:
        conn = self._connections.pop(peer_id, None)
        if conn:
            await conn.close()

    async def connect(self, host: str, port: int) -> str:
        reader, writer = await asyncio.open_connection(host, port)
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

    async def send(self, peer_id: str, payload: bytes) -> None:
        conn = self._connections.get(peer_id)
        if not conn:
            raise KeyError(f"unknown peer: {peer_id}")
        await conn.send(payload)

    async def broadcast(self, payload: bytes) -> None:
        for conn in self._connections.values():
            await conn.send(payload)

    def peers(self) -> list[TransportPeer]:
        return [c.peer for c in self._connections.values()]

    def has_peer(self, peer_id: str) -> bool:
        return peer_id in self._connections

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
                self._discovery_transport.sendto(payload, (target, port))
        return True


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, transport: TCPTransport) -> None:
        self._tcp = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        peer = TransportPeer(
            peer_id=f"discovered-{addr[0]}:{addr[1]}",
            address=f"{addr[0]}:{addr[1]}",
            transport="tcp",
            metadata={"discovery": True, "payload": data},
        )
        asyncio.create_task(
            self._tcp._emit_event(TransportEvent(kind="discovered", peer=peer, data=data))
        )