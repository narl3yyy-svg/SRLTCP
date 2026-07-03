"""aiohttp web server for SRLTCP local UI."""

from __future__ import annotations

from pathlib import Path

from aiohttp import web

from srltcp.core.messaging.constants import WEB_PORT
from srltcp.core.node import SRLTCPNode
from srltcp.routes.api import register_api_routes
from srltcp.routes.share import register_share_routes
from srltcp.routes.ws import broadcast_event, register_ws_routes
from srltcp.utils.logging import get_logger

log = get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def create_app(node: SRLTCPNode) -> web.Application:
    app = web.Application()

    async def index(_request: web.Request) -> web.Response:
        index_path = STATIC_DIR / "index.html"
        if index_path.exists():
            return web.FileResponse(index_path)
        return web.Response(text="SRLTCP Web UI", content_type="text/html")

    app.router.add_get("/", index)
    if STATIC_DIR.exists():
        app.router.add_static("/static", STATIC_DIR)

    register_api_routes(app, node)
    register_share_routes(app, node)
    register_ws_routes(app, node)

    # Wire backend callbacks to WebSocket broadcast
    async def on_message(data: dict) -> None:
        await broadcast_event(node, "message", data)

    async def on_peer(data: dict) -> None:
        await broadcast_event(node, "peer_discovered", data)

    async def on_link(hash_id: str, name: str) -> None:
        await broadcast_event(node, "link_up", {"hash_id": hash_id, "name": name})

    async def on_progress(data: dict) -> None:
        await broadcast_event(node, "transfer_progress", data)

    async def on_complete(data: dict) -> None:
        await broadcast_event(node, "transfer_complete", data)

    async def on_event(data: dict) -> None:
        await broadcast_event(node, "transport_event", data)

    node.backend.set_callbacks(
        on_message=on_message,
        on_peer_discovered=on_peer,
        on_link_up=on_link,
        on_transfer_progress=on_progress,
        on_transfer_complete=on_complete,
        on_event=on_event,
    )

    return app


async def run_web_server(
    node: SRLTCPNode,
    host: str = "127.0.0.1",
    port: int = WEB_PORT,
) -> web.AppRunner:
    app = create_app(node)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("Web UI at http://%s:%d", host, port)
    return runner