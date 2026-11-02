"""run.bat's Tor download (Windows): picking the bundle, checking it, unpacking it."""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest

import srltcp.core  # noqa: F401  (import order: core before its parts)
from srltcp.utils import tor_setup

BUNDLE = "tor-expert-bundle-windows-x86_64-16.0.tar.gz"


def _bundle(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_newest_stable_bundle_is_picked() -> None:
    page = " ".join(
        f'href="https://dist.torproject.org/torbrowser/{v}/tor-expert-bundle-windows-x86_64-{v}.tar.gz"'
        for v in ("15.0.9", "16.0", "16.0a14", "9.5.1")
    )
    assert tor_setup.newest_bundle(page) == (
        "16.0", "https://dist.torproject.org/torbrowser/16.0/tor-expert-bundle-windows-x86_64-16.0.tar.gz")
    assert tor_setup.newest_bundle("nothing here") is None


def test_checksum_line_is_found() -> None:
    sums = f"{'a' * 64}  other.tar.gz\n{'b' * 64}  {BUNDLE}\n"
    assert tor_setup.published_sha256(sums, BUNDLE) == "b" * 64
    assert tor_setup.published_sha256(sums, "missing.tar.gz") is None


def test_bundle_is_unpacked_into_its_folder(tmp_path: Path) -> None:
    dest = tmp_path / "tor-expert-bundle"
    exe = tor_setup.extract(_bundle({"tor/tor.exe": b"MZ", "data/geoip": b"x"}), dest)
    assert exe == dest / "tor" / "tor.exe" and exe.read_bytes() == b"MZ"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["tor-expert-bundle"]  # (no leftovers)


@pytest.mark.parametrize("bad", ["../evil.exe", "tor/../../evil.exe"])
def test_archive_escaping_its_folder_is_refused(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        tor_setup.extract(_bundle({"tor/tor.exe": b"MZ", bad: b"x"}), tmp_path / "bundle")
    assert not (tmp_path / "evil.exe").exists()


def test_download_refuses_a_bundle_not_matching_the_checksum(  # type: ignore[no-untyped-def]
    tmp_path: Path, monkeypatch
) -> None:
    archive = _bundle({"tor/tor.exe": b"MZ"})
    name = BUNDLE
    url = f"https://dist.torproject.org/torbrowser/16.0/{name}"
    pages = {
        tor_setup.DOWNLOAD_PAGE: f'<a href="{url}">'.encode(),
        f"{tor_setup.DIST}/16.0/sha256sums-signed-build.txt": f"{'0' * 64}  {name}\n".encode(),
        url: archive,
    }
    monkeypatch.setattr(tor_setup, "_get", lambda u: pages[u])
    with pytest.raises(RuntimeError, match="checksum"):
        tor_setup.download(tmp_path / "tor-expert-bundle")
    pages[f"{tor_setup.DIST}/16.0/sha256sums-signed-build.txt"] = (
        f"{hashlib.sha256(archive).hexdigest()}  {name}\n".encode())
    assert tor_setup.download(tmp_path / "tor-expert-bundle").is_file()


def test_asks_once_and_remembers_no(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import srltcp.core.messaging.tor as tor

    monkeypatch.setattr(tor_setup, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(tor, "_tor_binary", lambda _c="": None)
    monkeypatch.setattr(tor_setup, "tor_wanted", lambda: True)
    asked: list[int] = []
    monkeypatch.setattr("builtins.input", lambda: asked.append(1) or "n")
    assert tor_setup.main([]) == 0 and (tmp_path / tor_setup.DECLINED).exists()
    assert tor_setup.main([]) == 0 and len(asked) == 1  # (not asked again)
