"""USB Serial transport using pyserial + asyncio."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

from srltcp.core.protocol.framing import FrameReader, FrameWriter
from srltcp.transports.base import Transport, TransportEvent, TransportPeer
from srltcp.utils.logging import get_logger
from srltcp.utils.platform import default_serial_port

log = get_logger(__name__)


class SerialTransport(Transport):
    name = "serial"

    def __init__(
        self,
        port: str | None = None,
        baudrate: int = 115200,
        *,
        timeout: float = 0.1,
    ) -> None:
        super().__init__()
        self.port = port or default_serial_port()
        self.baudrate = baudrate
        self.timeout = timeout
        self._serial: Any = None
        self._peer: TransportPeer | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._frame_reader = FrameReader()
        self._running = False
        self._write_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._running:
            return
        try:
            import serial
        except ImportError as exc:
            raise RuntimeError("pyserial is required for serial transport") from exc

        self._serial = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            timeout=self.timeout,
        )
        self._running = True
        peer_id = str(uuid.uuid4())
        self._peer = TransportPeer(
            peer_id=peer_id,
            address=self.port,
            transport="serial",
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        await self._emit_event(TransportEvent(kind="connected", peer=self._peer))
        log.info("Serial transport open on %s @ %d baud", self.port, self.baudrate)

    async def stop(self) -> None:
        self._running = False
        if self._reader_task:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        if self._serial and self._serial.is_open:
            self._serial.close()
        if self._peer:
            await self._emit_event(TransportEvent(kind="disconnected", peer=self._peer))
        self._peer = None

    async def _read_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while self._running and self._serial and self._serial.is_open:
            try:
                data = await loop.run_in_executor(None, self._serial.read, 4096)
                if not data:
                    await asyncio.sleep(0.01)
                    continue
                for frame in self._frame_reader.feed(data):
                    if self._peer:
                        await self._emit_frame(self._peer, frame)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._emit_event(
                    TransportEvent(kind="error", error=str(exc), peer=self._peer)
                )
                await asyncio.sleep(0.5)

    async def send(self, peer_id: str, payload: bytes) -> None:
        if not self._serial or not self._serial.is_open:
            raise RuntimeError("serial port not open")
        frame = FrameWriter.write(payload)
        async with self._write_lock:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._serial.write, frame)
            await loop.run_in_executor(None, self._serial.flush)

    async def broadcast(self, payload: bytes) -> None:
        if self._peer:
            await self.send(self._peer.peer_id, payload)

    def peers(self) -> list[TransportPeer]:
        return [self._peer] if self._peer else []

    def update_peer_metadata(self, metadata: dict[str, Any]) -> None:
        if self._peer:
            self._peer.metadata.update(metadata)