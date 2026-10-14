"""Encryption at rest for the app's own files (contacts, keys, chats, settings).

Each file is sealed with AES-256-GCM under one random *data key*; the file
name is bound in as associated data, so files can't be swapped for each
other. Where the data key lives decides what this protects against:

- **Android:** wrapped by a key in the Android Keystore (hardware-backed on
  most phones, never leaves it). A copy of the app's files, from a rooted
  phone, a forensic image or a careless backup, is unreadable anywhere else.
- **Windows:** wrapped with DPAPI, i.e. tied to your Windows account: the
  files are useless on another computer or another account.
- **Linux / macOS:** an owner-only key file in the data folder. That keeps
  the files away from other users and out of plain-text backups of single
  files, but not from someone who copies the whole folder; use full-disk
  encryption there.

Files written by older versions (plain JSON) are read as they are and
sealed the next time they are saved; :func:`seal_existing` does that at
startup for all of them.
"""

from __future__ import annotations

import os
import secrets
import threading
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from srltcp.utils.logging import get_logger
from srltcp.utils.platform import is_android

log = get_logger(__name__)

MAGIC = b"SRLV1\n"
_AAD = b"srltcp-vault-v1:"
_lock = threading.Lock()
_keys: dict[Path, bytes] = {}  # data folder -> data key


class VaultError(OSError):
    """A sealed file can't be opened (wrong device, damaged, key lost)."""


# ── The data key ──


def _key_for(folder: Path) -> bytes:
    with _lock:
        key = _keys.get(folder)
        if key is None:
            key = _load_or_create_key(folder)
            _keys[folder] = key
        return key


def _load_or_create_key(folder: Path) -> bytes:
    folder.mkdir(parents=True, exist_ok=True)
    if is_android():
        return _android_key(folder / "datakey.keystore")
    if os.name == "nt":
        return _windows_key(folder / "datakey.dpapi")
    return _file_key(folder / "datakey")


def _write_private(path: Path, raw: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _file_key(path: Path) -> bytes:
    if path.exists():
        raw = path.read_bytes()
        if len(raw) != 32:
            raise VaultError(f"{path.name} is damaged")
        return raw
    key = secrets.token_bytes(32)
    _write_private(path, key)
    return key


def _android_key(path: Path) -> bytes:
    from java import jclass

    vault = jclass("com.srltcp.app.KeyVault")
    if path.exists():
        try:
            return bytes(vault.unwrap(path.read_bytes()))
        except Exception as exc:  # noqa: BLE001 - Java exception
            raise VaultError(f"Android Keystore could not unlock the data key: {exc}") from exc
    key = secrets.token_bytes(32)
    _write_private(path, bytes(vault.wrap(key)))
    return key


def _windows_key(path: Path) -> bytes:
    if path.exists():
        return _dpapi(path.read_bytes(), protect=False)
    key = secrets.token_bytes(32)
    _write_private(path, _dpapi(key, protect=True))
    return key


def _dpapi(data: bytes, *, protect: bool) -> bytes:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out)):
        raise VaultError("Windows could not unlock the data key (DPAPI)")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


# ── Sealing files ──


def _folder_of(path: Path) -> Path:
    """The data folder a file belongs to (its key lives there): decided by
    where the file is, never by settings that could change in between."""
    folder = path.parent
    return folder.parent if folder.name == "identities" else folder


def seal(path: Path, plaintext: bytes) -> bytes:
    nonce = secrets.token_bytes(12)
    aes = AESGCM(_key_for(_folder_of(path)))
    return MAGIC + nonce + aes.encrypt(nonce, plaintext, _AAD + path.name.encode())


def unseal(path: Path, blob: bytes) -> bytes:
    if not blob.startswith(MAGIC):
        return blob  # written before encryption at rest
    body = blob[len(MAGIC):]
    if len(body) < 12 + 16:
        raise VaultError(f"{path.name} is damaged")
    aes = AESGCM(_key_for(_folder_of(path)))
    try:
        return aes.decrypt(body[:12], body[12:], _AAD + path.name.encode())
    except InvalidTag as exc:
        raise VaultError(
            f"{path.name} can't be decrypted on this device (damaged, or from another device)"
        ) from exc


def is_sealed(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def read_text(path: Path) -> str:
    """The file's text, decrypted. Raises FileNotFoundError / VaultError."""
    return unseal(path, path.read_bytes()).decode("utf-8")


def write_text(path: Path, text: str) -> None:
    """Seal and replace the file in one step (a crash leaves old or new)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_private(path, seal(path, text.encode("utf-8")))


def seal_existing(paths: list[Path]) -> int:
    """Encrypt files still in plain text (from before 0.9). Returns how many."""
    done = 0
    for path in paths:
        if not path.is_file() or is_sealed(path):
            continue
        try:
            write_text(path, path.read_text(encoding="utf-8"))
            done += 1
        except (OSError, UnicodeDecodeError) as exc:
            log.warning("Could not encrypt %s: %s", path.name, exc)
    return done


def forget_keys() -> None:
    """Drop cached keys (tests; after the data folder was replaced)."""
    with _lock:
        _keys.clear()
