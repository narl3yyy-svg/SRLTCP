"""High-performance chunked file transfer mixin."""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from typing import TYPE_CHECKING

import zstandard as zstd

from srltcp.core.messaging.constants import (
    CHUNK_SEND_DELAY,
    CHUNK_SIZE,
    COMPRESS_THRESHOLD,
    SERIAL_CHUNK_DELAY,
    SERIAL_CHUNK_SIZE,
    TRANSFER_COOLDOWN,
)
from srltcp.core.messaging.models import FileTransfer, TransferState
from srltcp.core.protocol.messages import (
    Flags,
    MessageType,
    build_header,
    decode_payload,
    encode_payload,
    pack_file_chunk,
    unpack_file_chunk,
)
from srltcp.utils.files import ensure_dir, fsync_file, safe_filename, sha256_file, write_file_chunk
from srltcp.utils.logging import get_logger
from srltcp.utils.platform import data_dir

if TYPE_CHECKING:
    from srltcp.core.messaging.backend import MessagingBackend

log = get_logger(__name__)


class TransferMixin:
    """Streaming, resumable, optionally compressed file transfers."""

    _transfers: dict[str, FileTransfer]
    _incoming_paths: dict[str, Path]
    _transfer_tasks: dict[str, asyncio.Task[None]]

    def _init_transfer(self: MessagingBackend) -> None:
        self._transfers = {}
        self._incoming_paths = {}
        self._transfer_tasks = {}
        if self.config.incoming_dir:
            self._transfer_dir = Path(self.config.incoming_dir)
        else:
            self._transfer_dir = data_dir() / "transfers"
        ensure_dir(self._transfer_dir)
        self._transfer_started: dict[str, float] = {}
        self._transfer_cooldown_until: dict[str, float] = {}

    def _mark_transfer_cooldown(self: MessagingBackend, *hash_ids: str) -> None:
        deadline = time.monotonic() + TRANSFER_COOLDOWN
        for hash_id in hash_ids:
            if hash_id:
                self._transfer_cooldown_until[hash_id] = deadline

    def in_transfer_cooldown(self: MessagingBackend, hash_id: str) -> bool:
        deadline = self._transfer_cooldown_until.get(hash_id)
        if not deadline:
            return False
        if time.monotonic() >= deadline:
            self._transfer_cooldown_until.pop(hash_id, None)
            return False
        return True

    def _maybe_compress(
        self: MessagingBackend, data: bytes, *, transport: str = "tcp"
    ) -> tuple[bytes, bool]:
        if transport == "serial" or len(data) < COMPRESS_THRESHOLD:
            return data, False
        cctx = zstd.ZstdCompressor(level=3)
        compressed = cctx.compress(data)
        if len(compressed) < len(data):
            return compressed, True
        return data, False

    def _chunk_params(self: MessagingBackend, transport: str) -> tuple[int, float]:
        if transport == "serial":
            return SERIAL_CHUNK_SIZE, SERIAL_CHUNK_DELAY
        return CHUNK_SIZE, CHUNK_SEND_DELAY

    def _maybe_decompress(self: MessagingBackend, data: bytes, compressed: bool) -> bytes:
        if not compressed:
            return data
        dctx = zstd.ZstdDecompressor()
        return dctx.decompress(data)

    async def offer_file(
        self: MessagingBackend,
        recipient_hash: str,
        path: Path,
        *,
        transport: str = "tcp",
    ) -> FileTransfer | None:
        link = self.get_link(recipient_hash)
        if not link or not link.handshake_complete:
            log.warning("No encrypted link to %s", recipient_hash[:8])
            return None

        file_hash = await sha256_file(path)
        transfer = FileTransfer.create(
            self._identity_for_transport(link.transport).hash_id,
            recipient_hash,
            path,
            link.transport,
            sha256=file_hash,
        )
        self._transfers[transfer.id] = transfer
        chunk_size, _ = self._chunk_params(link.transport)
        body = encode_payload(
            {
                "transfer_id": transfer.id,
                "filename": transfer.filename,
                "size": transfer.size,
                "sha256": transfer.sha256,
                "chunk_size": chunk_size,
            }
        )
        packet = await self._encrypt_for_link(link, MessageType.FILE_OFFER, body)
        await self._send_raw(link.transport_peer_id, link.transport, packet)
        transfer.state = TransferState.OFFERED
        return transfer

    async def _handle_file_offer(self: MessagingBackend, hash_id: str, body: bytes) -> None:
        data = decode_payload(body)
        transfer_id = data["transfer_id"]
        dest = self._transfer_dir / f"{transfer_id}_{safe_filename(data['filename'])}"
        identity_hash = ""
        link = self.get_link(hash_id)
        if link:
            identity_hash = self._identity_for_transport(link.transport).hash_id
        transfer = FileTransfer(
            id=transfer_id,
            sender_hash=hash_id,
            recipient_hash=identity_hash,
            filename=data["filename"],
            path=dest,
            size=int(data["size"]),
            sha256=data["sha256"],
            transport="tcp",
            state=TransferState.OFFERED,
        )
        if link:
            transfer.transport = link.transport
        self._transfers[transfer_id] = transfer
        self._incoming_paths[transfer_id] = dest

        if link:
            accept_body = encode_payload({"transfer_id": transfer_id, "offset": 0})
            packet = await self._encrypt_for_link(link, MessageType.FILE_ACCEPT, accept_body)
            await self._send_raw(link.transport_peer_id, link.transport, packet)
            transfer.state = TransferState.ACCEPTED
            transfer.recipient_hash = self._identity_for_transport(link.transport).hash_id
            if self._on_file_offer:
                await self._on_file_offer(transfer.to_dict())
            await self._append_file_message(
                sender_hash=hash_id,
                recipient_hash=transfer.recipient_hash,
                transport=link.transport,
                transfer=transfer.to_dict(),
                direction="in",
            )

    async def _handle_file_accept(self: MessagingBackend, hash_id: str, body: bytes) -> None:
        data = decode_payload(body)
        transfer_id = data["transfer_id"]
        offset = int(data.get("offset", 0))
        transfer = self._transfers.get(transfer_id)
        if not transfer:
            return
        transfer.state = TransferState.TRANSFERRING
        transfer.offset = offset
        self._transfer_started[transfer_id] = time.time()
        if self._on_transfer_progress:
            await self._on_transfer_progress(transfer.to_dict())
        task = asyncio.create_task(self._send_file_chunks(hash_id, transfer))
        self._transfer_tasks[transfer_id] = task

    async def _handle_file_resume(self: MessagingBackend, hash_id: str, body: bytes) -> None:
        await self._handle_file_accept(hash_id, body)

    async def _send_file_chunks(
        self: MessagingBackend, hash_id: str, transfer: FileTransfer
    ) -> None:
        link = self.get_link(hash_id)
        if not link:
            transfer.state = TransferState.FAILED
            return

        import aiofiles

        chunk_size, chunk_delay = self._chunk_params(transfer.transport)

        try:
            async with aiofiles.open(transfer.path, "rb") as f:
                await f.seek(transfer.offset)
                while transfer.offset < transfer.size:
                    if transfer.state == TransferState.CANCELLED:
                        return
                    link = self.get_link(hash_id)
                    if not link or not link.handshake_complete:
                        for _ in range(40):
                            await asyncio.sleep(0.25)
                            if transfer.state == TransferState.CANCELLED:
                                return
                            link = self.get_link(hash_id)
                            if link and link.handshake_complete:
                                break
                        if not link or not link.handshake_complete:
                            log.warning(
                                "Link lost during transfer %s at %d/%d",
                                transfer.id,
                                transfer.offset,
                                transfer.size,
                            )
                            transfer.state = TransferState.FAILED
                            return
                    chunk = await f.read(chunk_size)
                    if not chunk:
                        break
                    payload, compressed = self._maybe_compress(
                        chunk, transport=transfer.transport
                    )
                    raw = pack_file_chunk(transfer.id, transfer.offset, payload)
                    flags = Flags.ENCRYPTED | Flags.E2EE
                    if compressed:
                        flags |= Flags.COMPRESSED
                    encrypted = link.crypto.encrypt(raw)
                    packet = build_header(
                        MessageType.FILE_CHUNK,
                        flags=flags,
                        body=encrypted,
                    )
                    await self._send_raw(link.transport_peer_id, link.transport, packet)
                    transfer.offset += len(chunk)
                    started = self._transfer_started.get(transfer.id, time.time())
                    elapsed = max(time.time() - started, 0.001)
                    transfer.speed_mbps = (transfer.offset / elapsed) / (1024 * 1024)
                    if self._on_transfer_progress:
                        await self._on_transfer_progress(transfer.to_dict())
                    await self._update_file_message(transfer.to_dict())
                    if chunk_delay > 0:
                        await asyncio.sleep(chunk_delay)

            if transfer.state == TransferState.CANCELLED:
                return
            link = self.get_link(hash_id)
            if not link or not link.handshake_complete:
                transfer.state = TransferState.FAILED
                return
            complete_body = encode_payload(
                {"transfer_id": transfer.id, "sha256": transfer.sha256}
            )
            packet = await self._encrypt_for_link(link, MessageType.FILE_COMPLETE, complete_body)
            await self._send_raw(link.transport_peer_id, link.transport, packet)
            transfer.state = TransferState.COMPLETE
            log.info("Transfer complete: %s", transfer.filename)
            self._mark_transfer_cooldown(hash_id, transfer.recipient_hash)
            if self._on_transfer_complete:
                await self._on_transfer_complete(transfer.to_dict())
            await self._update_file_message(transfer.to_dict())
        except asyncio.CancelledError:
            transfer.state = TransferState.CANCELLED
            if self._on_transfer_progress:
                await self._on_transfer_progress(transfer.to_dict())
            raise
        except Exception as exc:
            log.warning("Transfer failed for %s: %s", transfer.filename, exc)
            transfer.state = TransferState.FAILED
            if self._on_transfer_progress:
                await self._on_transfer_progress(transfer.to_dict())
            await self._update_file_message(transfer.to_dict())
        finally:
            self._transfer_tasks.pop(transfer.id, None)

    async def _handle_file_chunk(
        self: MessagingBackend, hash_id: str, body: bytes, *, compressed: bool = False
    ) -> None:
        link = self.get_link(hash_id)
        if not link:
            return
        try:
            decrypted = link.crypto.decrypt(body)
            transfer_id, offset, data = unpack_file_chunk(decrypted)
        except Exception as exc:
            log.warning("File chunk decrypt failed from %s: %s", hash_id[:8], exc)
            return

        transfer = self._transfers.get(transfer_id)
        if not transfer:
            log.warning("Unknown transfer %s from %s", transfer_id, hash_id[:8])
            return

        try:
            data = self._maybe_decompress(data, compressed)
        except Exception as exc:
            log.warning("File chunk decompress failed for %s: %s", transfer_id, exc)
            return

        dest = self._incoming_paths.get(transfer_id, transfer.path)
        await write_file_chunk(dest, offset, data)
        transfer.offset = max(transfer.offset, offset + len(data))
        transfer.state = TransferState.TRANSFERRING
        started = self._transfer_started.setdefault(transfer_id, time.time())
        elapsed = max(time.time() - started, 0.001)
        transfer.speed_mbps = (transfer.offset / elapsed) / (1024 * 1024)
        if self._on_transfer_progress:
            await self._on_transfer_progress(transfer.to_dict())
        await self._update_file_message(transfer.to_dict())

    async def _handle_file_complete(self: MessagingBackend, hash_id: str, body: bytes) -> None:
        data = decode_payload(body)
        transfer_id = data["transfer_id"]
        transfer = self._transfers.get(transfer_id)
        if not transfer:
            return
        dest = self._incoming_paths.get(transfer_id, transfer.path)
        await fsync_file(dest)
        actual = await sha256_file(dest)
        if actual != data.get("sha256", transfer.sha256):
            transfer.state = TransferState.FAILED
            log.error("SHA256 mismatch for %s", transfer.filename)
            return
        transfer.state = TransferState.COMPLETE
        self._mark_transfer_cooldown(hash_id, transfer.sender_hash)
        if self._on_transfer_complete:
            await self._on_transfer_complete(transfer.to_dict())
        await self._update_file_message(transfer.to_dict())

    async def resume_transfer(self: MessagingBackend, transfer_id: str) -> bool:
        transfer = self._transfers.get(transfer_id)
        if not transfer:
            return False
        link = self.get_link(transfer.recipient_hash)
        if not link:
            return False
        dest = transfer.path
        if dest.exists():
            transfer.offset = dest.stat().st_size
        body = encode_payload({"transfer_id": transfer_id, "offset": transfer.offset})
        packet = await self._encrypt_for_link(link, MessageType.FILE_RESUME, body)
        await self._send_raw(link.transport_peer_id, link.transport, packet)
        return True

    def list_transfers(self: MessagingBackend) -> list[dict]:
        return [t.to_dict() for t in self._transfers.values()]

    async def cancel_transfer(self: MessagingBackend, transfer_id: str) -> bool:
        transfer = self._transfers.get(transfer_id)
        if not transfer:
            return False
        if transfer.state in (
            TransferState.COMPLETE,
            TransferState.CANCELLED,
            TransferState.FAILED,
            TransferState.REJECTED,
        ):
            return False
        transfer.state = TransferState.CANCELLED
        task = self._transfer_tasks.pop(transfer_id, None)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        peer_hash = (
            transfer.recipient_hash
            if transfer.sender_hash in {i.hash_id for i in self.identities.values()}
            else transfer.sender_hash
        )
        link = self.get_link(peer_hash)
        if link and link.handshake_complete:
            body = encode_payload(
                {"transfer_id": transfer_id, "reason": "cancelled"}
            )
            try:
                packet = await self._encrypt_for_link(
                    link, MessageType.FILE_REJECT, body
                )
                await self._send_raw(
                    link.transport_peer_id, link.transport, packet
                )
            except (KeyError, RuntimeError, OSError) as exc:
                log.warning("Could not notify cancel to peer: %s", exc)
        if self._on_transfer_progress:
            await self._on_transfer_progress(transfer.to_dict())
        await self._update_file_message(transfer.to_dict())
        return True

    async def _handle_file_reject(self: MessagingBackend, hash_id: str, body: bytes) -> None:
        data = decode_payload(body)
        transfer_id = data.get("transfer_id", "")
        transfer = self._transfers.get(transfer_id)
        if not transfer:
            return
        transfer.state = TransferState.CANCELLED
        task = self._transfer_tasks.pop(transfer_id, None)
        if task and not task.done():
            task.cancel()
        if self._on_transfer_progress:
            await self._on_transfer_progress(transfer.to_dict())
        await self._update_file_message(transfer.to_dict())

    def has_active_transfer_for(self: MessagingBackend, hash_id: str) -> bool:
        for transfer in self._transfers.values():
            if transfer.state not in (
                TransferState.TRANSFERRING,
                TransferState.ACCEPTED,
                TransferState.OFFERED,
            ):
                continue
            if transfer.recipient_hash == hash_id or transfer.sender_hash == hash_id:
                return True
        return False