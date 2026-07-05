"""List SRLTCP data paths to remove (used by uninstall.sh / uninstall.bat)."""

from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path


def data_dir() -> Path:
    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA", "")
        base = Path(appdata) / "SRLTCP" if appdata else Path.home() / "SRLTCP"
    else:
        base = Path.home() / ".srltcp"
    return base.resolve()


def extra_paths(data_root: Path) -> list[Path]:
    """Custom incoming/shared dirs outside the default data root."""
    settings_path = data_root / "settings.json"
    if not settings_path.is_file():
        return []
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    extras: list[Path] = []
    for key in ("incoming_files_dir", "shared_folder"):
        raw = (data.get(key) or "").strip()
        if not raw:
            continue
        path = Path(raw).expanduser().resolve()
        try:
            path.relative_to(data_root)
        except ValueError:
            extras.append(path)

    seen: set[str] = set()
    unique: list[Path] = []
    for path in extras:
        text = str(path)
        if text not in seen:
            seen.add(text)
            unique.append(path)
    return unique


def main() -> int:
    root = data_dir()
    if len(sys.argv) > 1 and sys.argv[1] == "--data-dir":
        print(root)
        return 0
    for path in extra_paths(root):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())