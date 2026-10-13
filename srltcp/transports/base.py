"""Abstract transport interface."""

from __future__ import annotations

import asyncio
import contextlib
import socket
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from srltcp.core.protocol.framing import FrameReader, FrameWriter

# Stream tuning. Reads take what the socket has, up to READ_SIZE, so a 1 MiB
# file chunk arrives in a few reads instead of sixteen. The write buffer may
# hold WRITE_HIGH bytes before a sender has to wait: enough to keep a fast
# link busy while the next chunk is prepared, small enough that a chat
# message queued behind file data still goes out promptly.
STREAM_LIMIT = 1024 * 1024
READ_SIZE = 1024 * 1024
WRITE_HIGH = 2 * 1024 * 1024


@dataclass
class TransportPeer:
    peer_id: str
    address: str
    transport: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TransportEvent:
    kind: str  # connected | disconnected | frame | error
    peer: TransportPeer | None = None
    data: bytes | None = None
    error: str | None = None


FrameHandler = Callable[[TransportPeer, bytes], Awaitable[None]]
EventHandler = Callable[[TransportEvent], Awaitable[None]]


class Transport(ABC):
    """Async transport with framed reads/writes."""

    name: str = "base"

    def __init__(self) -> None:
        self._frame_handlers: list[FrameHandler] = []
        self._event_handlers: list[EventHandler] = []
        self._running = False

    def on_frame(self, handler: FrameHandler) -> None:
        self._frame_handlers.append(handler)

    def on_event(self, handler: EventHandler) -> None:
        self._event_handlers.append(handler)

    async def _emit_event(self, event: TransportEvent) -> None:
        for handler in self._event_handlers:
            await handler(event)

    async def _emit_frame(self, peer: TransportPeer, payload: bytes) -> None:
        for handler in self._frame_handlers:
            await handler(peer, payload)

    @abstractmethod
    async def start(self) -> None:
        ...

    @abstractmethod
    async def stop(self) -> None:
        ...

    @abstractmethod
    async def send(self, peer_id: str, payload: bytes) -> None:
        ...

    @abstractmethod
    async def broadcast(self, payload: bytes) -> None:
        ...

    @abstractmethod
    def peers(self) -> list[TransportPeer]:
        ...

class Connection:
    """Helper for a single peer connection with framed I/O."""

    def __init__(
        self,
        peer: TransportPeer,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self.peer = peer
        self.reader = reader
        self.writer = writer
        self._frame_reader = FrameReader()
        self._read_task: asyncio.Task[None] | None = None
        self._on_frame: FrameHandler | None = None
        self._on_close: Callable[[TransportPeer], Awaitable[None]] | None = None
        self._closed = False
        self._enable_keepalive(writer)
        with contextlib.suppress(AttributeError, RuntimeError):
            writer.transport.set_write_buffer_limits(high=WRITE_HIGH)

    @staticmethod
    def _enable_keepalive(writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        if not sock:
            return
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            if hasattr(socket, "TCP_KEEPIDLE"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
            if hasattr(socket, "TCP_KEEPINTVL"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
            if hasattr(socket, "TCP_KEEPCNT"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5)
        except OSError:
            pass

    def set_frame_handler(self, handler: FrameHandler) -> None:
        self._on_frame = handler

    def set_close_handler(
        self, handler: Callable[[TransportPeer], Awaitable[None]] | None
    ) -> None:
        self._on_close = handler

    async def start_reading(self) -> None:
        self._read_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            while not self._closed:
                data = await self.reader.read(READ_SIZE)
                if not data:
                    break
                for frame in self._frame_reader.feed(data):
                    if self._on_frame:
                        await self._on_frame(self.peer, frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            await self.close()

    async def send(self, payload: bytes) -> None:
        await self.send_framed(FrameWriter.write(payload))

    async def send_framed(self, frame: bytes) -> None:
        """Queue one complete frame. write() appends it whole, so frames from
        concurrent senders never interleave; only the wait for buffer space
        is shared (no lock: a chat message must not wait behind a file
        sender's drain)."""
        if self._closed:
            raise ConnectionError("connection closed")
        self.writer.write(frame)
        await self.writer.drain()

    def buffered(self) -> int:
        """Bytes queued in user space, not yet handed to the kernel."""
        try:
            return int(self.writer.transport.get_write_buffer_size())
        except (AttributeError, RuntimeError):
            return 0

    def tune_for_lane(self, high: int) -> None:
        """Extra Tor connection carrying file data: keep little in flight on
        it, so a slow circuit holds back as few bytes as possible."""
        with contextlib.suppress(AttributeError, RuntimeError):
            self.writer.transport.set_write_buffer_limits(high=high)
        sock = self.writer.get_extra_info("socket")
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, high)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._read_task
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass
        if self._on_close:
            await self._on_close(self.peer)