"""Pytest fixtures — keep unit tests out of ~/.srltcp."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SRLTCP_DATA_DIR", str(tmp_path))

@pytest.fixture(autouse=True)
def no_real_router(monkeypatch):
    """Tests never ask the machine's real router for a port mapping."""
    from srltcp.transports import portmap

    async def no_router(self, internal_port, lan):  # noqa: ANN001, ANN202
        return None

    monkeypatch.setattr(portmap.PortMapper, "_create", no_router)
