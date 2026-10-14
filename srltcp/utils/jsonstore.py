"""Small JSON files in the data folder: owner-only, written atomically."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any

from srltcp.utils.logging import get_logger

log = get_logger(__name__)


def load_json(path: Path, default: Any = None) -> Any:
    """The file's content, or ``default`` if it is missing or unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        log.warning("Could not read %s (%s)", path.name, exc)
        return default


def save_json(path: Path, data: Any, *, indent: int | None = None) -> bool:
    """Replace the file in one step (a crash leaves the old or the new one)."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, separators=None if indent else (",", ":"))
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("Could not save %s: %s", path.name, exc)
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        return False
    return True
