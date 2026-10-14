"""Encrypted backup of your identity, contacts, chats, groups and settings.

A backup is one file, sealed with a password you choose::

    "SRLTCP-BACKUP1\\n" | salt (16) | log2 N, r, p (3 bytes) | nonce (12) |
    AES-256-GCM( zstd( JSON {"version", "created", "app", "files"} ) )

The key comes from the password through scrypt (N = 2^16, r = 8: about
64 MiB and a fraction of a second per guess), so a stolen backup with a
strong password is useless. Everything in the header is authenticated.

What's in it: the files listed in :data:`BACKUP_FILES`, decrypted (the
on-device data key never leaves the device). Not in it: received files,
paused transfers, shared folders, Tor keys (a restored device gets a new
onion address and tells its contacts), the localhost certificate and token.

Restoring replaces those files on this device and needs an app restart.
Run the restored identity on one device only: two devices with the same
identity would keep pushing each other off the network.
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any

import zstandard as zstd
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from srltcp.utils import vault

MAGIC = b"SRLTCP-BACKUP1\n"
SCRYPT_LOG_N, SCRYPT_R, SCRYPT_P = 16, 8, 1
MIN_PASSWORD = 8
MAX_BACKUP = 256 * 1024 * 1024
BACKUP_FILES = (
    "identities/identity_tcp.json",
    "identities/identity_serial.json",
    "trusted.json",
    "messages.json",
    "groups.json",
    "settings.json",
)
# Every file the app keeps encrypted at rest (backed up or not).
APP_FILES = (*BACKUP_FILES, "shares.json", "pairing_tokens.json", "transfers.json")
# Restoring onto another device: where its own folders and programs are.
_DEVICE_SETTINGS = ("incoming_files_dir", "shared_folder", "tor_path", "lan_ip", "bind_interface")
# Stale on the restored device: removed so they can't mix with the backup.
_DROP_ON_RESTORE = ("transfers.json", "pairing_tokens.json", "shares.json")


class BackupError(ValueError):
    pass


def _key(password: str, salt: bytes, log_n: int, r: int, p: int) -> bytes:
    if not (14 <= log_n <= 20 and 1 <= r <= 16 and 1 <= p <= 4):
        raise BackupError("unsupported backup settings")
    return Scrypt(salt=salt, length=32, n=1 << log_n, r=r, p=p).derive(password.encode())


def create_backup(root: Path, password: str, *, app_version: str = "") -> bytes:
    """Seal this device's data into one backup file (bytes)."""
    if len(password) < MIN_PASSWORD:
        raise BackupError(f"use a password of at least {MIN_PASSWORD} characters")
    files: dict[str, str] = {}
    for name in BACKUP_FILES:
        path = root / name
        if path.is_file():
            files[name] = vault.read_text(path)
    if "identities/identity_tcp.json" not in files:
        raise BackupError("nothing to back up yet")
    payload = json.dumps(
        {"version": 1, "created": int(time.time()), "app": app_version, "files": files},
        separators=(",", ":"),
    ).encode()
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    header = MAGIC + salt + bytes((SCRYPT_LOG_N, SCRYPT_R, SCRYPT_P))
    key = _key(password, salt, SCRYPT_LOG_N, SCRYPT_R, SCRYPT_P)
    sealed = AESGCM(key).encrypt(nonce, zstd.ZstdCompressor(level=6).compress(payload), header)
    return header + nonce + sealed


def open_backup(blob: bytes, password: str) -> dict[str, Any]:
    """Check the password and return the backup's content. Raises BackupError."""
    if len(blob) > MAX_BACKUP or not blob.startswith(MAGIC):
        raise BackupError("this is not an SRLTCP backup file")
    head = len(MAGIC)
    if len(blob) < head + 16 + 3 + 12 + 16:
        raise BackupError("the backup file is incomplete")
    salt = blob[head:head + 16]
    log_n, r, p = blob[head + 16:head + 19]
    header = blob[:head + 19]
    nonce, sealed = blob[head + 19:head + 31], blob[head + 31:]
    try:
        packed = AESGCM(_key(password, salt, log_n, r, p)).decrypt(nonce, sealed, header)
    except InvalidTag as exc:
        raise BackupError("wrong password, or the backup file is damaged") from exc
    try:
        data = json.loads(
            zstd.ZstdDecompressor().decompress(packed, max_output_size=4 * MAX_BACKUP)
        )
    except (zstd.ZstdError, ValueError) as exc:
        raise BackupError("the backup file is damaged") from exc
    if not isinstance(data, dict):
        raise BackupError("the backup file is damaged")
    files = data.get("files")
    if (
        data.get("version") != 1
        or not isinstance(files, dict)
        or "identities/identity_tcp.json" not in files
        or not all(name in BACKUP_FILES and isinstance(text, str) for name, text in files.items())
    ):
        raise BackupError("the backup file is not one this version can restore")
    try:
        for text in files.values():
            json.loads(text)  # each must be valid JSON before anything is replaced
    except ValueError as exc:
        raise BackupError("the backup file is damaged") from exc
    return {str(k): v for k, v in data.items()}


def summary(data: dict[str, Any]) -> dict[str, Any]:
    """What a backup holds, for the confirmation dialog."""
    files = data["files"]
    ident = json.loads(files["identities/identity_tcp.json"])
    peers = json.loads(files.get("trusted.json", "{}")).get("peers", [])
    messages = json.loads(files.get("messages.json", "{}")).get("messages", [])
    return {
        "name": str(ident.get("name", "")),
        "hash_id": str(ident.get("hash_id", "")),
        "contacts": len(peers),
        "messages": len(messages),
        "created": data.get("created"),
    }


def restore_backup(root: Path, data: dict[str, Any]) -> None:
    """Replace this device's data with the backup's (app stopped first)."""
    files: dict[str, str] = dict(data["files"])
    if "settings.json" in files:
        settings = json.loads(files["settings.json"])
        for key in _DEVICE_SETTINGS:
            settings.pop(key, None)
        settings["setup_complete"] = True
        files["settings.json"] = json.dumps(settings)
    for name in _DROP_ON_RESTORE:
        (root / name).unlink(missing_ok=True)
    for name in BACKUP_FILES:
        target = root / name
        if name in files:
            vault.write_text(target, files[name])
        else:
            target.unlink(missing_ok=True)
