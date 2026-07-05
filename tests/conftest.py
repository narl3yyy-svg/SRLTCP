"""Pytest fixtures — keep unit tests out of ~/.srltcp."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SRLTCP_DATA_DIR", str(tmp_path))