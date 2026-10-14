"""The app's JSON files: encrypted at rest (see vault.py), owner-only,
replaced atomically."""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from typing import Any

from srltcp.utils import vault
from srltcp.utils.logging import get_logger

log = get_logger(__name__)


def load_json(path: Path, default: Any = None) -> Any:
    """The file's content, or ``default`` if it is missing or unreadable.

    A file that can't be decrypted is moved aside (never overwritten), so
    nothing is lost if the key comes back, e.g. after a restore."""
    try:
        return json.loads(vault.read_text(path))
    except FileNotFoundError:
        return default
    except vault.VaultError as exc:
        aside = path.with_name(f"{path.name}.unreadable-{int(time.time())}")
        log.error("%s — moved to %s", exc, aside.name)
        with contextlib.suppress(OSError):
            path.replace(aside)
        return default
    except (OSError, ValueError) as exc:
        log.warning("Could not read %s (%s)", path.name, exc)
        return default


def save_json(path: Path, data: Any, *, indent: int | None = None) -> bool:
    """Seal and replace the file in one step (a crash leaves the old or the new one)."""
    try:
        text = json.dumps(data, indent=indent, separators=None if indent else (",", ":"))
        vault.write_text(path, text)
    except OSError as exc:
        log.warning("Could not save %s: %s", path.name, exc)
        return False
    return True
