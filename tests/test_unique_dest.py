"""Incoming file path deduplication tests."""

from __future__ import annotations

from pathlib import Path

from srltcp.utils.files import unique_dest_path


def test_unique_dest_path_uses_original_name(tmp_path: Path) -> None:
    dest = unique_dest_path(tmp_path, "photo.png")
    assert dest == tmp_path / "photo.png"


def test_unique_dest_path_adds_numeric_suffix_for_duplicates(tmp_path: Path) -> None:
    (tmp_path / "photo.png").write_bytes(b"x")
    assert unique_dest_path(tmp_path, "photo.png") == tmp_path / "photo1.png"
    (tmp_path / "photo1.png").write_bytes(b"y")
    assert unique_dest_path(tmp_path, "photo.png") == tmp_path / "photo2.png"


def test_unique_dest_path_folder_zip_collision(tmp_path: Path) -> None:
    (tmp_path / "temp.zip").write_bytes(b"x")
    assert unique_dest_path(tmp_path, "temp.zip") == tmp_path / "temp1.zip"
    (tmp_path / "temp1.zip").write_bytes(b"y")
    assert unique_dest_path(tmp_path, "temp.zip") == tmp_path / "temp2.zip"


def test_unique_dest_path_no_extension(tmp_path: Path) -> None:
    (tmp_path / "README").write_text("a")
    assert unique_dest_path(tmp_path, "README") == tmp_path / "README1"