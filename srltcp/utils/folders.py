"""Server-side folder browser for path pickers."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _safe_root() -> Path:
    return Path.home()


def list_directory(path: str | None = None, *, dirs_only: bool = False) -> dict[str, Any]:
    """List a directory for the folder picker UI."""
    root = _safe_root()
    target = Path(path).expanduser() if path else root
    try:
        target = target.resolve()
    except OSError:
        target = root.resolve()

    if not str(target).startswith(str(root.resolve())):
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
    root = _safe_root().resolve()
    if not str(resolved).startswith(str(root)):
        return None
    if not resolved.is_dir():
        return None
    return resolved