"""Get Tor onto a computer: run.bat (Windows) and run.sh (macOS, Linux) call
this before starting SRLTCP.

If Tor is on (the default) and no Tor program is found, it asks whether to
download the Tor Expert Bundle (the Tor program only, ~20 MB) from
torproject.org, built for this computer: Windows x86_64, macOS on Apple
silicon (arm64; Intel Macs get the x86_64 build), Linux x86_64 or arm64. The
newest stable bundle is downloaded over HTTPS, checked against the SHA-256
the Tor Project publishes next to it, and extracted into
``tor-expert-bundle`` in the SRLTCP folder, where SRLTCP finds it.

Usage: python -m srltcp.utils.tor_setup [--yes]
"""

from __future__ import annotations

import hashlib
import io
import json
import platform as _platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

DOWNLOAD_PAGE = "https://www.torproject.org/download/tor/"
DIST = "https://dist.torproject.org/torbrowser"
FOLDER = "tor-expert-bundle"
DECLINED = ".tor-download-declined"  # "no" once: not asked again (delete it to be asked)
TIMEOUT = 60
MAX_SIZE = 200 * 1024 * 1024


def app_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def _apple_silicon() -> bool:
    """An Apple silicon Mac, even when this Python runs as x86_64 under Rosetta
    (platform.machine() then says x86_64)."""
    if _platform.machine().lower() in ("arm64", "aarch64"):
        return True
    try:
        out = subprocess.run(["sysctl", "-n", "hw.optional.arm64"], capture_output=True,
                             text=True, timeout=5, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return out == "1"


def bundle_platform() -> str | None:
    """Which Tor Expert Bundle fits this computer (None: there's none)."""
    machine = _platform.machine().lower()
    if sys.platform == "win32":
        return "windows-x86_64" if machine in ("amd64", "x86_64", "arm64") else "windows-i686"
    if sys.platform == "darwin":
        return "macos-aarch64" if _apple_silicon() else "macos-x86_64"
    if sys.platform.startswith("linux"):
        if machine in ("x86_64", "amd64"):
            return "linux-x86_64"
        if machine in ("aarch64", "arm64"):
            return "linux-aarch64"
    return None


def exe_name() -> str:
    return "tor.exe" if sys.platform == "win32" else "tor"


def setup_command() -> str:
    return "run.bat tor-setup" if sys.platform == "win32" else "./run.sh tor-setup"


def newest_bundle(page: str, platform: str | None = None) -> tuple[str, str] | None:
    """(version, url) of the newest stable bundle for ``platform`` (default:
    this computer) on the page. Alphas (e.g. "16.0a14") are skipped."""
    platform = platform or bundle_platform()
    if not platform:
        return None
    pattern = re.compile(
        r"https://dist\.torproject\.org/torbrowser/(\d+(?:\.\d+)*)/"
        rf"tor-expert-bundle-{re.escape(platform)}-\1\.tar\.gz"
    )
    found = {m.group(1): m.group(0) for m in pattern.finditer(page)}
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


def extract(archive: bytes, dest: Path, exe: str | None = None) -> Path:
    """Unpack the bundle into ``dest`` (replacing it); returns the Tor
    program's path (``tor/tor.exe`` or ``tor/tor``). Only plain files and
    folders inside ``dest`` (no links, no ``..``)."""
    exe = exe or exe_name()
    tmp = Path(tempfile.mkdtemp(prefix="tor-bundle-", dir=dest.parent))
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            for member in tar.getmembers():
                target = (tmp / member.name).resolve()
                inside = target.is_relative_to(tmp.resolve())
                if not inside or not (member.isfile() or member.isdir()):
                    raise ValueError(f"unexpected entry in the archive: {member.name}")
            tar.extractall(tmp, filter="data")  # (and every entry checked above)
        program = tmp / "tor" / exe
        if not program.is_file():
            raise ValueError(f"the archive has no tor/{exe}")
        if exe == "tor":
            for f in (tmp / "tor").iterdir():  # (the program and its libraries)
                if f.is_file():
                    f.chmod(0o700)
        if dest.exists():
            shutil.rmtree(dest)
        tmp.rename(dest)
        if sys.platform == "darwin":
            # (urllib sets no quarantine flag, but in case the folder was copied
            # from a download: Gatekeeper would refuse to start the program.)
            subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(dest)],
                           capture_output=True, check=False)
        return dest / "tor" / exe
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


def download(dest: Path, platform: str | None = None) -> Path:
    platform = platform or bundle_platform()
    if not platform:
        raise RuntimeError(f"the Tor Project has no Tor Expert Bundle for {_platform.machine()}")
    page = _get(DOWNLOAD_PAGE).decode("utf-8", "replace")
    newest = newest_bundle(page, platform)
    if not newest:
        raise RuntimeError(f"couldn't find the Tor Expert Bundle ({platform}) on torproject.org")
    version, url = newest
    filename = url.rsplit("/", 1)[1]
    sums = _get(f"{DIST}/{version}/sha256sums-signed-build.txt").decode("utf-8", "replace")
    want = published_sha256(sums, filename)
    if not want:
        raise RuntimeError("torproject.org publishes no checksum for that bundle")
    print(f"[srltcp] Downloading Tor {version} (Tor Expert Bundle, {platform})…", flush=True)
    archive = _get(url)
    if hashlib.sha256(archive).hexdigest() != want:
        raise RuntimeError("the download doesn't match the Tor Project's checksum — not used")
    return extract(archive, dest, "tor.exe" if platform.startswith("windows") else "tor")


def _install_hint() -> str:
    if sys.platform == "darwin":
        return "         (Or install Tor yourself: brew install tor, or Tor Browser.)\n"
    if sys.platform.startswith("linux"):
        return ("         (Or install Tor yourself: sudo apt install tor, sudo dnf install tor,\n"
                "          sudo pacman -S tor…)\n")
    return ""


def interactive() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except (OSError, ValueError):
        return False


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
    forced = "--yes" in argv
    if _tor_binary("") and not forced:
        return 0
    if not forced and (not tor_wanted() or (root / DECLINED).exists()):
        return 0
    if not bundle_platform():
        print(f"[srltcp] Tor isn't installed and the Tor Project has no ready-made Tor for "
              f"this computer ({_platform.machine()}). Install it with your package manager "
              "to find contacts on other networks.", flush=True)
        return 0 if not forced else 1
    if not forced:
        if not interactive():
            return 0  # (no one to ask: started by a service, not a terminal)
        print(
            "[srltcp] Tor isn't installed. SRLTCP uses it only to find your contacts when\n"
            "         you're on different networks (messages never go over Tor).\n"
            f"         Download the Tor Expert Bundle for this computer ({bundle_platform()},\n"
            f"         ~20 MB) from torproject.org into {root / FOLDER} now?\n"
            + _install_hint()
            + "         [Y/n] ",
            end="", flush=True,
        )
        try:
            answer = input().strip().lower()
        except EOFError:
            answer = "n"
        if answer not in ("", "y", "yes"):
            (root / DECLINED).write_text("Delete this file to be asked again.\n", encoding="utf-8")
            print(f"[srltcp] Skipped. To get Tor later: {setup_command()}", flush=True)
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
