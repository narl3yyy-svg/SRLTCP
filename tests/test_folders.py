"""Folder picker browse/validate tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from srltcp.utils.folders import _is_under_roots, list_directory, validate_folder


def test_is_under_roots_accepts_child_paths(tmp_path: Path) -> None:
    root = tmp_path / "home"
    child = root / "Desktop" / "cha"
    child.mkdir(parents=True)
    assert _is_under_roots(child, [root])


def test_is_under_roots_rejects_outside_paths(tmp_path: Path) -> None:
    root = tmp_path / "home"
    root.mkdir()
    outside = tmp_path / "other"
    outside.mkdir()
    assert not _is_under_roots(outside, [root])


def test_list_directory_navigates_into_child(tmp_path: Path) -> None:
    root = tmp_path / "home"
    desktop = root / "Desktop"
    desktop.mkdir(parents=True)
    (desktop / "cha").mkdir()

    with patch("srltcp.utils.folders._browse_roots", return_value=[root]):
        listing = list_directory(str(desktop))

    assert listing["path"] == str(desktop.resolve())
    names = {e["name"] for e in listing["entries"]}
    assert "cha" in names


def test_validate_folder_accepts_child(tmp_path: Path) -> None:
    root = tmp_path / "home"
    target = root / "incoming"
    target.mkdir(parents=True)

    with patch("srltcp.utils.folders._browse_roots", return_value=[root]):
        validated = validate_folder(str(target))

    assert validated == target.resolve()