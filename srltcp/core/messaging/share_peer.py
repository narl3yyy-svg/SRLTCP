"""End-to-end encrypted folder and file sharing with contacts.

You pick a folder (or files, copied into a private share folder), the
contacts or group to share with, how long, how many downloads, and an
optional password. Each recipient gets their own grant (bound to their
identity), offered inside the encrypted session — right away, or as soon as
they're next connected. Grants survive restarts (owner-only ``shares.json``).
A password never leaves the session in the clear: the recipient sends it
inside the E2EE channel, the owner keeps only a salted scrypt hash, and
repeated wrong guesses lock the grant for a while.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from srltcp.core.protocol.messages import MessageType, encode_payload
from srltcp.utils.files import walk_directory
from srltcp.utils.logging import get_logger

if TYPE_CHECKING:
    from srltcp.core.messaging.backend import MessagingBackend

log = get_logger(__name__)

MAX_SHARE_ENTRIES = 500
MAX_PASSWORD_FAILURES = 5  # wrong passwords per grant ...
PASSWORD_LOCK_SECONDS = 600  # ... within this window lock it for this long
MAX_SHARE_RECIPIENTS = 64
FOREVER_EXPIRES = time.time() + 100 * 365 * 86400

SHARE_TTL_SECONDS: dict[str, float] = {
    "1m": 60,
    "5m": 300,
    "1h": 3600,
    "1d": 86400,
    "1w": 604800,
    "forever": FOREVER_EXPIRES - time.time(),
}

SHARE_DOWNLOAD_LIMITS: dict[str, int] = {
    "1": 1,
    "2": 2,
    "5": 5,
    "10": 10,
    "25": 25,
    "unlimited": 0,
}


def resolve_share_ttl(preset: str) -> float:
    """Return absolute expiry timestamp for a TTL preset name."""
    if preset == "forever":
        return FOREVER_EXPIRES
    seconds = SHARE_TTL_SECONDS.get(preset, SHARE_TTL_SECONDS["1h"])
    return time.time() + seconds


def resolve_download_limit(preset: str) -> int:
    return SHARE_DOWNLOAD_LIMITS.get(preset, 0)


@dataclass
class ShareGrant:
    grant_id: str
    owner_hash: str
    recipient_hash: str
    root: Path
    expires: float
    label: str = "shared"
    max_downloads: int = 0
    download_count: int = 0
    revoked: bool = False
    ttl_preset: str = "1h"
    download_limit_preset: str = "unlimited"
    kind: str = "folder"  # folder | files (copied into a private share folder)
    password_salt: str = ""  # owner side only
    password_hash: str = ""  # owner side only (scrypt)
    password_required: bool = False  # recipient side: what the owner told us
    offer_sent: bool = True  # owner side: delivered to the recipient yet?
    created: float = field(default_factory=time.time)
    _temp_files: list[Path] = field(default_factory=list)

    @property
    def has_password(self) -> bool:
        return bool(self.password_hash) or self.password_required

    def set_password(self, password: str) -> None:
        if not password:
            self.password_salt = self.password_hash = ""
            return
        salt = secrets.token_bytes(16)
        self.password_salt = salt.hex()
        self.password_hash = _scrypt(password, salt).hex()

    def check_password(self, password: str) -> bool:
        if not self.password_hash:
            return True
        try:
            want = bytes.fromhex(self.password_hash)
            got = _scrypt(password or "", bytes.fromhex(self.password_salt))
        except ValueError:
            return False
        return secrets.compare_digest(want, got)

    def to_store(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "owner_hash": self.owner_hash,
            "recipient_hash": self.recipient_hash,
            "root": str(self.root),
            "expires": self.expires,
            "label": self.label,
            "max_downloads": self.max_downloads,
            "download_count": self.download_count,
            "revoked": self.revoked,
            "ttl_preset": self.ttl_preset,
            "download_limit_preset": self.download_limit_preset,
            "kind": self.kind,
            "password_salt": self.password_salt,
            "password_hash": self.password_hash,
            "password_required": self.password_required,
            "offer_sent": self.offer_sent,
            "created": self.created,
        }

    @classmethod
    def from_store(cls, raw: dict[str, Any]) -> ShareGrant | None:
        try:
            return cls(
                grant_id=str(raw["grant_id"])[:64],
                owner_hash=str(raw["owner_hash"])[:64],
                recipient_hash=str(raw["recipient_hash"])[:64],
                root=Path(str(raw.get("root", "/"))),
                expires=float(raw["expires"]),
                label=str(raw.get("label", "shared"))[:128],
                max_downloads=int(raw.get("max_downloads", 0)),
                download_count=int(raw.get("download_count", 0)),
                revoked=bool(raw.get("revoked", False)),
                ttl_preset=str(raw.get("ttl_preset", "1h")),
                download_limit_preset=str(raw.get("download_limit_preset", "unlimited")),
                kind=str(raw.get("kind", "folder")),
                password_salt=str(raw.get("password_salt", "")),
                password_hash=str(raw.get("password_hash", "")),
                password_required=bool(raw.get("password_required", False)),
                offer_sent=bool(raw.get("offer_sent", True)),
                created=float(raw.get("created", time.time())),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def is_expired(self) -> bool:
        return time.time() >= self.expires

    def downloads_exhausted(self) -> bool:
        return self.max_downloads > 0 and self.download_count >= self.max_downloads

    def valid_for(self, requester_hash: str) -> bool:
        return (
            not self.revoked
            and not self.is_expired()
            and not self.downloads_exhausted()
            and secrets.compare_digest(self.recipient_hash, requester_hash)
        )

    def record_download(self) -> bool:
        if self.downloads_exhausted():
            return False
        self.download_count += 1
        return True

    def to_public_dict(self, *, remote: bool) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "owner_hash": self.owner_hash,
            "recipient_hash": self.recipient_hash,
            "label": self.label,
            "expires": self.expires,
            "max_downloads": self.max_downloads,
            "download_count": self.download_count,
            "downloads_remaining": (
                None
                if self.max_downloads == 0
                else max(0, self.max_downloads - self.download_count)
            ),
            "ttl_preset": self.ttl_preset,
            "download_limit_preset": self.download_limit_preset,
            "revoked": self.revoked,
            "remote": remote,
            "kind": self.kind,
            "password": self.has_password,
            "waiting": not remote and not self.offer_sent,
            "folder": "" if remote else str(self.root),
            "created": self.created,
        }


def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32
    )


class SharePeerMixin:
    """Offer and browse shared folders exclusively over E2EE sessions."""

    _share_grants: dict[str, ShareGrant]
    _remote_share_grants: dict[str, ShareGrant]

    def _init_share_peer(self: MessagingBackend) -> None:
        self._share_grants = {}
        self._remote_share_grants = {}
        self._share_listing_waiters: dict[str, asyncio.Future[list[dict[str, Any]]]] = {}
        self._share_passwords: dict[str, str] = {}  # recipient side, memory only
        self._share_failures: dict[str, list[float]] = {}
        self._share_list_errors: dict[str, str] = {}
        self._load_shares()

    def share_list_error(self: MessagingBackend, grant_id: str) -> str:
        """Why the owner refused the last listing (password_required,
        wrong_password, locked, access_denied) — or empty."""
        return self._share_list_errors.get(grant_id, "")

    # ── Persistence ──

    def _shares_path(self: MessagingBackend) -> Path:
        return self.trusted.path.parent / "shares.json"

    def share_files_dir(self: MessagingBackend) -> Path:
        """Where files picked for a share are copied (one folder per share)."""
        return self.trusted.path.parent / "share_files"

    def _load_shares(self: MessagingBackend) -> None:
        with contextlib.suppress(OSError, ValueError, TypeError, AttributeError):
            raw = json.loads(self._shares_path().read_text(encoding="utf-8"))
            stores = (("local", self._share_grants), ("remote", self._remote_share_grants))
            for key, store in stores:
                for item in raw.get(key, [])[:1000]:
                    grant = ShareGrant.from_store(item) if isinstance(item, dict) else None
                    if grant and not grant.is_expired() and not grant.revoked:
                        store[grant.grant_id] = grant

    def _save_shares(self: MessagingBackend) -> None:
        data = {
            "local": [g.to_store() for g in self._share_grants.values()],
            "remote": [g.to_store() for g in self._remote_share_grants.values()],
        }
        path = self._shares_path()
        tmp = path.with_suffix(".json.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("Could not save shares: %s", exc)

    def _share_root(self: MessagingBackend) -> Path:
        if self.config.incoming_dir:
            base = Path(self.config.incoming_dir).parent
        else:
            from srltcp.utils.platform import data_dir

            base = data_dir()
        shared = base / "shared"
        if hasattr(self, "_transfer_dir"):
            candidate = self._transfer_dir.parent / "shared"
            if candidate.exists():
                shared = candidate
        return shared.resolve()

    def _prune_share_grants(self: MessagingBackend) -> None:
        changed = False
        for store in (self._share_grants, self._remote_share_grants):
            expired = [
                gid
                for gid, g in store.items()
                if g.is_expired() or g.revoked
            ]
            for gid in expired:
                grant = store.pop(gid, None)
                changed = True
                self._share_passwords.pop(gid, None)
                if grant:
                    for tmp in grant._temp_files:
                        with contextlib.suppress(OSError):
                            tmp.unlink(missing_ok=True)
        if changed:
            self._remove_unused_share_files()
            self._save_shares()

    def _remove_unused_share_files(self: MessagingBackend) -> None:
        """Copies made for "share files" go once no grant uses them."""
        base = self.share_files_dir()
        if not base.is_dir():
            return
        used = {g.root.resolve() for g in self._share_grants.values() if g.kind == "files"}
        for child in base.iterdir():
            if child.is_dir() and child.resolve() not in used:
                shutil.rmtree(child, ignore_errors=True)

    def list_local_share_grants(self: MessagingBackend) -> list[dict[str, Any]]:
        self._prune_share_grants()
        return [g.to_public_dict(remote=False) for g in self._share_grants.values()]

    def list_remote_share_grants(self: MessagingBackend) -> list[dict[str, Any]]:
        self._prune_share_grants()
        return [g.to_public_dict(remote=True) for g in self._remote_share_grants.values()]

    async def notify_share_revoked(
        self: MessagingBackend, grant_id: str, recipient_hash: str
    ) -> bool:
        grant = self._share_grants.get(grant_id)
        if not grant:
            return False
        grant.revoked = True
        recipient_hash = grant.recipient_hash
        self._prune_share_grants()
        link = self.get_link(recipient_hash)
        if not link or not link.handshake_complete:
            return True
        body = encode_payload({"action": "revoke", "grant_id": grant_id})
        packet = await self._encrypt_for_link(link, MessageType.SHARE_LIST, body)
        await self._send_raw(link.transport_peer_id, link.transport, packet)
        return True

    async def offer_share(
        self: MessagingBackend,
        recipients: list[str],
        *,
        folder: Path,
        label: str = "",
        ttl_preset: str = "1h",
        download_limit_preset: str = "unlimited",
        password: str = "",
        kind: str = "folder",
    ) -> list[dict[str, Any]]:
        """Share ``folder`` with each trusted contact in ``recipients``.

        Contacts who aren't connected get the offer the next time they are."""
        root = folder.resolve()
        if not root.is_dir():
            raise ValueError("shared folder does not exist")
        if ttl_preset not in SHARE_TTL_SECONDS:
            raise ValueError("invalid time limit")
        if download_limit_preset not in SHARE_DOWNLOAD_LIMITS:
            raise ValueError("invalid download limit")
        if len(password) > 256:
            raise ValueError("password too long")
        identity = self._identity_for_transport("tcp")
        out: list[dict[str, Any]] = []
        for recipient in dict.fromkeys(recipients[:MAX_SHARE_RECIPIENTS]):
            if not self.trusted.is_trusted(recipient):
                continue
            grant = ShareGrant(
                grant_id=secrets.token_hex(16),
                owner_hash=identity.hash_id,
                recipient_hash=recipient,
                root=root,
                expires=resolve_share_ttl(ttl_preset),
                label=(label or root.name or "shared")[:128],
                max_downloads=resolve_download_limit(download_limit_preset),
                ttl_preset=ttl_preset,
                download_limit_preset=download_limit_preset,
                kind=kind if kind in ("folder", "files") else "folder",
                offer_sent=False,
            )
            grant.set_password(password)
            self._share_grants[grant.grant_id] = grant
            out.append(grant.to_public_dict(remote=False))
        self._save_shares()
        for recipient in {g["recipient_hash"] for g in out}:
            link = self.get_link(recipient)
            if link and link.handshake_complete:
                await self.send_pending_share_offers(recipient)
            else:
                self._kick_connect(recipient)
        return [self._share_grants[g["grant_id"]].to_public_dict(remote=False) for g in out]

    async def send_pending_share_offers(self: MessagingBackend, recipient_hash: str) -> None:
        """Deliver offers made while this contact was away (called on link-up)."""
        link = self.get_link(recipient_hash)
        if not link or not link.handshake_complete:
            return
        self._prune_share_grants()
        changed = False
        for grant in list(self._share_grants.values()):
            if grant.recipient_hash != recipient_hash or grant.offer_sent:
                continue
            body = encode_payload(
                {
                    "action": "offer",
                    "grant_id": grant.grant_id,
                    "label": grant.label,
                    "expires": grant.expires,
                    "owner_hash": grant.owner_hash,
                    "max_downloads": grant.max_downloads,
                    "ttl_preset": grant.ttl_preset,
                    "download_limit_preset": grant.download_limit_preset,
                    "password": bool(grant.password_hash),
                    "kind": grant.kind,
                }
            )
            try:
                packet = await self._encrypt_for_link(link, MessageType.SHARE_LIST, body)
                await self._send_raw(link.transport_peer_id, link.transport, packet)
            except (KeyError, OSError, RuntimeError, ConnectionError) as exc:
                log.debug("Share offer to %s not sent yet: %s", recipient_hash[:8], exc)
                return
            grant.offer_sent = True
            changed = True
            log.info("Share offer %s to %s", grant.grant_id[:8], recipient_hash[:8])
        if changed:
            self._save_shares()

    async def request_share_list(
        self: MessagingBackend,
        owner_hash: str,
        grant_id: str,
        *,
        timeout: float = 12.0,
        password: str | None = None,
    ) -> list[dict[str, Any]] | None:
        if password is not None:
            self._share_passwords[grant_id] = password[:256]
        grant = self._remote_share_grants.get(grant_id)
        if not grant or grant.owner_hash != owner_hash or grant.revoked:
            return None
        link = self.get_link(owner_hash)
        if not link or not link.handshake_complete:
            transport = link.transport if link else "tcp"
            await self.connect_to_peer(owner_hash, transport=transport)
            await self.wait_for_handshake(owner_hash, timeout=10.0)
            link = self.get_link(owner_hash)
        if not link or not link.handshake_complete:
            return None
        identity = self._identity_for_transport(link.transport)
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[list[dict[str, Any]]] = loop.create_future()
        self._share_listing_waiters[grant_id] = waiter
        body = encode_payload(
            {
                "action": "list",
                "grant_id": grant_id,
                "requester_hash": identity.hash_id,
                "password": self._share_passwords.get(grant_id, ""),
            }
        )
        try:
            packet = await self._encrypt_for_link(link, MessageType.SHARE_REQUEST, body)
            await self._send_raw(link.transport_peer_id, link.transport, packet)
            return await asyncio.wait_for(waiter, timeout=timeout)
        except TimeoutError:
            log.warning("Share listing timeout for grant %s", grant_id[:8])
            return None
        finally:
            self._share_listing_waiters.pop(grant_id, None)

    async def request_share_file(
        self: MessagingBackend,
        owner_hash: str,
        grant_id: str,
        rel_path: str,
        *,
        as_folder: bool = False,
    ) -> bool:
        grant = self._remote_share_grants.get(grant_id)
        if not grant or grant.owner_hash != owner_hash or grant.revoked:
            return False
        link = self.get_link(owner_hash)
        if not link or not link.handshake_complete:
            return False
        identity = self._identity_for_transport(link.transport)
        body = encode_payload(
            {
                "action": "fetch",
                "grant_id": grant_id,
                "path": rel_path,
                "requester_hash": identity.hash_id,
                "as_folder": as_folder,
                "password": self._share_passwords.get(grant_id, ""),
            }
        )
        packet = await self._encrypt_for_link(link, MessageType.SHARE_REQUEST, body)
        await self._send_raw(link.transport_peer_id, link.transport, packet)
        return True

    def _zip_share_path(self: MessagingBackend, grant: ShareGrant, rel: str) -> Path | None:
        rel_norm = rel.replace("\\", "/").strip("/")
        root = grant.root.resolve()
        target = (root / rel_norm).resolve() if rel_norm else root
        if not target.is_relative_to(root):
            return None
        if not target.exists():
            return None

        fd, tmp_name = tempfile.mkstemp(suffix=".zip", prefix="srltcp-share-")
        import os

        os.close(fd)
        zip_path = Path(tmp_name)
        base_name = target.name if target.is_dir() else f"{target.stem}.zip"

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            if target.is_file():
                zf.write(target, target.name)
            else:
                for entry in walk_directory(target):
                    if entry.get("type") != "file":
                        continue
                    rel_file = str(entry.get("name", ""))
                    full = target / rel_file
                    arcname = f"{base_name}/{rel_file}" if rel_norm else rel_file
                    zf.write(full, arcname)

        grant._temp_files.append(zip_path)
        return zip_path

    async def _handle_share_list(
        self: MessagingBackend, hash_id: str, body: bytes
    ) -> None:
        from srltcp.core.protocol.messages import decode_payload

        data = decode_payload(body)
        action = data.get("action", "")
        if action == "revoke":
            grant_id = str(data.get("grant_id", ""))
            remote = self._remote_share_grants.get(grant_id)
            if remote and remote.owner_hash == hash_id:
                remote.revoked = True
                self._prune_share_grants()
                if self._on_event:
                    await self._on_event(
                        {
                            "kind": "share_revoked",
                            "hash_id": hash_id,
                            "grant_id": grant_id,
                        }
                    )
            return
        if action == "offer":
            grant_id = str(data.get("grant_id", ""))[:64]
            if not grant_id:
                return
            prior = self._remote_share_grants.get(grant_id)
            if prior and prior.owner_hash != hash_id:
                return
            identity = ""
            link = self.get_link(hash_id)
            if link:
                identity = self._identity_for_transport(link.transport).hash_id
            grant = ShareGrant(
                grant_id=grant_id,
                owner_hash=hash_id,
                recipient_hash=identity,
                root=Path("/"),
                expires=float(data.get("expires", time.time() + 3600)),
                label=str(data.get("label", "shared"))[:128],
                max_downloads=int(data.get("max_downloads", 0)),
                ttl_preset=str(data.get("ttl_preset", "1h"))[:16],
                download_limit_preset=str(data.get("download_limit_preset", "unlimited"))[:16],
                kind="files" if data.get("kind") == "files" else "folder",
                password_required=bool(data.get("password", False)),
            )
            if len(self._remote_share_grants) >= 1000 and grant_id not in self._remote_share_grants:
                return  # a contact can't fill our disk with offers
            self._remote_share_grants[grant_id] = grant
            self._save_shares()
            if self._on_event:
                await self._on_event(
                    {
                        "kind": "share_offer",
                        "hash_id": hash_id,
                        "grant_id": grant_id,
                        "label": grant.label,
                        "expires": grant.expires,
                        "max_downloads": grant.max_downloads,
                        "password": grant.password_required,
                    }
                )
            return
        if action == "listing":
            grant_id = str(data.get("grant_id", ""))
            remote = self._remote_share_grants.get(grant_id)
            if not remote or remote.owner_hash != hash_id:
                return
            entries = data.get("entries", [])
            if not isinstance(entries, list):
                return
            normalized = []
            for entry in entries[:MAX_SHARE_ENTRIES]:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name", entry.get("path", "")))
                normalized.append(
                    {
                        "name": name,
                        "path": name,
                        "type": entry.get("type", "file"),
                        "size": entry.get("size", 0),
                    }
                )
            self._share_list_errors[grant_id] = str(data.get("error", ""))[:32]
            waiter = self._share_listing_waiters.get(grant_id)
            if waiter and not waiter.done():
                waiter.set_result(normalized)
            if self._on_event:
                payload: dict[str, Any] = {
                    "kind": "share_listing",
                    "hash_id": hash_id,
                    "grant_id": grant_id,
                    "entries": normalized,
                }
                if data.get("error"):
                    payload["error"] = data["error"]
                await self._on_event(payload)

    async def _handle_share_request(
        self: MessagingBackend, hash_id: str, body: bytes
    ) -> None:
        from srltcp.core.protocol.messages import decode_payload

        data = decode_payload(body)
        grant_id = str(data.get("grant_id", ""))
        # The requester is whoever authenticated this session — never a field
        # the remote side controls.
        requester = hash_id
        grant = self._share_grants.get(grant_id)
        action = data.get("action", "")
        link = self.get_link(hash_id)
        if not link or not link.handshake_complete:
            return

        error = ""
        if not grant or not grant.valid_for(requester):
            error = "access_denied"
        elif grant.password_hash:
            now = time.time()
            fails = [
                t for t in self._share_failures.get(grant_id, [])
                if now - t < PASSWORD_LOCK_SECONDS
            ]
            self._share_failures[grant_id] = fails
            if len(fails) >= MAX_PASSWORD_FAILURES:
                error = "locked"
            elif not grant.check_password(str(data.get("password", ""))[:256]):
                fails.append(now)
                error = "wrong_password" if data.get("password") else "password_required"
        if error:
            log.warning(
                "Share request denied for grant %s from %s (%s)",
                grant_id[:8],
                requester[:8],
                error,
            )
            if grant_id:
                denied = encode_payload(
                    {
                        "action": "listing",
                        "grant_id": grant_id,
                        "entries": [],
                        "error": error,
                    }
                )
                packet = await self._encrypt_for_link(link, MessageType.SHARE_LIST, denied)
                await self._send_raw(link.transport_peer_id, link.transport, packet)
            return
        assert grant is not None
        self._share_failures.pop(grant_id, None)

        if action == "list":
            entries = (await asyncio.to_thread(walk_directory, grant.root))[:MAX_SHARE_ENTRIES]
            listing = [
                {
                    "name": str(e.get("name", "")),
                    "path": str(e.get("name", "")),
                    "type": e.get("type", "file"),
                    "size": e.get("size", 0),
                }
                for e in entries
            ]
            response = encode_payload(
                {
                    "action": "listing",
                    "grant_id": grant_id,
                    "entries": listing,
                }
            )
            packet = await self._encrypt_for_link(link, MessageType.SHARE_LIST, response)
            await self._send_raw(link.transport_peer_id, link.transport, packet)
            return

        if action == "fetch":
            rel = str(data.get("path", "")).replace("\\", "/").lstrip("/")
            if ".." in rel.split("/"):
                return
            as_folder = bool(data.get("as_folder", False))
            root_resolved = grant.root.resolve()
            target = (root_resolved / rel).resolve() if rel else root_resolved
            if not target.is_relative_to(root_resolved):
                return

            send_path: Path | None = None
            if as_folder or target.is_dir():
                send_path = await asyncio.to_thread(self._zip_share_path, grant, rel)
            elif target.is_file():
                send_path = target
            if not send_path or not send_path.is_file():
                log.warning("Share fetch path invalid: %s", rel)
                return

            ok = grant.record_download()
            self._save_shares()
            if not ok:
                log.warning("Share download limit reached for grant %s", grant_id[:8])
                if send_path != target:
                    send_path.unlink(missing_ok=True)
                return

            transfer = await self.offer_file(hash_id, send_path, transport=link.transport)
            if transfer and send_path != target:
                # A zip made for this fetch: delete it when the transfer ends.
                transfer.metadata["temp_zip_path"] = str(send_path)
            elif not transfer and send_path != target:
                send_path.unlink(missing_ok=True)
            if transfer:
                log.info(
                    "Share file %s sent to %s (grant %s)",
                    send_path.name,
                    hash_id[:8],
                    grant_id[:8],
                )
            else:
                grant.download_count = max(0, grant.download_count - 1)
                log.warning("Share offer_file failed for grant %s", grant_id[:8])