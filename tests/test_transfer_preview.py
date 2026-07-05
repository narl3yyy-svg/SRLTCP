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
    tid = "aabbccddeeff0011"
    transfer = FileTransfer(
        id=tid,
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
    node.backend._transfers = {tid: transfer}
    app = web.Application()
    register_api_routes(app, node)

    request = MagicMock()
    request.match_info = {"transfer_id": tid}
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


@pytest.mark.asyncio
async def test_transfer_file_prefers_incoming_path(tmp_path: Path) -> None:
    node = MagicMock()
    incoming = tmp_path / "received.png"
    incoming.write_bytes(b"\x89PNG\r\n\x1a\n")
    tid = "aabbccddeeff0022"
    transfer = FileTransfer(
        id=tid,
        sender_hash="a" * 32,
        recipient_hash="b" * 32,
        filename="shot.png",
        path=tmp_path / "missing-on-path.png",
        size=incoming.stat().st_size,
        sha256="",
        transport="serial",
        state=TransferState.COMPLETE,
    )
    node.backend._transfers = {tid: transfer}
    node.backend._incoming_paths = {tid: incoming}
    app = web.Application()
    register_api_routes(app, node)

    request = MagicMock()
    request.match_info = {"transfer_id": tid}
    request.rel_url.query = {}

    handler = None
    for route in app.router.routes():
        if getattr(route.resource, "canonical", "") == "/api/transfers/{transfer_id}/file":
            handler = route.handler
            break
    assert handler is not None

    response = await handler(request)
    assert response.status == 200
    assert response._path == incoming
    assert response.content_type == "image/png"