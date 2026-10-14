"""Folder sync: keep a folder the same on two devices (yours and a contact's).

Two-way and incremental. Each side keeps an *index* of the synced folder:
for every file its SHA-256, size, modification time and a **version vector**
(how many changes each of the two devices made to it). Devices swap index
entries inside the encrypted session; whichever version is newer wins and is
fetched with the normal file transfer (end-to-end encrypted, resumable,
verified by its hash), so sync works on every path chat works on.

- **Conflicts** (both sides changed a file before they synced): both devices
  pick the same winner; the losing version is kept next to it as
  ``name (conflict <date> <device>).ext`` and synced too. Nothing is lost.
- **Deletes** propagate, but the file goes to ``.srltcp-sync/trash`` on the
  other side (kept 30 days), never straight to oblivion. Deleting and
  editing at once keeps the edit.
- **Safety**: a contact can only touch the folder of a sync you started or
  accepted with *that* contact; every path is checked (no ``..``, absolute
  paths, drive letters or the hidden ``.srltcp-sync`` folder), and a file is
  only sent if it's in our index with exactly the hash asked for.

Wire format: one session message type, ``SYNC``, with an ``op``:
``invite``/``accept``/``decline``/``stop`` (setting it up), ``index`` (a page
of index entries), ``get`` (send me this version of this file).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import secrets
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from srltcp.core.protocol.messages import MessageType, decode_payload
from srltcp.utils.files import safe_filename
from srltcp.utils.jsonstore import load_json, save_json
from srltcp.utils.logging import get_logger

if TYPE_CHECKING:
    from srltcp.core.messaging.backend import MessagingBackend
    from srltcp.core.messaging.models import FileTransfer

log = get_logger(__name__)

META_DIR = ".srltcp-sync"  # inside the synced folder: trash, partial downloads
SCAN_EVERY = 20.0
PAGE = 500  # index entries per message
MAX_FILES = 50_000
MAX_PATH = 1024
MAX_GETS = 4  # files requested from the contact at once, per sync
GET_TIMEOUT = 300.0  # ask again if a requested file hasn't started by then
TRASH_DAYS = 30
STATES = ("invited", "offered", "active", "paused", "stopped")


def clean_rel(raw: object) -> str | None:
    """A contact-supplied path inside the synced folder, or None if unsafe."""
    if not isinstance(raw, str) or not raw or len(raw) > MAX_PATH:
        return None
    if "\\" in raw or "\x00" in raw or ":" in raw or raw.startswith("/"):
        return None
    parts = PurePosixPath(raw).parts
    if not parts or any(p in ("", ".", "..") for p in parts) or parts[0] == META_DIR:
        return None
    if any(p != p.strip() for p in parts):
        return None
    return "/".join(parts)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(1024 * 1024):
            h.update(block)
    return h.hexdigest()


def _vv_cmp(a: dict[str, int], b: dict[str, int]) -> int:
    """1: a is newer, -1: b is newer, 0: same, 2: concurrent (both changed)."""
    keys = set(a) | set(b)
    a_more = any(a.get(k, 0) > b.get(k, 0) for k in keys)
    b_more = any(b.get(k, 0) > a.get(k, 0) for k in keys)
    if a_more and b_more:
        return 2
    return 1 if a_more else (-1 if b_more else 0)


def _vv_merge(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    return {k: max(a.get(k, 0), b.get(k, 0)) for k in set(a) | set(b)}


def _clean_entry(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    vv_raw = raw.get("vv")
    if not isinstance(vv_raw, dict) or len(vv_raw) > 4:
        return None
    vv: dict[str, int] = {}
    for k, v in vv_raw.items():
        if not (isinstance(k, str) and len(k) == 32) or isinstance(v, bool) or not isinstance(
            v, int
        ) or not 0 <= v < 2**53:
            return None
        vv[k] = v
    deleted = bool(raw.get("del"))
    sha = str(raw.get("sha", ""))
    size = raw.get("size", 0)
    mtime = raw.get("mtime", 0)
    if not deleted and (len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha)):
        return None
    if not isinstance(size, int) or not isinstance(mtime, int) or size < 0 or size > 2**50:
        return None
    return {"sha": "" if deleted else sha, "size": size, "mtime": mtime, "vv": vv, "del": deleted}


@dataclass
class SyncFolder:
    id: str
    peer: str
    name: str
    root: str = ""  # empty until the invited side accepts
    state: str = "invited"  # invited (we asked), offered (they asked), active, paused, stopped
    created: float = field(default_factory=time.time)
    invite_unsent: bool = False
    last_sync: float = 0.0
    conflicts: int = 0

    def public(self, extra: dict[str, Any]) -> dict[str, Any]:
        return {**asdict(self), **extra}


class SyncMixin:
    """Two-way folder sync with contacts (see module docstring)."""

    def _init_sync(self: MessagingBackend) -> None:
        self._syncs_path = self.trusted.path.parent / "syncs.json"
        self._syncs: dict[str, SyncFolder] = {}
        self._sync_index: dict[str, dict[str, dict[str, Any]]] = {}
        self._sync_dirty: dict[str, set[str]] = {}  # entries the contact hasn't seen
        self._sync_wanted: dict[str, dict[str, tuple[str, float]]] = {}  # path -> (sha, asked at)
        self._sync_queue: dict[str, dict[str, dict[str, Any]]] = {}  # path -> their entry
        # (sync id, path) -> the entry being downloaded right now
        self._sync_incoming: dict[tuple[str, str], dict[str, Any]] = {}
        self._sync_task: asyncio.Task[None] | None = None
        self._sync_lock = asyncio.Lock()
        raw = load_json(self._syncs_path, {})
        for item in raw.get("syncs", []) if isinstance(raw, dict) else []:
            with contextlib.suppress(TypeError, ValueError):
                known = SyncFolder.__dataclass_fields__
                sync = SyncFolder(**{k: v for k, v in item.items() if k in known})
                if sync.state in STATES:
                    self._syncs[sync.id] = sync
        for sync in self._syncs.values():
            # Downloads cut off by the last shutdown: the contact sends them again.
            if sync.root:
                shutil.rmtree(Path(sync.root) / META_DIR / "incoming", ignore_errors=True)

    # ── Storage ──

    def _index_path(self: MessagingBackend, sid: str) -> Path:
        return self.trusted.path.parent / f"sync-{sid}.json"

    def _save_syncs(self: MessagingBackend) -> None:
        save_json(self._syncs_path, {"syncs": [asdict(s) for s in self._syncs.values()]})

    def _index(self: MessagingBackend, sid: str) -> dict[str, dict[str, Any]]:
        index = self._sync_index.get(sid)
        if index is None:
            raw = load_json(self._index_path(sid), {})
            files = raw.get("files") if isinstance(raw, dict) else None
            index = {}
            for rel, entry in (files or {}).items():
                clean = _clean_entry(entry)
                if clean_rel(rel) and clean:
                    index[rel] = clean
            self._sync_index[sid] = index
        return index

    def _save_index(self: MessagingBackend, sid: str) -> None:
        save_json(self._index_path(sid), {"version": 1, "files": self._index(sid)})

    def _me(self: MessagingBackend) -> str:
        return self._identity_for_transport("tcp").hash_id

    # ── What the UI and API use ──

    def list_syncs(self: MessagingBackend, peer: str = "") -> list[dict[str, Any]]:
        out = []
        for sync in self._syncs.values():
            if peer and sync.peer != peer:
                continue
            index = self._index(sync.id) if sync.root else {}
            contact = self.trusted.get(sync.peer)
            out.append(sync.public({
                "files": sum(1 for e in index.values() if not e["del"]),
                "pending": len(self._sync_wanted.get(sync.id, {}))
                + len(self._sync_queue.get(sync.id, {})),
                "peer_name": contact.name if contact else sync.peer[:8],
            }))
        return out

    def _sync_view(self: MessagingBackend, sid: str) -> dict[str, Any] | None:
        return next((s for s in self.list_syncs() if s["id"] == sid), None)

    async def _sync_event(self: MessagingBackend, kind: str, sid: str) -> None:
        view = self._sync_view(sid)
        if self._on_event and view:
            await self._on_event({"kind": kind, "sync": view, "hash_id": view["peer"]})

    async def create_sync(
        self: MessagingBackend, peer: str, root: Path, name: str = ""
    ) -> dict[str, Any]:
        if not self.is_trusted(peer):
            raise ValueError("not a contact")
        root = root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        for sync in self._syncs.values():
            if sync.peer == peer and sync.root and Path(sync.root) == root:
                raise ValueError("this folder is already synced with this contact")
        sync = SyncFolder(
            id=secrets.token_hex(8), peer=peer, name=(name or root.name or "Folder")[:64],
            root=str(root), state="invited", invite_unsent=True,
        )
        self._syncs[sync.id] = sync
        self._save_syncs()
        await self._scan(sync)
        await self._send_invite(sync)
        self._ensure_sync_loop()
        return self._sync_view(sync.id) or {}

    async def accept_sync(self: MessagingBackend, sid: str, root: Path) -> dict[str, Any]:
        sync = self._syncs.get(sid)
        if not sync or sync.state != "offered":
            raise ValueError("no such invitation")
        root = root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        sync.root, sync.state = str(root), "active"
        self._save_syncs()
        await self._scan(sync)
        await self._send_sync(sync.peer, {"op": "accept", "id": sid})
        await self._send_index(sync, full=True)
        self._ensure_sync_loop()
        return self._sync_view(sid) or {}

    async def decline_sync(self: MessagingBackend, sid: str) -> None:
        sync = self._syncs.pop(sid, None)
        if sync:
            self._save_syncs()
            await self._send_sync(sync.peer, {"op": "decline", "id": sid})

    async def remove_sync(self: MessagingBackend, sid: str) -> bool:
        """Stop syncing (both sides). The files stay where they are."""
        sync = self._syncs.pop(sid, None)
        if not sync:
            return False
        self._save_syncs()
        self._sync_index.pop(sid, None)
        for store in (self._sync_dirty, self._sync_wanted, self._sync_queue):
            store.pop(sid, None)
        with contextlib.suppress(OSError):
            self._index_path(sid).unlink(missing_ok=True)
        await self._send_sync(sync.peer, {"op": "stop", "id": sid})
        return True

    async def set_sync_paused(self: MessagingBackend, sid: str, paused: bool) -> dict[str, Any]:
        sync = self._syncs.get(sid)
        if not sync or sync.state not in ("active", "paused"):
            raise ValueError("this folder isn't being synced")
        sync.state = "paused" if paused else "active"
        self._save_syncs()
        if not paused:
            await self.sync_now(sid)
        return self._sync_view(sid) or {}

    async def sync_now(self: MessagingBackend, sid: str) -> None:
        sync = self._syncs.get(sid)
        if sync and sync.state == "active":
            await self._sync_round(sync, full=True)

    # ── Loop ──

    def _ensure_sync_loop(self: MessagingBackend) -> None:
        if self._sync_task is None or self._sync_task.done():
            self._sync_task = asyncio.create_task(self._sync_loop())

    async def stop_sync_loop(self: MessagingBackend) -> None:
        if self._sync_task:
            self._sync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._sync_task
            self._sync_task = None

    async def _sync_loop(self: MessagingBackend) -> None:
        while self._running:
            for sync in list(self._syncs.values()):
                if sync.state == "active" or (sync.state == "invited" and sync.root):
                    try:
                        await self._sync_round(sync)
                    except Exception as exc:  # noqa: BLE001 - one folder must not stop the rest
                        log.warning("Sync of %s failed: %s", sync.name, exc)
            await asyncio.sleep(SCAN_EVERY)

    def _peer_ready(self: MessagingBackend, peer: str) -> bool:
        link = self.get_link(peer)
        return bool(link and link.handshake_complete)

    async def _sync_round(self: MessagingBackend, sync: SyncFolder, *, full: bool = False) -> None:
        await self._scan(sync)
        if sync.state != "active" or not self._peer_ready(sync.peer):
            return
        await self._send_index(sync, full=full)
        await self._request_files(sync)

    async def sync_link_up(self: MessagingBackend, peer: str) -> None:
        """The contact is connected: catch up on everything."""
        for sync in list(self._syncs.values()):
            if sync.peer != peer:
                continue
            if sync.invite_unsent:
                await self._send_invite(sync)
            elif sync.state == "active":
                self._sync_wanted.pop(sync.id, None)  # ask again: old requests are gone
                await self._sync_round(sync, full=True)
        if self._syncs:
            self._ensure_sync_loop()

    # ── Scanning our folder ──

    async def _scan(self: MessagingBackend, sync: SyncFolder) -> None:
        if not sync.root:
            return
        async with self._sync_lock:
            changed = await asyncio.to_thread(self._scan_blocking, sync)
        if changed:
            self._save_index(sync.id)
            self._sync_dirty.setdefault(sync.id, set()).update(changed)

    def _scan_blocking(self: MessagingBackend, sync: SyncFolder) -> set[str]:
        """Worker thread: find added, changed and deleted files."""
        root = Path(sync.root)
        if not root.is_dir():
            return set()  # folder unplugged/removed: don't turn that into deletes
        index = self._index(sync.id)
        me = self._me()
        changed: set[str] = set()
        seen: set[str] = set()
        # Files on their way in (or renamed aside by a conflict) aren't deletes.
        receiving = set(self._sync_wanted.get(sync.id, {})) | set(
            self._sync_queue.get(sync.id, {})
        )
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            rel_dir = Path(dirpath).relative_to(root).as_posix()
            if rel_dir == META_DIR or rel_dir.startswith(META_DIR + "/"):
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if not (rel_dir == "." and d == META_DIR)]
            for fname in filenames:
                full = Path(dirpath) / fname
                rel = clean_rel(full.relative_to(root).as_posix())
                if rel is None or full.is_symlink() or len(seen) >= MAX_FILES:
                    continue
                seen.add(rel)
                try:
                    st = full.stat()
                except OSError:
                    continue
                entry = index.get(rel)
                if entry and not entry["del"] and entry["size"] == st.st_size and (
                    entry["mtime"] == st.st_mtime_ns
                ):
                    continue
                if rel in receiving and entry is None:
                    continue
                try:
                    sha = _sha256_file(full)
                except OSError:
                    continue
                if entry and not entry["del"] and entry["sha"] == sha:
                    entry["size"], entry["mtime"] = st.st_size, st.st_mtime_ns
                    continue
                vv = dict(entry["vv"]) if entry else {}
                vv[me] = vv.get(me, 0) + 1
                index[rel] = {"sha": sha, "size": st.st_size, "mtime": st.st_mtime_ns,
                              "vv": vv, "del": False}
                changed.add(rel)
        for rel, entry in index.items():
            if not entry["del"] and rel not in seen and rel not in receiving:
                vv = dict(entry["vv"])
                vv[me] = vv.get(me, 0) + 1
                index[rel] = {"sha": "", "size": 0, "mtime": 0, "vv": vv, "del": True}
                changed.add(rel)
        self._purge_trash(root)
        return changed

    @staticmethod
    def _purge_trash(root: Path) -> None:
        trash = root / META_DIR / "trash"
        if not trash.is_dir():
            return
        limit = time.time() - TRASH_DAYS * 86400
        for day in trash.iterdir():
            with contextlib.suppress(OSError):
                if day.stat().st_mtime < limit:
                    shutil.rmtree(day, ignore_errors=True)

    # ── Talking to the contact ──

    async def _send_sync(self: MessagingBackend, peer: str, data: dict[str, Any]) -> bool:
        return await self._send_control(peer, MessageType.SYNC, data)

    async def _send_invite(self: MessagingBackend, sync: SyncFolder) -> None:
        sent = await self._send_sync(sync.peer, {"op": "invite", "id": sync.id, "name": sync.name})
        sync.invite_unsent = not sent
        self._save_syncs()

    async def _send_index(self: MessagingBackend, sync: SyncFolder, *, full: bool) -> None:
        index = self._index(sync.id)
        dirty = self._sync_dirty.get(sync.id, set())
        rels = sorted(index) if full else sorted(r for r in dirty if r in index)
        for start in range(0, len(rels), PAGE):
            page = {r: index[r] for r in rels[start:start + PAGE]}
            if not await self._send_sync(sync.peer, {"op": "index", "id": sync.id, "files": page}):
                return  # try again next round
        self._sync_dirty.pop(sync.id, None)
        sync.last_sync = time.time()

    async def _handle_sync(self: MessagingBackend, hash_id: str, body: bytes) -> None:
        data = decode_payload(body)
        op, sid = str(data.get("op", "")), str(data.get("id", ""))
        if len(sid) != 16 or any(c not in "0123456789abcdef" for c in sid):
            return
        sync = self._syncs.get(sid)
        if sync is not None and sync.peer != hash_id:
            log.warning("Sync message for a folder shared with someone else, from %s", hash_id[:8])
            return
        if op == "invite":
            if sync is None:
                name = safe_filename(str(data.get("name") or "Folder"))[:64] or "Folder"
                self._syncs[sid] = SyncFolder(id=sid, peer=hash_id, name=name, state="offered")
                self._save_syncs()
                await self._sync_event("sync_invite", sid)
            elif sync.state == "active":
                await self._send_sync(hash_id, {"op": "accept", "id": sid})  # lost answer
            return
        if sync is None:
            if op in ("index", "get", "accept"):
                await self._send_sync(hash_id, {"op": "stop", "id": sid})
            return
        if op == "accept" and sync.state == "invited":
            sync.state = "active"
            self._save_syncs()
            await self._sync_event("sync_update", sid)
            await self._sync_round(sync, full=True)
        elif op in ("decline", "stop"):
            sync.state = "stopped"
            self._save_syncs()
            await self._sync_event("sync_update", sid)
        elif op == "index" and sync.state == "active":
            files = data.get("files")
            if isinstance(files, dict) and len(files) <= PAGE:
                await self._merge_remote(sync, files)
        elif op == "get" and sync.state == "active":
            await self._serve_get(sync, data)

    # ── Merging their index into ours ──

    async def _merge_remote(
        self: MessagingBackend, sync: SyncFolder, files: dict[str, Any]
    ) -> None:
        async with self._sync_lock:  # not while a scan is reading the index
            await self._merge_locked(sync, files)
        await self._request_files(sync)
        await self._sync_event("sync_update", sync.id)

    async def _merge_locked(
        self: MessagingBackend, sync: SyncFolder, files: dict[str, Any]
    ) -> None:
        index = self._index(sync.id)
        queue = self._sync_queue.setdefault(sync.id, {})
        changed = False
        for raw_rel, raw_entry in files.items():
            rel, theirs = clean_rel(raw_rel), _clean_entry(raw_entry)
            if rel is None or theirs is None:
                continue
            if rel not in index and len(index) >= MAX_FILES:
                continue
            ours = index.get(rel)
            order = _vv_cmp(theirs["vv"], ours["vv"]) if ours else 1
            if order in (0, -1):
                continue  # we have this version or a newer one
            if order == 2:
                order = self._resolve_conflict(sync, rel, ours, theirs)  # type: ignore[arg-type]
                changed = True
                if order != 1:
                    continue
            # Theirs is newer (or won the conflict).
            if ours and not ours["del"] and not theirs["del"] and ours["sha"] == theirs["sha"]:
                ours["vv"] = _vv_merge(ours["vv"], theirs["vv"])
                changed = True
            elif theirs["del"]:
                await asyncio.to_thread(self._move_to_trash, sync, rel)
                index[rel] = theirs
                changed = True
            else:
                queue[rel] = theirs
        if changed:
            self._save_index(sync.id)

    def _resolve_conflict(
        self: MessagingBackend, sync: SyncFolder, rel: str,
        ours: dict[str, Any], theirs: dict[str, Any],
    ) -> int:
        """Both sides changed ``rel``. Returns 1 if theirs should be applied."""
        if not ours["del"] and not theirs["del"] and ours["sha"] == theirs["sha"]:
            ours["vv"] = _vv_merge(ours["vv"], theirs["vv"])
            return 0  # same content: nothing to do
        me = self._me()
        if theirs["del"]:  # they deleted, we edited: keep the edit
            ours["vv"] = _vv_merge(ours["vv"], theirs["vv"])
            ours["vv"][me] = ours["vv"].get(me, 0) + 1
            self._sync_dirty.setdefault(sync.id, set()).add(rel)
            return 0
        if ours["del"]:  # we deleted, they edited: their edit comes back
            return 1
        # Both edited. The same winner on both devices: more changes, then the
        # higher content hash. The loser is kept as a conflict copy.
        def rank(e: dict[str, Any]) -> tuple[int, str]:
            return sum(e["vv"].values()), e["sha"]

        if rank(ours) >= rank(theirs):
            ours["vv"] = _vv_merge(ours["vv"], theirs["vv"])
            ours["vv"][me] = ours["vv"].get(me, 0) + 1
            self._sync_dirty.setdefault(sync.id, set()).add(rel)
            return 0
        copy = self._conflict_copy(sync, rel)
        sync.conflicts += 1
        self._save_syncs()
        if copy:
            log.info("Sync conflict on %s: our version kept as %s", rel, copy)
        return 1

    def _conflict_copy(self: MessagingBackend, sync: SyncFolder, rel: str) -> str | None:
        """Rename our version of ``rel`` to a conflict copy (picked up as a new file)."""
        src = Path(sync.root) / rel
        if not src.is_file():
            return None
        p = PurePosixPath(rel)
        stamp = time.strftime("%Y-%m-%d %H%M")
        device = safe_filename(self._identity_for_transport("tcp").name)[:24] or "this device"
        for n in range(1, 100):
            suffix = "" if n == 1 else f" {n}"
            name = f"{p.stem} (conflict {stamp} {device}{suffix}){p.suffix}"
            new_rel = str(p.with_name(name))
            dst = Path(sync.root) / new_rel
            if not dst.exists():
                with contextlib.suppress(OSError):
                    os.replace(src, dst)
                    return new_rel
        return None

    def _move_to_trash(self: MessagingBackend, sync: SyncFolder, rel: str) -> None:
        src = Path(sync.root) / rel
        if not src.is_file():
            return
        dst = Path(sync.root) / META_DIR / "trash" / time.strftime("%Y-%m-%d") / rel
        with contextlib.suppress(OSError):
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                dst = dst.with_name(f"{dst.stem}-{secrets.token_hex(3)}{dst.suffix}")
            os.replace(src, dst)
            # Remove folders the delete left empty (never the synced folder itself).
            parent = src.parent
            root = Path(sync.root)
            while parent != root and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent

    # ── Fetching files ──

    async def _request_files(self: MessagingBackend, sync: SyncFolder) -> None:
        if sync.state != "active" or not self._peer_ready(sync.peer):
            return
        wanted = self._sync_wanted.setdefault(sync.id, {})
        now = time.monotonic()
        for rel, (_sha, asked) in list(wanted.items()):
            if now - asked > GET_TIMEOUT:
                wanted.pop(rel)  # never started: ask again
        queue = self._sync_queue.get(sync.id, {})
        for rel in list(queue):
            if len(wanted) >= MAX_GETS:
                break
            if rel in wanted:
                continue
            entry = queue.pop(rel)
            wanted[rel] = (entry["sha"], now)
            self._sync_incoming[(sync.id, rel)] = entry
            await self._send_sync(sync.peer, {"op": "get", "id": sync.id, "path": rel,
                                              "sha": entry["sha"]})

    async def _serve_get(self: MessagingBackend, sync: SyncFolder, data: dict[str, Any]) -> None:
        rel, sha = clean_rel(data.get("path")), str(data.get("sha", ""))
        entry = self._index(sync.id).get(rel or "")
        if rel is None or not entry or entry["del"] or entry["sha"] != sha:
            return  # not (or no longer) this version: our index tells them what's current
        path = Path(sync.root) / rel
        try:
            st = path.stat()
        except OSError:
            return
        if st.st_size != entry["size"] or st.st_mtime_ns != entry["mtime"]:
            return  # changed since the last scan; the next scan announces it
        await self.offer_file(
            sync.peer, path, filename=path.name, sync={"id": sync.id, "path": rel, "sha": sha}
        )

    def sync_offer_target(
        self: MessagingBackend, hash_id: str, meta: object, size: int
    ) -> Path | None:
        """Where an incoming sync file goes while it downloads (None: refuse)."""
        if not isinstance(meta, dict):
            return None
        sid, rel = str(meta.get("id", "")), clean_rel(meta.get("path"))
        sha = str(meta.get("sha", ""))
        sync = self._syncs.get(sid)
        if not sync or sync.peer != hash_id or sync.state != "active" or rel is None:
            return None
        wanted = self._sync_wanted.get(sid, {}).get(rel)
        if not wanted or wanted[0] != sha:
            return None  # only what we asked for
        incoming = self._sync_incoming.get((sid, rel))
        if incoming and incoming["size"] != size:
            return None
        part = Path(sync.root) / META_DIR / "incoming" / f"{secrets.token_hex(8)}.part"
        part.parent.mkdir(parents=True, exist_ok=True)
        return part

    async def sync_transfer_done(self: MessagingBackend, transfer: FileTransfer, ok: bool) -> None:
        """An incoming sync file finished (ok) or failed: put it in place."""
        meta = transfer.metadata.get("sync") or {}
        sid, rel = str(meta.get("id", "")), clean_rel(meta.get("path"))
        sync = self._syncs.get(sid)
        part = self._incoming_paths.get(transfer.id)
        entry = self._sync_incoming.pop((sid, rel or ""), None)
        self._sync_wanted.get(sid, {}).pop(rel or "", None)
        self._transfers.pop(transfer.id, None)
        self._incoming_paths.pop(transfer.id, None)
        if not sync or rel is None or part is None or entry is None or not ok:
            if part:
                with contextlib.suppress(OSError):
                    part.unlink(missing_ok=True)
            if sync and rel and entry and not ok:
                self._sync_queue.setdefault(sid, {})[rel] = entry  # try again
            return
        if transfer.sha256 != entry["sha"]:
            with contextlib.suppress(OSError):
                part.unlink(missing_ok=True)
            return
        async with self._sync_lock:
            placed = self._place_file(sync, rel, part, entry)
        if placed:
            await self._request_files(sync)
            await self._sync_event("sync_update", sid)

    def _place_file(
        self: MessagingBackend, sync: SyncFolder, rel: str, part: Path, entry: dict[str, Any]
    ) -> bool:
        index = self._index(sync.id)
        target = Path(sync.root) / rel
        ours = index.get(rel)
        # Changed here while it downloaded? Then that's a conflict: keep ours too.
        if target.exists() and ours and not ours["del"]:
            try:
                st = target.stat()
                if st.st_size != ours["size"] or st.st_mtime_ns != ours["mtime"]:
                    self._conflict_copy(sync, rel)
            except OSError:
                pass
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(part, target)
            if entry["mtime"]:
                os.utime(target, ns=(entry["mtime"], entry["mtime"]))
            st = target.stat()
        except OSError as exc:
            log.warning("Sync could not write %s: %s", rel, exc)
            with contextlib.suppress(OSError):
                part.unlink(missing_ok=True)
            return False
        index[rel] = {**entry, "size": st.st_size, "mtime": st.st_mtime_ns}
        self._save_index(sync.id)
        return True
