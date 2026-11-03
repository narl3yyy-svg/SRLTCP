"""Put together what the desktop app runs, for one platform: a standalone
Python (python-build-standalone), SRLTCP and its packages, and Tor.

    python3.12 desktop/scripts/prepare_backend.py win-x64     # Windows 11, x64
    python3.12 desktop/scripts/prepare_backend.py mac-arm64   # macOS, Apple silicon
    python3.12 desktop/scripts/prepare_backend.py linux-x64   # (for testing)

The result goes to desktop/backend/<target>/ (python/, tor/), which
electron-builder copies into the app. Everything downloaded is checked
against a pinned (Python) or published (Tor) SHA-256. Packages are wheels
built for the target ("--platform"), so this runs on any computer.
"""

from __future__ import annotations

import compileall
import hashlib
import io
import shutil
import subprocess
import sys
import tarfile
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DESKTOP = ROOT / "desktop"
CACHE = DESKTOP / ".cache"

PBS = "https://github.com/astral-sh/python-build-standalone/releases/download/20261026"
PYTHON = {
    "win-x64": ("cpython-3.12.16+20261026-x86_64-pc-windows-msvc-install_only_stripped.tar.gz",
                "1f2ab53880fc67ea323398c6388e05af28addc99a8e389209f08a51fdff4b149"),
    "mac-arm64": ("cpython-3.12.16+20261026-aarch64-apple-darwin-install_only_stripped.tar.gz",
                  "353d5f1b4fd2715e7053f12f068435d5bbad16b91e057006b5d01e0925709a7e"),
    "linux-x64": ("cpython-3.12.16+20261026-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz",
                  "ce4f6d0a6cfdb3bed6e44c2f9affd85943c76a42e25ad3a259002f31d6fb8a20"),
}
# pip --platform tags for each target (newest first is not needed: pip takes any).
WHEEL_PLATFORMS = {
    "win-x64": ["win_amd64"],
    "mac-arm64": [f"macosx_{v}_0_arm64" for v in (15, 14, 13, 12, 11)]
    + [f"macosx_{v}_0_universal2" for v in (11,)]
    + [f"macosx_10_{v}_universal2" for v in (9, 12, 13, 14, 15)],
    "linux-x64": [f"manylinux_2_{v}_x86_64" for v in (39, 34, 28, 27, 24, 17, 12, 5)]
    + ["manylinux2014_x86_64", "manylinux2010_x86_64", "manylinux1_x86_64"],
}
MARKER_ENV = {
    "win-x64": {"sys_platform": "win32", "platform_system": "Windows", "os_name": "nt"},
    "mac-arm64": {"sys_platform": "darwin", "platform_system": "Darwin", "os_name": "posix"},
    "linux-x64": {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix"},
}
TOR_PLATFORM = {"win-x64": "windows-x86_64", "mac-arm64": "macos-aarch64",
                "linux-x64": "linux-x86_64"}
# Parts of the standard library SRLTCP never uses (smaller, fewer files to sign).
STDLIB_DROP = ("test", "idlelib", "tkinter", "turtledemo", "ensurepip", "lib2to3",
               "pydoc_data", "turtle.py", "__phello__")


def fetch(url: str, sha256: str, name: str) -> bytes:
    CACHE.mkdir(parents=True, exist_ok=True)
    cached = CACHE / name
    if cached.exists() and hashlib.sha256(cached.read_bytes()).hexdigest() == sha256:
        return cached.read_bytes()
    print(f"  downloading {name}", flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": "SRLTCP-build"})
    with urllib.request.urlopen(req, timeout=300) as res:  # noqa: S310 - https, pinned hash
        data = res.read()
    if hashlib.sha256(data).hexdigest() != sha256:
        raise SystemExit(f"{name}: SHA-256 doesn't match the pinned one — not used")
    cached.write_bytes(data)
    return data


def site_packages(python_dir: Path, target: str) -> Path:
    if target.startswith("win"):
        return python_dir / "Lib" / "site-packages"
    return python_dir / "lib" / "python3.12" / "site-packages"


def stdlib(python_dir: Path, target: str) -> Path:
    return python_dir / "Lib" if target.startswith("win") else python_dir / "lib" / "python3.12"


def requirements(target: str) -> list[str]:
    from pip._vendor.packaging.requirements import Requirement

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    env = {"python_version": "3.12", "python_full_version": "3.12.16",
           "implementation_name": "cpython", "platform_python_implementation": "CPython",
           **MARKER_ENV[target]}
    out = []
    for spec in project["dependencies"]:
        req = Requirement(spec)
        if req.marker is None or req.marker.evaluate(env):
            out.append(f"{req.name}{req.specifier}")
    return out


def install_python(target: str, dest: Path) -> Path:
    name, sha = PYTHON[target]
    data = fetch(f"{PBS}/{name.replace('+', '%2B')}", sha, name)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(dest, filter="tar")  # (keeps the symlinks python/bin needs)
    python_dir = dest / "python"
    lib = stdlib(python_dir, target)
    for part in STDLIB_DROP:
        p = lib / part
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
    for extra in ("tcl", "share", "include"):
        if (python_dir / extra).is_dir():
            shutil.rmtree(python_dir / extra)
    lib_dir = python_dir / "lib"
    for pattern in ("libt[ck]l*", "tk*", "tcl*", "itcl*", "thread*", "pkgconfig"):
        for tk in lib_dir.glob(pattern):
            shutil.rmtree(tk) if tk.is_dir() else tk.unlink()
    for cfg in lib.glob("config-*"):
        shutil.rmtree(cfg)
    # pip and setuptools: the app installs nothing at run time.
    for pkg in ("pip", "setuptools", "_distutils_hack", "distutils-precedence.pth"):
        for p in lib.glob(f"site-packages/{pkg}*"):
            shutil.rmtree(p) if p.is_dir() else p.unlink()
    bin_dir = python_dir / "bin"
    if bin_dir.is_dir():  # (only python itself; the rest are scripts)
        for f in bin_dir.iterdir():
            if not f.name.startswith("python3") or f.name.endswith("-config"):
                f.unlink()
    for f in python_dir.glob("Scripts/*"):
        f.unlink()
    if target.startswith("win"):
        for f in (python_dir / "DLLs").glob("_tkinter*"):
            f.unlink()
        for f in (python_dir / "DLLs").glob("t[ck]*.dll"):
            f.unlink()
    else:
        for f in (lib / "lib-dynload").glob("_tkinter*"):
            f.unlink()
    return python_dir


def install_packages(target: str, python_dir: Path) -> None:
    site = site_packages(python_dir, target)
    site.mkdir(parents=True, exist_ok=True)
    reqs = requirements(target)
    print(f"  packages: {', '.join(reqs)}", flush=True)
    cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
           "--no-compile", "--no-cache-dir", "--target", str(site), "--only-binary=:all:",
           "--python-version", "3.12", "--implementation", "cp",
           "--abi", "cp312", "--abi", "abi3", "--abi", "none"]
    for plat in WHEEL_PLATFORMS[target]:
        cmd += ["--platform", plat]
    subprocess.run([*cmd, *reqs], check=True)
    # SRLTCP itself (no tests, no caches).
    shutil.copytree(ROOT / "srltcp", site / "srltcp",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
    for junk in site.glob("*.dist-info/RECORD"):
        junk.unlink()  # (lists files and hashes of the build machine's install)
    shutil.rmtree(site / "bin", ignore_errors=True)  # (console scripts with this machine's paths)
    for f in site.glob("zstandard/_cffi*"):
        f.unlink()  # (the C backend is used; the cffi one is a 12 MB spare)


def compile_bytecode(target: str, python_dir: Path) -> None:
    """Bytecode, made now: the app never writes into itself (python -B), and
    unchecked-hash .pyc files stay valid whatever the files' dates."""
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("Run this with Python 3.12 (the bytecode must match the bundled one)")
    ok = compileall.compile_dir(
        str(stdlib(python_dir, target)), quiet=1, workers=0,
        invalidation_mode=compileall.py_compile.PycInvalidationMode.UNCHECKED_HASH,
    )
    if not ok:
        print("  (some files didn't compile: they're compiled when used)", flush=True)


def install_tor(target: str, dest: Path) -> None:
    sys.path.insert(0, str(ROOT))
    from srltcp.utils import tor_setup

    tor_dir = dest / "tor"
    exe = tor_setup.download(tor_dir, TOR_PLATFORM[target])
    # Only Tor itself (and its libraries, GeoIP data): no bridges' helper
    # programs (pluggable transports), docs or debug builds.
    for extra in ("docs", "debug"):
        shutil.rmtree(tor_dir / extra, ignore_errors=True)
    shutil.rmtree(tor_dir / "tor" / "pluggable_transports", ignore_errors=True)
    print(f"  Tor: {exe.relative_to(dest)}", flush=True)


def main(argv: list[str]) -> int:
    targets = argv or ["win-x64", "mac-arm64"]
    for target in targets:
        if target not in PYTHON:
            raise SystemExit(f"unknown target {target} (one of {', '.join(PYTHON)})")
        dest = DESKTOP / "backend" / target
        print(f"[{target}] → {dest}", flush=True)
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        python_dir = install_python(target, dest)
        install_packages(target, python_dir)
        compile_bytecode(target, python_dir)
        install_tor(target, dest)
        size = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file() and not f.is_symlink())
        print(f"[{target}] ready ({size / 1e6:.0f} MB)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
