"""File transfers inside an end-to-end encrypted session.

Protocol (every message travels inside the session)::

    sender                                   receiver
    FILE_OFFER    {id, filename, size}  -->
                                        <--  FILE_ACCEPT {id, offset}
    FILE_CHUNK    (id, offset, data)    -->  written in order, hashed while written
                                        <--  FILE_ACK    {id, offset}      on disk
    FILE_COMPLETE {id, sha256}          -->
                                        <--  FILE_ACK    {id, offset=size, ok}
    FILE_REJECT   {id, reason}          <->  cancel / refuse, either side

- Window: the sender keeps at most one path's window of unconfirmed bytes in
  flight. That keeps a fast link busy, bounds the receiver's memory and makes
  the sender's progress bar show what has really arrived.
- Recovery: no confirmation for a while or a path switch (relay -> direct)
  makes the sender go back to the last confirmed byte; the
  receiver drops what it already has. A transfer survives link drops and
  path changes without corrupting the file.
- Order: chunks may arrive out of order (after a path switch). The receiver
  holds early ones in memory (bounded) and writes strictly sequentially
  through one file handle, so the file never has holes, and both sides hash
  while streaming: no extra pass over the file before or after.
- Work off the event loop: reading, hashing, compressing and encrypting a
  chunk (sender) and writing + hashing (receiver) run in worker threads, so
  chat, calls and the UI stay responsive during big transfers.
- Cancel: FILE_REJECT both ways. A side that gets data, an offer or an
  accept for a transfer it cancelled answers with FILE_REJECT again, so a
  cancel lost with a dropping link still gets through. A cancelled or
  corrupt download is deleted.
- Restarts: unfinished transfers are kept in ``transfers.json`` and come
  back PAUSED; they resume like after a dropped link once the contact is
  connected. The receiver's partial file is what it has (it hashes that part
  again before writing more); the sender re-reads the part already sent, and
  gives up if the file was changed meanwhile.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

import zstandard as zstd

from srltcp.core.messaging import constants as C
from srltcp.core.messaging.disappear import wire_ttl
from srltcp.core.messaging.models import FileTransfer, TransferState
from srltcp.core.protocol.framing import pack_frame
from srltcp.core.protocol.messages import (
    Flags,
    MessageType,
    build_header,
    decode_payload,
    encode_payload,
    header_aad,
    normalize_transfer_id,
    pack_file_chunk,
    unpack_file_chunk,
)
from srltcp.utils.files import ensure_dir, purge_dir, safe_filename, unique_dest_path
from srltcp.utils.jsonstore import load_json, save_json
from srltcp.utils.logging import get_logger
from srltcp.utils.platform import data_dir

if TYPE_CHECKING:
    from srltcp.core.messaging.backend import MessagingBackend
    from srltcp.core.messaging.links import PeerLink

log = get_logger(__name__)

TERMINAL = frozenset(
    {TransferState.COMPLETE, TransferState.CANCELLED, TransferState.FAILED, TransferState.REJECTED}
)
ACTIVE = frozenset({TransferState.OFFERED, TransferState.ACCEPTED, TransferState.TRANSFERRING})
MAX_CHUNK = 1024 * 1024  # largest chunk a receiver accepts (after decompression)
RX_BUFFER_MAX = 48 * 1024 * 1024  # early + unwritten bytes kept per incoming file
ACK_BYTES = 1024 * 1024  # confirm at least every MiB written…
ACK_INTERVAL = 0.1  # …and never leave newer bytes unconfirmed longer than this
REJECT_REPEAT = 2.0  # answer stray data for a cancelled transfer at most this often
LINK_WAIT = 10.0  # a sender waits this long for a dropped session before pausing
JOURNAL_DELAY = 1.0  # batch journal writes (state changes come in bursts)
MAX_JOURNAL = 500
MAX_PENDING_INCOMING = 256  # unfinished incoming files per contact (a big photo batch fits)
MiB = 1024 * 1024

# Already-compressed formats: compressing them only burns CPU (and battery).
_NO_COMPRESS = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".avif",
    ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi",
    ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac",
    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".7z", ".rar", ".apk", ".jar",
    ".docx", ".xlsx", ".pptx", ".odt", ".pdf", ".woff2",
})


@dataclass(frozen=True)
class PathParams:
    chunk: int
    window: int
    stall: float
    level: int  # zstd level, 0 = never compress
    delay: float = 0.0


@dataclass
class _Outgoing:
    """Sender state of one transfer (lives across pauses and reconnects)."""

    acked: int = 0  # bytes the receiver has on disk
    next: int = 0  # next byte to send
    hashed: int = 0  # bytes fed to the hasher (file order)
    hasher: Any = field(default_factory=hashlib.sha256)
    result: bool | None = None  # receiver's verdict on the finished file
    complete_sent: bool = False
    progress_at: float = field(default_factory=time.monotonic)
    stalls: int = 0
    rewind: bool = False
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    compress: bool = True
    skip: int = 0  # chunks to send uncompressed before trying again
    cctx: Any = None
    level: int = 0


@dataclass
class _Incoming:
    """Receiver state of one transfer."""

    path: Path
    expected: int = 0  # contiguous bytes received (queued or written)
    written: int = 0  # bytes on disk and hashed
    hasher: Any = field(default_factory=hashlib.sha256)
    ahead: dict[int, bytes] = field(default_factory=dict)  # arrived early
    ahead_bytes: int = 0
    queue: list[bytes] = field(default_factory=list)  # in order, not yet written
    queued_bytes: int = 0
    fh: BinaryIO | None = None
    writer: asyncio.Task[None] | None = None
    sha256: str = ""  # announced by FILE_COMPLETE
    acked: int = -1
    acked_at: float = 0.0
    ack_task: asyncio.Task[None] | None = None
    last_data: float = field(default_factory=time.monotonic)
    closed: bool = False
    prehash: int = 0  # bytes on disk from before a restart, not hashed yet


PART_SUFFIX = ".srltcp-part"


def part_path(dest: Path) -> Path:
    """Where a download's bytes go until verified: hidden (a leading dot; the
    gallery and media scanner skip it), next to its final name so the move
    into place is a rename."""
    return dest.with_name(f".{dest.name}{PART_SUFFIX}")


def _place_verified(part: Path, dest: Path) -> Path:
    """Worker thread: move a verified download to its final name (or the next
    free one if something took that name meanwhile)."""
    import os

    if part == dest:
        return dest
    final = dest if not dest.exists() else unique_dest_path(dest.parent, dest.name)
    os.replace(part, final)
    return final


def _valid_sha(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _catch_up_hash(rx: _Incoming) -> None:
    """Worker thread: hash the part of the file written before a restart."""
    if not rx.prehash:
        return
    with open(rx.path, "rb") as fh:
        left = rx.prehash
        while left:
            block = fh.read(min(MiB, left))
            if not block:
                raise OSError("the partial download was changed or deleted")
            rx.hasher.update(block)
            left -= len(block)
    rx.prehash = 0


def _write_blocks(rx: _Incoming, blocks: list[bytes]) -> None:
    """Worker thread: append blocks to the file and the running hash."""
    if rx.fh is None:
        _catch_up_hash(rx)
        fh = open(rx.path, "r+b" if rx.path.exists() else "wb")  # noqa: SIM115
        fh.seek(rx.written)
        fh.truncate()
        rx.fh = fh
    for block in blocks:
        rx.fh.write(block)
        rx.hasher.update(block)


def _finish_file(rx: _Incoming) -> None:
    """Worker thread: complete the hash and flush the file to disk."""
    _catch_up_hash(rx)
    _close_file(rx, sync=True)


def _close_file(rx: _Incoming, *, sync: bool) -> None:
    fh, rx.fh = rx.fh, None
    if fh is None:
        return
    try:
        fh.flush()
        if sync:
            import os

            os.fsync(fh.fileno())
    finally:
        fh.close()


class TransferMixin:
    """Windowed, resumable, verified file transfers."""

    _transfers: dict[str, FileTransfer]
    _incoming_paths: dict[str, Path]
    _transfer_tasks: dict[str, asyncio.Task[None]]

    def _init_transfer(self: MessagingBackend) -> None:
        self._transfers = {}
        self._incoming_paths = {}
        self._transfer_tasks = {}
        self._out: dict[str, _Outgoing] = {}
        self._in: dict[str, _Incoming] = {}
        if self.config.incoming_dir:
            self._transfer_dir = Path(self.config.incoming_dir)
        else:
            self._transfer_dir = data_dir() / "transfers"
        ensure_dir(self._transfer_dir)
        self._transfer_cooldown_until: dict[str, float] = {}
        self._transfer_progress_emit: dict[str, float] = {}
        self._speed_sample: dict[str, tuple[float, int]] = {}
        self._reject_sent_at: dict[str, float] = {}
        # Outgoing offers not yet accepted: transfer id -> (sent at, tries).
        self._offer_sent_at: dict[str, tuple[float, int]] = {}
        # Unfinished transfers survive a restart (see restore_transfers).
        self._journal_path = data_dir() / "transfers.json"
        self._journal_handle: asyncio.TimerHandle | None = None
        # Copies of files sent from the browser, and folder zips: only needed
        # while their transfer can still run.
        self._copy_dirs = (data_dir() / "uploads", data_dir() / "transfers" / ".zip-temp")

    async def stop_transfers(self: MessagingBackend) -> None:
        tasks = [t for t in self._transfer_tasks.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._transfer_tasks.clear()
        for rx in list(self._in.values()):
            await self._rx_close(rx, delete=False)
        self._transfer_cooldown_until.clear()
        self._transfer_progress_emit.clear()
        self.save_transfer_journal()

    # ── Surviving a restart ──

    def _journal_changed(self: MessagingBackend) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.save_transfer_journal()
            return
        if self._journal_handle is None:
            self._journal_handle = loop.call_later(JOURNAL_DELAY, self.save_transfer_journal)

    def save_transfer_journal(self: MessagingBackend) -> None:
        if self._journal_handle:
            self._journal_handle.cancel()
            self._journal_handle = None
        if not self._history_enabled():
            # "Keep messages: until restart" keeps no trace of files either.
            with contextlib.suppress(OSError):
                self._journal_path.unlink(missing_ok=True)
            return
        keep = [
            {**t.to_dict(), "path": str(self._incoming_paths.get(t.id, t.path)),
             "final": str(t.path)}
            for t in self._transfers.values()
            if not t.metadata.get("sync")  # sync asks again after a restart
            and (
                t.state not in TERMINAL or t.metadata.get("reject_unsent")
                or self._awaits_expiry(t)
            )
        ]
        if keep or self._journal_path.exists():
            save_json(self._journal_path, {"version": 1, "transfers": keep[-MAX_JOURNAL:]})

    def _awaits_expiry(self: MessagingBackend, transfer: FileTransfer) -> bool:
        """A received disappearing file: kept in the journal until its timer
        deletes it, so that also happens after a restart."""
        return bool(
            transfer.metadata.get("ttl")
            and transfer.state == TransferState.COMPLETE
            and not self._is_outgoing(transfer)
        )

    def restore_transfers(self: MessagingBackend) -> None:
        """Load the transfers that were unfinished when the app stopped: they
        wait (PAUSED) and resume when their contact is connected. Call once
        the identities are loaded."""
        raw = load_json(self._journal_path, {}) if self._history_enabled() else {}
        items = raw.get("transfers") if isinstance(raw, dict) else None
        own = self._own_hash_ids()
        for item in items if isinstance(items, list) else []:
            transfer = self._transfer_from_journal(item, own) if isinstance(item, dict) else None
            if transfer and transfer.id not in self._transfers:
                self._transfers[transfer.id] = transfer
        if items:
            log.info("%d unfinished transfer(s) will resume when their contacts connect",
                     sum(t.state == TransferState.PAUSED for t in self._transfers.values()))
        self._purge_copies()

    def _transfer_from_journal(
        self: MessagingBackend, item: dict[str, Any], own: set[str]
    ) -> FileTransfer | None:
        try:
            tid = normalize_transfer_id(str(item["id"]))
            sender, recipient = str(item["sender_hash"]), str(item["recipient_hash"])
            path = Path(str(item["path"]))
            size = int(item["size"])
            state = TransferState(str(item["state"]))
            meta = dict(item.get("metadata") or {})
            offset = int(item.get("offset", 0))
            created = float(item.get("created", time.time()))
        except (KeyError, ValueError, TypeError):
            return None
        outgoing = sender in own
        if (not outgoing and recipient not in own) or size < 0:
            return None  # another identity (it was reset)
        final = Path(str(item.get("final") or path))  # (incoming: verified copy's name)
        transfer = FileTransfer(
            id=tid, sender_hash=sender, recipient_hash=recipient,
            filename=safe_filename(str(item.get("filename") or path.name)),
            path=path if outgoing else final, size=size, sha256="",
            transport=str(item.get("transport") or "tcp"),
            state=state, offset=max(0, min(offset, size)), created=created, metadata=meta,
        )
        if state in TERMINAL:
            if self._awaits_expiry(transfer):
                self._incoming_paths[tid] = path  # deleted when its timer runs out
                return transfer
            return transfer if meta.get("reject_unsent") else None
        transfer.state = TransferState.PAUSED
        if outgoing:
            try:
                st = path.stat()
                same = st.st_size == size and meta.get("mtime_ns", st.st_mtime_ns) == st.st_mtime_ns
            except OSError:
                same = False
            if not same:
                log.info("%s changed or was removed since the restart — not resumed",
                         transfer.filename)
                transfer.state = TransferState.FAILED
                meta.update(reason="changed", reject_unsent=True)
            return transfer
        try:
            have = min(path.stat().st_size, size)
        except OSError:
            have = 0  # partial file gone: start over
        self._incoming_paths[tid] = path
        self._in[tid] = _Incoming(path=path, expected=have, written=have, prehash=have)
        transfer.offset = have
        return transfer

    def _free_dest(self: MessagingBackend, filename: str) -> Path:
        """A final name no file has and no unfinished download will take."""
        directory = ensure_dir(self._transfer_dir)
        taken = {t.path for t in self._transfers.values() if t.state not in TERMINAL}
        safe = safe_filename(filename)
        stem, suffix = Path(safe).stem, Path(safe).suffix
        n = 0
        while True:
            dest = directory / (safe if not n else f"{stem}{n}{suffix}")
            if not dest.exists() and dest not in taken and not part_path(dest).exists():
                return dest
            n += 1

    def _purge_copies(self: MessagingBackend) -> None:
        keep = {
            t.path for t in self._transfers.values()
            if t.state not in TERMINAL and self._is_outgoing(t)
        }
        removed = sum(purge_dir(d, keep=keep) for d in self._copy_dirs)
        if removed:
            log.info("Removed %d leftover copies of sent files", removed)

    # ── Queries ──

    def transfer_file_path(self: MessagingBackend, transfer_id: str) -> Path | None:
        """Where a transfer's bytes live on disk (received copy or the sent original)."""
        transfer = self._transfers.get(transfer_id)
        if not transfer:
            return None
        incoming = self._incoming_paths.get(transfer_id)
        if incoming and incoming.is_file():
            return incoming
        if transfer.path.is_file():
            return transfer.path
        return None

    def list_transfers(self: MessagingBackend, *, active_only: bool = False) -> list[dict]:
        items = list(self._transfers.values())
        if active_only:
            items = [t for t in items if t.state in ACTIVE or t.state == TransferState.PAUSED]
        return [t.to_dict() for t in items]

    def peers_with_queued_files(self: MessagingBackend) -> set[str]:
        """Contacts we still have outgoing files waiting for."""
        own = self._own_hash_ids()
        return {
            t.recipient_hash
            for t in self._transfers.values()
            if t.state == TransferState.PAUSED and t.sender_hash in own
        }

    def has_active_transfer_for(self: MessagingBackend, hash_id: str) -> bool:
        # PAUSED is not "active": a paused transfer needs the link back, so it
        # must not suppress reconnects or dead-link detection.
        return any(
            t.state in ACTIVE and hash_id in (t.recipient_hash, t.sender_hash)
            for t in self._transfers.values()
        )

    def _mark_transfer_cooldown(self: MessagingBackend, *hash_ids: str) -> None:
        deadline = time.monotonic() + C.TRANSFER_COOLDOWN
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

    def _own_hash_ids(self: MessagingBackend) -> set[str]:
        return {i.hash_id for i in self.identities.values()}

    def _is_outgoing(self: MessagingBackend, transfer: FileTransfer) -> bool:
        return transfer.sender_hash in self._own_hash_ids()

    def _peer_hash_for_transfer(self: MessagingBackend, transfer: FileTransfer) -> str:
        return transfer.recipient_hash if self._is_outgoing(transfer) else transfer.sender_hash

    def _path_params(self: MessagingBackend, link: PeerLink) -> PathParams:
        if link.transport == "serial":
            return PathParams(
                C.SERIAL_CHUNK_SIZE, C.SERIAL_WINDOW, C.TRANSFER_STALL_SLOW, 0,
                C.SERIAL_CHUNK_DELAY,
            )
        if link.transport == "relay":
            return PathParams(C.RELAY_CHUNK_SIZE, C.RELAY_WINDOW, C.TRANSFER_STALL_SLOW, 3)
        if link.address.startswith("udp:"):
            return PathParams(C.UDP_CHUNK_SIZE, C.CHUNK_WINDOW, C.TRANSFER_STALL, 1)
        return PathParams(C.CHUNK_SIZE, C.CHUNK_WINDOW, C.TRANSFER_STALL, 1)

    # ── Progress ──

    async def _emit_transfer_progress(
        self: MessagingBackend, transfer: FileTransfer, *, force: bool = False
    ) -> None:
        now = time.monotonic()
        terminal = transfer.state in TERMINAL
        last = self._transfer_progress_emit.get(transfer.id, 0.0)
        if not force and not terminal and (now - last) < C.PROGRESS_EMIT_INTERVAL:
            return
        self._transfer_progress_emit[transfer.id] = now
        # Speed over the last moment, smoothed — not the average since the
        # start, which pauses and slow starts would drag down for good.
        if transfer.state == TransferState.TRANSFERRING:
            sample = self._speed_sample.get(transfer.id)
            if sample is None:
                self._speed_sample[transfer.id] = (now, transfer.offset)
            elif now - sample[0] >= 0.25:
                rate = max(0.0, (transfer.offset - sample[1]) / (now - sample[0]) / MiB)
                transfer.speed_mbps = (
                    rate if not transfer.speed_mbps else 0.5 * rate + 0.5 * transfer.speed_mbps
                )
                self._speed_sample[transfer.id] = (now, transfer.offset)
        else:
            transfer.speed_mbps = 0.0
            self._speed_sample.pop(transfer.id, None)
        if terminal:
            self._transfer_progress_emit.pop(transfer.id, None)
        if terminal or force:
            self._journal_changed()
        if transfer.metadata.get("sync"):
            # Folder sync: no chat bubble; the sync engine places the file.
            if terminal and not self._is_outgoing(transfer):
                await self.sync_transfer_done(transfer, transfer.state == TransferState.COMPLETE)
            elif terminal:
                self._transfers.pop(transfer.id, None)
            return
        payload = transfer.to_dict()
        if self._on_transfer_progress:
            await self._on_transfer_progress(payload)
        await self._update_file_message(payload, notify=terminal or force)

    # ── Small senders ──

    async def _send_control(
        self: MessagingBackend, hash_id: str, kind: MessageType, data: dict[str, Any]
    ) -> bool:
        link = self.get_link(hash_id)
        if not link or not link.handshake_complete:
            return False
        try:
            packet = self._seal(link, kind, encode_payload(data))
            await self._send_raw(link.transport_peer_id, link.transport, packet)
        except (KeyError, OSError, RuntimeError, ConnectionError) as exc:
            log.debug("%s to %s not sent: %s", kind.name, hash_id[:8], exc)
            return False
        return True

    async def _send_reject(
        self: MessagingBackend, hash_id: str, tid: str, reason: str = "cancelled"
    ) -> bool:
        self._reject_sent_at[tid] = time.monotonic()
        return await self._send_control(
            hash_id, MessageType.FILE_REJECT, {"transfer_id": tid, "reason": reason}
        )

    async def _repeat_reject(self: MessagingBackend, hash_id: str, transfer: FileTransfer) -> None:
        """The peer still acts on a transfer we ended: tell it again (rate-limited)."""
        if time.monotonic() - self._reject_sent_at.get(transfer.id, 0.0) < REJECT_REPEAT:
            return
        reason = "failed" if transfer.state == TransferState.FAILED else "cancelled"
        await self._send_reject(hash_id, transfer.id, reason)

    # ── Starting a transfer ──

    def _new_outgoing(
        self: MessagingBackend,
        recipient_hash: str,
        path: Path,
        transport: str,
        filename: str | None,
        ttl: int = 0,
    ) -> FileTransfer:
        transfer = FileTransfer.create(
            self._identity_for_transport(transport).hash_id, recipient_hash, path, transport
        )
        if ttl:
            transfer.metadata["ttl"] = ttl  # disappearing: both phones delete it
        if filename:
            transfer.filename = safe_filename(filename)
        if Path(transfer.filename).suffix.lower() in _NO_COMPRESS:
            transfer.metadata["no_compress"] = True
        with contextlib.suppress(OSError):
            # A restart resumes the transfer only if the file is unchanged.
            transfer.metadata["mtime_ns"] = path.stat().st_mtime_ns
        self._transfers[transfer.id] = transfer
        self._journal_changed()
        return transfer

    async def offer_file(
        self: MessagingBackend,
        recipient_hash: str,
        path: Path,
        *,
        transport: str = "tcp",
        filename: str | None = None,
        ttl: int = 0,
        sync: dict[str, Any] | None = None,
    ) -> FileTransfer | None:
        link = self.get_link(recipient_hash)
        if not link or not link.handshake_complete:
            log.warning("No encrypted link to %s", recipient_hash[:8])
            return None
        # No hashing up front: the hash is computed while the file streams
        # and sent at the end, so a large file starts moving immediately.
        transfer = self._new_outgoing(recipient_hash, path, link.transport, filename, ttl)
        if sync:
            transfer.metadata["sync"] = sync
        transfer.state = TransferState.OFFERED
        await self._send_file_offer(link, transfer)
        return transfer

    async def queue_file(
        self: MessagingBackend,
        recipient_hash: str,
        path: Path,
        *,
        transport: str = "tcp",
        filename: str | None = None,
        ttl: int = 0,
    ) -> FileTransfer:
        """Create an outgoing transfer for a contact with no session yet.

        It waits PAUSED (offset 0) and is offered by resume_transfer when the
        contact's session comes up."""
        transfer = self._new_outgoing(recipient_hash, path, transport, filename, ttl)
        transfer.state = TransferState.PAUSED
        log.info("Contact %s unreachable — file %s queued", recipient_hash[:8], transfer.id)
        return transfer

    async def _send_file_offer(
        self: MessagingBackend, link: PeerLink, transfer: FileTransfer
    ) -> None:
        _, tries = self._offer_sent_at.get(transfer.id, (0.0, 0))
        self._offer_sent_at[transfer.id] = (time.monotonic(), tries + 1)
        offer: dict[str, Any] = {
            "transfer_id": transfer.id, "filename": transfer.filename, "size": transfer.size,
        }
        if transfer.metadata.get("ttl"):
            offer["ttl"] = int(transfer.metadata["ttl"])
        if transfer.metadata.get("sync"):
            offer["sync"] = transfer.metadata["sync"]
        packet = self._seal(link, MessageType.FILE_OFFER, encode_payload(offer))
        await self._send_raw(link.transport_peer_id, link.transport, packet)

    async def _check_stale_offers(self: MessagingBackend) -> None:
        """Periodic upkeep (ping loop).

        Offers nobody answered (lost with a dropping link) must not count as
        an active transfer forever: that would stop pings, dead-link detection
        and reconnects for the contact. Re-offer once, then queue the file
        again (PAUSED) so it is offered on the next link-up. Incoming files
        that stopped receiving data are paused for the same reason."""
        now = time.monotonic()
        for tid, (sent_at, tries) in list(self._offer_sent_at.items()):
            transfer = self._transfers.get(tid)
            if not transfer or transfer.state != TransferState.OFFERED:
                self._offer_sent_at.pop(tid, None)
                continue
            if now - sent_at < C.OFFER_TIMEOUT:
                continue
            link = self.get_link(transfer.recipient_hash)
            if tries < 2 and link and link.handshake_complete:
                log.info("File offer %s unanswered — offering again", tid)
                with contextlib.suppress(Exception):
                    await self._send_file_offer(link, transfer)
                continue
            log.info("File offer %s unanswered — queued until reconnect", tid)
            self._offer_sent_at.pop(tid, None)
            transfer.state = TransferState.PAUSED
            await self._emit_transfer_progress(transfer, force=True)
        for tid, rx in list(self._in.items()):
            transfer = self._transfers.get(tid)
            if (
                transfer
                and transfer.state in (TransferState.ACCEPTED, TransferState.TRANSFERRING)
                and now - rx.last_data > C.INCOMING_IDLE
            ):
                log.info("No data for %s in %.0f s — paused", transfer.filename, C.INCOMING_IDLE)
                transfer.state = TransferState.PAUSED
                await self._emit_transfer_progress(transfer, force=True)

    async def _pause_outgoing_for_peer(self: MessagingBackend, peer_hash: str) -> None:
        """Link to a contact went down: outgoing files not yet streaming wait
        for the next session (the send loop pauses streaming ones itself)."""
        own = self._own_hash_ids()
        for tid, transfer in list(self._transfers.items()):
            if transfer.recipient_hash != peer_hash or transfer.sender_hash not in own:
                continue
            if transfer.state in (TransferState.OFFERED, TransferState.ACCEPTED):
                self._offer_sent_at.pop(tid, None)
                transfer.state = TransferState.PAUSED
                await self._emit_transfer_progress(transfer, force=True)

    # ── Receiver: offer, data, completion ──

    async def _handle_file_offer(self: MessagingBackend, hash_id: str, body: bytes) -> None:
        data = decode_payload(body)
        try:
            transfer_id = normalize_transfer_id(str(data["transfer_id"]))
            size = int(data["size"])
            filename = str(data.get("filename") or "file")[:255]
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("Invalid file offer from %s: %s", hash_id[:8], exc)
            return
        if size < 0 or size > 2**50:
            log.warning("Invalid file offer size from %s", hash_id[:8])
            return
        existing = self._transfers.get(transfer_id)
        if existing and existing.sender_hash != hash_id:
            log.warning("Transfer id collision from %s — offer ignored", hash_id[:8])
            return
        if not self.get_link(hash_id):
            return
        if existing:
            # Re-offer of a transfer we know (reconnect, lost answer).
            if existing.state == TransferState.COMPLETE:
                await self._send_control(
                    hash_id, MessageType.FILE_ACK,
                    {"transfer_id": transfer_id, "offset": existing.size, "ok": True},
                )
            elif existing.state in TERMINAL:
                await self._send_reject(
                    hash_id, transfer_id,
                    "failed" if existing.state == TransferState.FAILED else "cancelled",
                )
            else:
                rx = self._in.get(transfer_id)
                await self._send_control(
                    hash_id, MessageType.FILE_ACCEPT,
                    {"transfer_id": transfer_id, "offset": rx.expected if rx else 0},
                )
            return
        pending = sum(
            1 for t in self._transfers.values()
            if t.sender_hash == hash_id and t.state not in TERMINAL and not self._is_outgoing(t)
        )
        if pending >= MAX_PENDING_INCOMING:
            # Unfinished files from one contact are capped: a flood of offers
            # can't fill memory, the disk's file table or the chat.
            log.warning("Too many unfinished files from %s — offer refused", hash_id[:8])
            await self._send_reject(hash_id, transfer_id, "busy")
            return
        try:
            free = shutil.disk_usage(self._transfer_dir).free
        except OSError:
            free = size + 1
        if free < size + 16 * MiB:
            log.warning("No room for %s (%d bytes) from %s", filename, size, hash_id[:8])
            await self._send_reject(hash_id, transfer_id, "no_space")
            return
        filename = safe_filename(filename).lstrip(".") or "file"
        sync_meta = data.get("sync")
        if sync_meta is not None:
            # Folder sync: only files we asked for, into the sync folder.
            target = self.sync_offer_target(hash_id, sync_meta, size)
            if target is None:
                await self._send_reject(hash_id, transfer_id)
                return
            dest = target
        else:
            dest = self._free_dest(filename)
        link = self.get_link(hash_id)
        assert link is not None
        transfer = FileTransfer(
            id=transfer_id,
            sender_hash=hash_id,
            recipient_hash=self._identity_for_transport(link.transport).hash_id,
            filename=filename,
            path=dest,
            size=size,
            sha256="",
            transport=link.transport,
            state=TransferState.ACCEPTED,
        )
        ttl = wire_ttl(data)
        if ttl and sync_meta is None:
            transfer.metadata["ttl"] = ttl  # the sender's disappearing timer
        if sync_meta is not None:
            transfer.metadata["sync"] = sync_meta
        # The bytes go to a hidden partial file until verified (sync files have
        # their own partial files already): nothing half-received or damaged
        # ever shows under the real name.
        part = dest if sync_meta is not None else part_path(dest)
        self._transfers[transfer_id] = transfer
        self._incoming_paths[transfer_id] = part
        self._in[transfer_id] = _Incoming(path=part)
        with contextlib.suppress(OSError):
            part.touch(exist_ok=True)  # (reserves the name for concurrent offers)
        self._journal_changed()
        await self._send_control(
            hash_id, MessageType.FILE_ACCEPT, {"transfer_id": transfer_id, "offset": 0}
        )
        if sync_meta is not None:
            return  # no chat bubble for sync files
        if self._on_file_offer:
            await self._on_file_offer(transfer.to_dict())
        await self._append_file_message(
            sender_hash=hash_id,
            recipient_hash=transfer.recipient_hash,
            transport=link.transport,
            transfer=transfer.to_dict(),
            direction="in",
        )

    async def _handle_file_chunk(
        self: MessagingBackend, hash_id: str, body: bytes, *, compressed: bool = False
    ) -> None:
        """One decrypted chunk of an *incoming* transfer from its sender."""
        try:
            tid, offset, data = unpack_file_chunk(body)
        except ValueError as exc:
            log.warning("Malformed file chunk from %s: %s", hash_id[:8], exc)
            return
        transfer = self._transfers.get(tid)
        if not transfer or transfer.sender_hash != hash_id:
            log.debug("Unexpected chunk for transfer %s from %s", tid, hash_id[:8])
            return
        if transfer.state in TERMINAL:
            if transfer.state != TransferState.COMPLETE:
                await self._repeat_reject(hash_id, transfer)
            return
        rx = self._in.get(tid)
        if rx is None or rx.closed:
            return
        if offset >= transfer.size or offset < 0:
            log.warning("Chunk outside file bounds for %s from %s", tid, hash_id[:8])
            return
        limit = min(MAX_CHUNK, transfer.size - offset)
        if compressed:
            try:
                data = zstd.ZstdDecompressor().decompress(data, max_output_size=limit)
            except zstd.ZstdError as exc:
                log.warning("File chunk decompress failed for %s: %s", tid, exc)
                return
        if not data or len(data) > limit:
            log.warning("Chunk outside file bounds for %s from %s", tid, hash_id[:8])
            return
        rx.last_data = time.monotonic()
        if transfer.state != TransferState.TRANSFERRING:
            transfer.state = TransferState.TRANSFERRING
        end = offset + len(data)
        if end <= rx.expected or offset in rx.ahead:
            # A resend of what we have: our confirmations were lost (path
            # change, re-key). Confirm again so the sender moves on.
            if time.monotonic() - rx.acked_at >= ACK_INTERVAL:
                await self._rx_ack(transfer, rx)
            return
        if offset < rx.expected:
            data, offset = data[rx.expected - offset:], rx.expected  # overlaps what we have
        if offset > rx.expected:
            if rx.ahead_bytes + rx.queued_bytes + len(data) > RX_BUFFER_MAX:
                return  # the sender resends it once the gap is filled
            rx.ahead[offset] = data
            rx.ahead_bytes += len(data)
            return
        self._rx_append(rx, data)
        self._rx_take_ahead(rx)
        if rx.writer is None or rx.writer.done():
            rx.writer = asyncio.create_task(self._rx_write_loop(transfer, rx))

    def _rx_append(self: MessagingBackend, rx: _Incoming, data: bytes) -> None:
        rx.queue.append(data)
        rx.queued_bytes += len(data)
        rx.expected += len(data)

    def _rx_take_ahead(self: MessagingBackend, rx: _Incoming) -> None:
        """Move early chunks that now follow on into the write queue."""
        while rx.ahead:
            block = rx.ahead.pop(rx.expected, None)
            if block is not None:
                rx.ahead_bytes -= len(block)
                self._rx_append(rx, block)
                continue
            # Drop what is now behind us (resent with a different chunk size
            # after a path change); keep the part of a block that is still new.
            stale = [k for k in rx.ahead if k < rx.expected]
            if not stale:
                return
            for k in stale:
                block = rx.ahead.pop(k)
                rx.ahead_bytes -= len(block)
                tail = k + len(block) - rx.expected
                if tail > 0 and rx.expected not in rx.ahead:
                    rx.ahead[rx.expected] = block[-tail:]
                    rx.ahead_bytes += tail

    async def _rx_write_loop(self: MessagingBackend, transfer: FileTransfer, rx: _Incoming) -> None:
        try:
            while rx.queue and not rx.closed:
                blocks, rx.queue = rx.queue, []
                count = sum(len(b) for b in blocks)
                await asyncio.to_thread(_write_blocks, rx, blocks)
                rx.queued_bytes -= count
                rx.written += count
                transfer.offset = rx.written
                await self._rx_progress(transfer, rx)
            if not rx.closed and rx.sha256 and rx.written >= transfer.size:
                await self._rx_finalize(transfer, rx)  # hash arrived while writing
        except OSError as exc:
            log.warning("Writing %s failed: %s", transfer.filename, exc)
            if transfer.state not in TERMINAL:
                transfer.state = TransferState.FAILED
                await self._rx_close(rx, delete=True, wait=False)
                await self._send_reject(
                    transfer.sender_hash, transfer.id,
                    "no_space" if exc.errno == 28 else "failed",
                )
                await self._emit_transfer_progress(transfer, force=True)

    async def _rx_progress(self: MessagingBackend, transfer: FileTransfer, rx: _Incoming) -> None:
        if rx.written >= transfer.size and rx.sha256:
            await self._rx_finalize(transfer, rx)
            return
        if (
            rx.written >= transfer.size
            or rx.written - rx.acked >= ACK_BYTES
            or time.monotonic() - rx.acked_at >= ACK_INTERVAL
        ):
            await self._rx_ack(transfer, rx)
        elif rx.ack_task is None or rx.ack_task.done():
            # Confirm the rest shortly: the sender may be waiting on exactly
            # these bytes (its window is full).
            rx.ack_task = asyncio.create_task(self._rx_ack(transfer, rx, delay=ACK_INTERVAL))
        await self._emit_transfer_progress(transfer)

    async def _rx_ack(
        self: MessagingBackend, transfer: FileTransfer, rx: _Incoming, *, delay: float = 0.0
    ) -> None:
        if delay:
            await asyncio.sleep(delay)
            if rx.closed or rx.written <= rx.acked:
                return
        rx.acked, rx.acked_at = rx.written, time.monotonic()
        await self._send_control(
            transfer.sender_hash, MessageType.FILE_ACK,
            {"transfer_id": transfer.id, "offset": rx.written},
        )

    async def _rx_finalize(self: MessagingBackend, transfer: FileTransfer, rx: _Incoming) -> None:
        """All bytes written and the sender's hash known: verify, then tell the sender."""
        if rx.closed:
            return
        rx.closed = True  # set before any await: finalize runs once
        self._in.pop(transfer.id, None)
        try:
            await asyncio.to_thread(_finish_file, rx)
            ok = hmac.compare_digest(rx.hasher.hexdigest(), rx.sha256)
        except OSError as exc:
            log.warning("Finishing %s failed: %s", transfer.filename, exc)
            ok = False
        if transfer.state in TERMINAL:
            return  # cancelled meanwhile
        transfer.offset = transfer.size
        if ok and not transfer.metadata.get("sync"):
            try:
                final = await asyncio.to_thread(_place_verified, rx.path, transfer.path)
                transfer.path = final
                self._incoming_paths[transfer.id] = final
            except OSError as exc:
                log.warning("Moving %s into place failed: %s", transfer.filename, exc)
                ok = False
        if ok:
            transfer.sha256 = rx.sha256
            transfer.state = TransferState.COMPLETE
            log.info("Transfer complete: %s", transfer.filename)
            self._mark_transfer_cooldown(transfer.sender_hash, transfer.recipient_hash)
        else:
            transfer.state = TransferState.FAILED
            log.error("SHA256 mismatch for %s — discarded", transfer.filename)
            with contextlib.suppress(OSError):
                rx.path.unlink(missing_ok=True)
        await self._send_control(
            transfer.sender_hash, MessageType.FILE_ACK,
            {"transfer_id": transfer.id, "offset": transfer.size, "ok": ok},
        )
        await self._emit_transfer_progress(transfer, force=True)
        if ok and self._on_transfer_complete and not transfer.metadata.get("sync"):
            await self._on_transfer_complete(transfer.to_dict())

    async def _rx_close(
        self: MessagingBackend, rx: _Incoming, *, delete: bool, wait: bool = True
    ) -> None:
        rx.closed = True
        rx.queue.clear()
        rx.ahead.clear()
        rx.queued_bytes = rx.ahead_bytes = 0
        writer = rx.writer
        if wait and writer and not writer.done() and writer is not asyncio.current_task():
            with contextlib.suppress(Exception):
                await writer
        with contextlib.suppress(OSError):
            await asyncio.to_thread(_close_file, rx, sync=False)
        if delete:
            with contextlib.suppress(OSError):
                rx.path.unlink(missing_ok=True)
        for tid, other in list(self._in.items()):
            if other is rx:
                self._in.pop(tid, None)

    async def _handle_file_complete(self: MessagingBackend, hash_id: str, body: bytes) -> None:
        data = decode_payload(body)
        try:
            tid = normalize_transfer_id(str(data["transfer_id"]))
        except (KeyError, ValueError):
            return
        transfer = self._transfers.get(tid)
        if not transfer or transfer.sender_hash != hash_id or self._is_outgoing(transfer):
            return
        if transfer.state == TransferState.COMPLETE:
            await self._send_control(
                hash_id, MessageType.FILE_ACK,
                {"transfer_id": tid, "offset": transfer.size, "ok": True},
            )
            return
        if transfer.state in TERMINAL:
            await self._repeat_reject(hash_id, transfer)
            return
        rx = self._in.get(tid)
        sha = str(data.get("sha256", "")).lower()
        if rx is None or not _valid_sha(sha):
            return
        rx.sha256 = sha
        if rx.written >= transfer.size and (rx.writer is None or rx.writer.done()):
            await self._rx_finalize(transfer, rx)

    async def _recover_incoming_transfers_for_peer(self: MessagingBackend, peer_hash: str) -> None:
        """The sender's link dropped: incoming files wait for it (PAUSED)."""
        for transfer in list(self._transfers.values()):
            if transfer.sender_hash != peer_hash or transfer.state not in ACTIVE:
                continue
            if self._is_outgoing(transfer):
                continue
            transfer.state = TransferState.PAUSED
            await self._emit_transfer_progress(transfer, force=True)
            log.info(
                "Incoming transfer paused after disconnect: %s (%d/%d bytes)",
                transfer.filename, transfer.offset, transfer.size,
            )

    # ── Sender: accept, acks, the send loop ──

    async def _handle_file_accept(self: MessagingBackend, hash_id: str, body: bytes) -> None:
        data = decode_payload(body)
        try:
            tid = normalize_transfer_id(str(data["transfer_id"]))
            offset = int(data.get("offset", 0))
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("Invalid file accept from %s: %s", hash_id[:8], exc)
            return
        transfer = self._transfers.get(tid)
        if not transfer or transfer.recipient_hash != hash_id or not self._is_outgoing(transfer):
            return
        if transfer.state in (TransferState.CANCELLED, TransferState.FAILED):
            await self._repeat_reject(hash_id, transfer)
            return
        if transfer.state in TERMINAL:
            return
        offset = max(0, min(offset, transfer.size))
        out = self._out.setdefault(tid, _Outgoing())
        # The receiver's offset is authoritative: it is what it has.
        out.acked = out.next = offset
        out.progress_at = time.monotonic()
        out.stalls = 0
        out.complete_sent = False
        transfer.offset = offset
        self._offer_sent_at.pop(tid, None)
        transfer.state = TransferState.TRANSFERRING
        task = self._transfer_tasks.get(tid)
        if task and not task.done():
            out.wake.set()
            return
        await self._emit_transfer_progress(transfer, force=True)
        self._transfer_tasks[tid] = asyncio.create_task(self._send_file_chunks(hash_id, transfer))

    async def _handle_file_ack(self: MessagingBackend, hash_id: str, body: bytes) -> None:
        data = decode_payload(body)
        try:
            tid = normalize_transfer_id(str(data["transfer_id"]))
            offset = int(data.get("offset", 0))
        except (KeyError, ValueError, TypeError):
            return
        transfer = self._transfers.get(tid)
        if not transfer or transfer.recipient_hash != hash_id or not self._is_outgoing(transfer):
            return
        if transfer.state in (TransferState.CANCELLED, TransferState.FAILED):
            await self._repeat_reject(hash_id, transfer)
            return
        out = self._out.get(tid)
        if transfer.state in TERMINAL or out is None:
            return
        offset = max(0, min(offset, transfer.size))
        if offset > out.acked:
            out.acked = offset
            out.progress_at = time.monotonic()
            out.stalls = 0
            transfer.offset = offset
        out.next = max(out.next, out.acked)
        if "ok" in data and offset >= transfer.size:
            out.result = bool(data.get("ok"))
        out.wake.set()
        if transfer.state == TransferState.TRANSFERRING:
            await self._emit_transfer_progress(transfer)

    async def _handle_file_resume(self: MessagingBackend, hash_id: str, body: bytes) -> None:
        """FILE_RESUME after a reconnect.

        To the sender it means "continue from my offset" (same as an accept).
        To the receiver it means "the sender is back": answer with our own
        offset, which is authoritative, and the sender restarts.
        """
        data = decode_payload(body)
        try:
            tid = normalize_transfer_id(str(data.get("transfer_id", "")))
        except ValueError:
            return
        transfer = self._transfers.get(tid)
        if transfer and transfer.sender_hash == hash_id and not self._is_outgoing(transfer):
            if transfer.state not in TERMINAL:
                await self.resume_transfer(tid)
            return
        if transfer is None:
            # We don't have it any more (e.g. "Keep messages: until restart"):
            # the receiver must not wait for it forever.
            await self._send_control(
                hash_id, MessageType.FILE_REJECT, {"transfer_id": tid, "reason": "failed"}
            )
            return
        await self._handle_file_accept(hash_id, body)

    async def _await_link(
        self: MessagingBackend, hash_id: str, transfer: FileTransfer
    ) -> PeerLink | None:
        deadline = time.monotonic() + LINK_WAIT
        while time.monotonic() < deadline:
            if transfer.state == TransferState.CANCELLED:
                return None
            link = self.data_link(hash_id)  # (never over Tor: it only rings the bell)
            if link:
                return link
            await asyncio.sleep(0.25)
        return None

    def _prep_chunk(
        self: MessagingBackend,
        fh: BinaryIO,
        out: _Outgoing,
        transfer: FileTransfer,
        offset: int,
        length: int,
        level: int,
        nonce: bytes,
        aes: Any,
        framed: bool,
    ) -> tuple[bytes, int]:
        """Worker thread: read, hash, maybe compress, encrypt (and frame) one chunk."""
        if out.hashed < offset:  # resumed after a restart: hash what they already have
            fh.seek(out.hashed)
            while out.hashed < offset:
                block = fh.read(min(MiB, offset - out.hashed))
                if not block:
                    raise OSError("file shrank while sending")
                out.hasher.update(block)
                out.hashed += len(block)
        fh.seek(offset)
        data = fh.read(length)
        if not data:
            raise OSError("file shrank while sending")
        if offset == out.hashed:
            out.hasher.update(data)
            out.hashed += len(data)
        payload, flags = data, 0
        if level and out.compress and len(data) >= 4096:
            if out.skip:
                out.skip -= 1
            else:
                if out.cctx is None or out.level != level:
                    out.cctx, out.level = zstd.ZstdCompressor(level=level), level
                packed = out.cctx.compress(data)
                if len(packed) < len(data) * 0.92:
                    payload, flags = packed, Flags.COMPRESSED
                else:
                    out.skip = 16  # incompressible here: don't keep trying every chunk
        flags |= Flags.ENCRYPTED | Flags.E2EE
        body = pack_file_chunk(transfer.id, offset, payload)
        sealed = nonce + aes.encrypt(nonce, body, header_aad(MessageType.FILE_CHUNK, flags))
        packet = build_header(MessageType.FILE_CHUNK, flags=flags, body=sealed)
        return (pack_frame(packet) if framed else packet), len(data)

    async def _send_file_chunks(
        self: MessagingBackend, hash_id: str, transfer: FileTransfer
    ) -> None:
        out = self._out.setdefault(transfer.id, _Outgoing())
        out.compress = not transfer.metadata.get("no_compress")
        fh: BinaryIO | None = None
        path_key: tuple[str, int] | None = None
        try:
            fh = await asyncio.to_thread(open, transfer.path, "rb")
            while True:
                if transfer.state == TransferState.CANCELLED:
                    return
                if out.result is not None:
                    break
                link = self.data_link(hash_id)
                if not link:
                    link = await self._await_link(hash_id, transfer)
                    if link is None:
                        if transfer.state != TransferState.CANCELLED:
                            log.warning(
                                "Link lost during transfer %s at %d/%d — paused",
                                transfer.id, out.acked, transfer.size,
                            )
                            transfer.state = TransferState.PAUSED
                            await self._emit_transfer_progress(transfer, force=True)
                        return
                key = (link.transport_peer_id, link.crypto.epoch)
                if key != path_key:
                    if path_key is not None:
                        out.next = out.acked  # new path or keys: in-flight data is gone
                        out.complete_sent = False
                    path_key = key
                if out.rewind:
                    out.rewind = False
                    out.next = out.acked
                params = self._path_params(link)
                if out.next < transfer.size and out.next - out.acked < params.window:
                    length = min(params.chunk, transfer.size - out.next)
                    nonce, aes = link.crypto.reserve()
                    framed = link.transport == "tcp"
                    frame, sent = await asyncio.to_thread(
                        self._prep_chunk, fh, out, transfer, out.next, length,
                        params.level, nonce, aes, framed,
                    )
                    if transfer.state == TransferState.CANCELLED:
                        return
                    try:
                        if framed and self.tcp_transport:
                            await self.tcp_transport.send_framed(link.transport_peer_id, frame)
                        else:
                            await self._send_raw(link.transport_peer_id, link.transport, frame)
                    except (KeyError, OSError, ConnectionError) as exc:
                        log.debug("Chunk send to %s failed: %s", hash_id[:8], exc)
                        out.next = out.acked
                        await asyncio.sleep(0.2)
                        continue
                    out.next += sent
                    if params.delay:
                        await asyncio.sleep(params.delay)
                    continue
                now = time.monotonic()
                all_sent = out.next >= transfer.size and out.hashed >= transfer.size
                if all_sent and not out.complete_sent:
                    transfer.sha256 = out.hasher.hexdigest()
                    out.complete_sent = await self._send_control(
                        hash_id, MessageType.FILE_COMPLETE,
                        {"transfer_id": transfer.id, "sha256": transfer.sha256},
                    )
                out.wake.clear()
                wait = params.stall - (now - out.progress_at)
                if wait > 0:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(out.wake.wait(), timeout=wait)
                    continue
                # Nothing confirmed for a while: resend from the last confirmed byte.
                out.stalls += 1
                out.progress_at = now
                out.next = out.acked
                out.complete_sent = False
                if out.stalls >= 3:
                    log.warning(
                        "No answer from %s for %s — reconnecting", hash_id[:8], transfer.filename
                    )
                    transfer.state = TransferState.PAUSED
                    await self._emit_transfer_progress(transfer, force=True)
                    await self._teardown_link(hash_id)
                    self._schedule_reconnect(hash_id)
                    return
                log.info("Transfer %s stalled at %d — resending", transfer.id, out.acked)

            if out.result:
                transfer.state = TransferState.COMPLETE
                transfer.offset = transfer.size
                log.info("Transfer complete: %s", transfer.filename)
                self._mark_transfer_cooldown(hash_id, transfer.recipient_hash)
                await self._emit_transfer_progress(transfer, force=True)
                if self._on_transfer_complete and not transfer.metadata.get("sync"):
                    await self._on_transfer_complete(transfer.to_dict())
            else:
                log.warning("%s arrived damaged at %s", transfer.filename, hash_id[:8])
                transfer.state = TransferState.FAILED
                await self._emit_transfer_progress(transfer, force=True)
        except asyncio.CancelledError:
            # Only shutdown cancels a running transfer (a cancel or reject
            # sets the final state first): it resumes after the restart.
            if transfer.state not in TERMINAL:
                transfer.state = TransferState.PAUSED
            raise
        except Exception as exc:
            log.warning("Transfer failed for %s: %s", transfer.filename, exc)
            transfer.state = TransferState.FAILED
            await self._repeat_reject(hash_id, transfer)
            await self._emit_transfer_progress(transfer, force=True)
        finally:
            if fh is not None:
                with contextlib.suppress(OSError):
                    fh.close()
            if self._transfer_tasks.get(transfer.id) is asyncio.current_task():
                self._transfer_tasks.pop(transfer.id, None)
            if transfer.state in TERMINAL:
                self._out.pop(transfer.id, None)
                self._cleanup_temp_zip(transfer)

    # ── Resume, cancel ──

    async def resume_transfer(self: MessagingBackend, transfer_id: str) -> bool:
        try:
            tid = normalize_transfer_id(transfer_id)
        except ValueError:
            return False
        transfer = self._transfers.get(tid)
        if not transfer or transfer.state in TERMINAL:
            return False
        peer_hash = self._peer_hash_for_transfer(transfer)
        link = self.data_link(peer_hash)
        if not link:
            return False
        if self._is_outgoing(transfer):
            # Re-send the full offer: the receiver may never have got it (file
            # queued while offline, or the link dropped right after offering).
            # A receiver that already knows the transfer answers with its
            # offset, so this also resumes a partial transfer.
            if transfer.state == TransferState.PAUSED:
                transfer.state = TransferState.OFFERED
            await self._send_file_offer(link, transfer)
            await self._emit_transfer_progress(transfer, force=True)
            return True
        rx = self._in.get(tid)
        offset = rx.expected if rx else 0
        # The sender restarts from this offset when it answers (accept).
        return await self._send_control(
            peer_hash, MessageType.FILE_RESUME, {"transfer_id": tid, "offset": offset}
        )

    async def _resume_paused_transfers_for_peer(self: MessagingBackend, peer_hash: str) -> None:
        for tid, transfer in list(self._transfers.items()):
            if self._peer_hash_for_transfer(transfer) != peer_hash:
                continue
            if transfer.metadata.get("reject_unsent") and transfer.state in TERMINAL:
                # Ended while the contact was away: tell them now.
                reason = "failed" if transfer.state == TransferState.FAILED else "cancelled"
                if await self._send_reject(peer_hash, tid, reason):
                    transfer.metadata.pop("reject_unsent", None)
                    self._journal_changed()
                continue
            if transfer.state != TransferState.PAUSED:
                continue
            try:
                await self.resume_transfer(tid)
            except Exception as exc:  # noqa: BLE001 - one bad transfer must not stop the rest
                log.warning("Resume failed for %s after reconnect: %s", tid, exc)

    async def cancel_transfer(self: MessagingBackend, transfer_id: str) -> bool:
        try:
            tid = normalize_transfer_id(transfer_id)
        except ValueError:
            return False
        transfer = self._transfers.get(tid)
        if not transfer or transfer.state in TERMINAL:
            return False
        transfer.state = TransferState.CANCELLED
        self._offer_sent_at.pop(tid, None)
        await self._stop_transfer_work(transfer)
        peer_hash = self._peer_hash_for_transfer(transfer)
        if not await self._send_reject(peer_hash, tid):
            transfer.metadata["reject_unsent"] = True
        await self._emit_transfer_progress(transfer, force=True)
        log.info("Transfer cancelled: %s", transfer.filename)
        return True

    async def _stop_transfer_work(self: MessagingBackend, transfer: FileTransfer) -> None:
        """Stop the send loop or the writer; delete an unfinished download."""
        task = self._transfer_tasks.pop(transfer.id, None)
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._out.pop(transfer.id, None)
        rx = self._in.get(transfer.id)
        if rx is not None:
            await self._rx_close(rx, delete=True)
        elif not self._is_outgoing(transfer):
            path = self._incoming_paths.get(transfer.id)
            if path and transfer.state != TransferState.COMPLETE:
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
        self._cleanup_temp_zip(transfer)

    async def _handle_file_reject(self: MessagingBackend, hash_id: str, body: bytes) -> None:
        data = decode_payload(body)
        try:
            tid = normalize_transfer_id(str(data.get("transfer_id", "")))
        except ValueError:
            return
        transfer = self._transfers.get(tid)
        if not transfer or self._peer_hash_for_transfer(transfer) != hash_id:
            return
        if transfer.state in TERMINAL:
            return
        reason = str(data.get("reason", ""))
        if reason in ("failed", "no_space"):
            transfer.state = TransferState.FAILED
            transfer.metadata["reason"] = reason
        else:
            transfer.state = TransferState.CANCELLED
        self._offer_sent_at.pop(tid, None)
        self._reject_sent_at[tid] = time.monotonic()  # no echo back
        await self._stop_transfer_work(transfer)
        log.info(
            "Transfer %s ended by %s (%s)", transfer.filename, hash_id[:8], reason or "cancelled"
        )
        await self._emit_transfer_progress(transfer, force=True)

    def _cleanup_temp_zip(self: MessagingBackend, transfer: FileTransfer) -> None:
        temp_zip = transfer.metadata.pop("temp_zip_path", None)
        if temp_zip:
            with contextlib.suppress(OSError):
                Path(temp_zip).unlink(missing_ok=True)
