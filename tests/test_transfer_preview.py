"""Transfer preview API guards."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from srltcp.core.messaging.models import FileTransfer, TransferState
from srltcp.routes.api import register_api_routes


@pytest.mark.asyncio
async def test_transfer_file_blocks_inline_until_complete(tmp_path: Path) -> None:
    node = MagicMock()
    transfer = FileTransfer(
        id="tid123",
        sender_hash="a" * 32,
        recipient_hash="b" * 32,
        filename="clip.mp4",
        path=tmp_path / "clip.mp4",
        size=1024,
        sha256="",
        transport="tcp",
        state=TransferState.TRANSFERRING,
    )
    transfer.path.write_bytes(b"\x00" * 512)
    node.backend._transfers = {"tid123": transfer}
    app = web.Application()
    register_api_routes(app, node)

    request = MagicMock()
    request.match_info = {"transfer_id": "tid123"}
    request.rel_url.query = {}

    handler = None
    for route in app.router.routes():
        if getattr(route.resource, "canonical", "") == "/api/transfers/{transfer_id}/file":
            handler = route.handler
            break
    assert handler is not None

    response = await handler(request)
    assert response.status == 409

    request.rel_url.query = {"download": "1"}
    response_dl = await handler(request)
    assert response_dl.status == 200