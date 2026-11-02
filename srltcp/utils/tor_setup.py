"""Get Tor onto a Windows PC: run.bat calls this before starting SRLTCP.

If Tor is on (the default) and no tor.exe is found, it asks whether to
download the Tor Expert Bundle (the Tor program only, ~23 MB) from
torproject.org. The newest stable bundle is downloaded over HTTPS, checked
against the SHA-256 the Tor Project publishes next to it, and extracted into
``tor-expert-bundle`` in the SRLTCP folder, where SRLTCP finds it.

Usage: python -m srltcp.utils.tor_setup [--yes]
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

DOWNLOAD_PAGE = "https://www.torproject.org/download/tor/"
DIST = "https://dist.torproject.org/torbrowser"
FOLDER = "tor-expert-bundle"
DECLINED = ".tor-download-declined"  # "no" once: not asked again (delete it to be asked)
# Stable bundles only (alphas look like "16.0a14").
_BUNDLE = re.compile(
    r"https://dist\.torproject\.org/torbrowser/(\d+(?:\.\d+)*)/"
    r"tor-expert-bundle-windows-x86_64-\1\.tar\.gz"
)
TIMEOUT = 60
MAX_SIZE = 200 * 1024 * 1024


def app_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def newest_bundle(page: str) -> tuple[str, str] | None:
    """(version, url) of the newest stable Windows x86_64 bundle on the page."""
    found = {m.group(1): m.group(0) for m in _BUNDLE.finditer(page)}
    if not found:
        return None
    version = max(found, key=lambda v: tuple(int(p) for p in v.split(".")))
    return version, found[version]


def published_sha256(sums: str, filename: str) -> str | None:
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == filename and re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            return parts[0]
    return None


def extract(archive: bytes, dest: Path) -> Path:
    """Unpack the bundle into ``dest`` (replacing it); returns tor.exe's path.
    Only plain files and folders inside ``dest`` (no links, no ``..``)."""
    tmp = Path(tempfile.mkdtemp(prefix="tor-bundle-", dir=dest.parent))
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            for member in tar.getmembers():
                target = (tmp / member.name).resolve()
                inside = target.is_relative_to(tmp.resolve())
                if not inside or not (member.isfile() or member.isdir()):
                    raise ValueError(f"unexpected entry in the archive: {member.name}")
            tar.extractall(tmp, filter="data")  # (and every entry checked above)
        exe = tmp / "tor" / "tor.exe"
        if not exe.is_file():
            raise ValueError("the archive has no tor\\tor.exe")
        if dest.exists():
            shutil.rmtree(dest)
        tmp.rename(dest)
        return dest / "tor" / "tor.exe"
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "SRLTCP"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as res:  # noqa: S310 - https only
        data = res.read(MAX_SIZE + 1)
    if len(data) > MAX_SIZE:
        raise ValueError("download too large")
    return bytes(data)


def download(dest: Path) -> Path:
    page = _get(DOWNLOAD_PAGE).decode("utf-8", "replace")
    newest = newest_bundle(page)
    if not newest:
        raise RuntimeError("couldn't find the Tor Expert Bundle on torproject.org")
    version, url = newest
    filename = url.rsplit("/", 1)[1]
    sums = _get(f"{DIST}/{version}/sha256sums-signed-build.txt").decode("utf-8", "replace")
    want = published_sha256(sums, filename)
    if not want:
        raise RuntimeError("torproject.org publishes no checksum for that bundle")
    print(f"[srltcp] Downloading Tor {version} (Tor Expert Bundle)…", flush=True)
    archive = _get(url)
    if hashlib.sha256(archive).hexdigest() != want:
        raise RuntimeError("the download doesn't match the Tor Project's checksum — not used")
    return extract(archive, dest)


def tor_wanted() -> bool:
    """Tor is on in the settings (the default)."""
    from srltcp.utils.platform import data_dir

    try:
        settings = json.loads((data_dir() / "settings.json").read_text(encoding="utf-8"))
        return bool(settings.get("tor_enabled", True))
    except (OSError, ValueError):
        return True


def main(argv: list[str]) -> int:
    import srltcp.core  # noqa: F401  (import order: core before its parts)
    from srltcp.core.messaging.tor import _tor_binary

    root = app_dir()
    if _tor_binary("") or not tor_wanted():
        return 0
    if (root / DECLINED).exists() and "--yes" not in argv:
        return 0
    if "--yes" not in argv:
        print(
            "[srltcp] Tor isn't installed. SRLTCP uses it only to find your contacts when\n"
            "         you're on different networks (messages never go over Tor).\n"
            "         Download the Tor Expert Bundle (~23 MB) from torproject.org into\n"
            f"         {root / FOLDER} now? [Y/n] ",
            end="", flush=True,
        )
        try:
            answer = input().strip().lower()
        except EOFError:
            answer = "n"
        if answer not in ("", "y", "yes"):
            (root / DECLINED).write_text("Delete this file to be asked again.\n", encoding="utf-8")
            print("[srltcp] Skipped. To get Tor later: run.bat tor-setup", flush=True)
            return 0
    try:
        exe = download(root / FOLDER)
    except Exception as exc:  # noqa: BLE001 - report and carry on without Tor
        print(f"[srltcp] Tor download failed: {exc}", flush=True)
        return 1
    (root / DECLINED).unlink(missing_ok=True)
    print(f"[srltcp] Tor installed: {exe}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
