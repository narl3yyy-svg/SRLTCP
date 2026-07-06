"""Server-side folder browser for path pickers."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from srltcp.core.settings import android_downloads_root
from srltcp.utils.platform import data_dir, is_android


def _browse_roots() -> list[Path]:
    """Directories the folder picker may list or validate under."""
    roots: list[Path] = []
    if is_android():
        downloads = android_downloads_root()
        if downloads:
            roots.append(downloads.resolve())
        roots.append(data_dir().resolve())
    roots.append(Path.home().resolve())
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def _safe_root() -> Path:
    roots = _browse_roots()
    return roots[0]


def _is_under_roots(path: Path, roots: list[Path] | None = None) -> bool:
    resolved = path.resolve()
    for root in roots or _browse_roots():
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def list_directory(path: str | None = None, *, dirs_only: bool = False) -> dict[str, Any]:
    """List a directory for the folder picker UI."""
    roots = _browse_roots()
    root = _safe_root()
    target = Path(path).expanduser() if path else root
    try:
        target = target.resolve()
    except OSError:
        target = root.resolve()

    if not _is_under_roots(target, roots):
        target = root.resolve()

    if not target.is_dir():
        target = target.parent if target.parent.is_dir() else root

    entries: list[dict[str, str]] = []
    try:
        for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if child.name.startswith("."):
                continue
            try:
                is_dir = child.is_dir()
                if dirs_only and not is_dir:
                    continue
                entries.append(
                    {
                        "name": child.name,
                        "path": str(child),
                        "type": "dir" if is_dir else "file",
                    }
                )
            except OSError:
                continue
    except PermissionError:
        entries = []

    parent = str(target.parent) if target != target.parent else str(target)
    return {
        "path": str(target),
        "parent": parent,
        "entries": entries[:200],
        "home": str(root),
    }


def validate_folder(path: str) -> Path | None:
    p = Path(path).expanduser()
    try:
        resolved = p.resolve()
    except OSError:
        return None
    if not _is_under_roots(resolved):
        return None
    if not resolved.is_dir():
        return None
    return resolved


def _deletable(path: Path, roots: list[Path] | None = None) -> bool:
    """Return True when a directory may be removed (never storage roots)."""
    resolved = path.resolve()
    for root in roots or _browse_roots():
        root_r = root.resolve()
        if resolved == root_r:
            return False
        try:
            rel = resolved.relative_to(root_r)
        except ValueError:
            continue
        if not rel.parts:
            return False
        if root_r.name.lower() in ("download", "downloads") and len(rel.parts) < 2:
            return False
        return True
    return False


def delete_directory(path: str) -> Path:
    """Delete a folder under allowed browse roots."""
    resolved = validate_folder(path)
    if not resolved:
        raise ValueError("invalid or inaccessible folder")
    if not _deletable(resolved):
        raise ValueError("folder cannot be deleted from this location")
    shutil.rmtree(resolved)
    return resolved