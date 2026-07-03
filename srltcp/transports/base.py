"""Abstract transport interface."""

from __future__ import annotations

import asyncio
import contextlib
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from srltcp.core.protocol.framing import FrameReader, FrameWriter


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

    @staticmethod
    def encode_frame(payload: bytes) -> bytes:
        return FrameWriter.write(payload)

    @staticmethod
    def decode_frames(buffer: bytes, reader: FrameReader) -> list[bytes]:
        return reader.feed(buffer)


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
        self._send_lock = asyncio.Lock()

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
                data = await self.reader.read(65536)
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
        frame = FrameWriter.write(payload)
        async with self._send_lock:
            self.writer.write(frame)
            await self.writer.drain()

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