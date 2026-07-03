"""aiohttp HTTPS web server for SRLTCP local UI (localhost only)."""

from __future__ import annotations

from pathlib import Path

from aiohttp import web

from srltcp.core.messaging.constants import WEB_PORT
from srltcp.core.node import SRLTCPNode
from srltcp.routes.api import register_api_routes
from srltcp.routes.share import register_share_routes
from srltcp.routes.ws import broadcast_event, register_ws_routes
from srltcp.utils.logging import get_logger
from srltcp.utils.ports import start_web_site
from srltcp.utils.tls import create_ssl_context
from srltcp.web.security import quiet_access_log, security_middleware

log = get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
STATIC_FILES = {
    "app.css": "text/css",
    "app.js": "application/javascript",
}


def create_app(node: SRLTCPNode) -> web.Application:
    app = web.Application(middlewares=[security_middleware, quiet_access_log])

    async def index(_request: web.Request) -> web.Response:
        index_path = STATIC_DIR / "index.html"
        if index_path.exists():
            return web.FileResponse(index_path)
        return web.Response(text="SRLTCP Web UI", content_type="text/html")

    async def static_file(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if name not in STATIC_FILES:
            raise web.HTTPNotFound()
        path = STATIC_DIR / name
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "no-store"})

    app.router.add_get("/", index)
    app.router.add_get("/static/{name}", static_file)

    register_api_routes(app, node)
    register_share_routes(app, node)
    register_ws_routes(app, node)

    async def on_message(data: dict) -> None:
        await broadcast_event(node, "message", data)

    async def on_peer(data: dict) -> None:
        await broadcast_event(node, "peer_discovered", data)

    async def on_link(hash_id: str, name: str) -> None:
        await broadcast_event(node, "link_up", {"hash_id": hash_id, "name": name})

    async def on_link_down(hash_id: str, name: str) -> None:
        await broadcast_event(node, "link_down", {"hash_id": hash_id, "name": name})

    async def on_metrics(hash_id: str, metrics: dict) -> None:
        await broadcast_event(
            node, "peer_metrics", {"hash_id": hash_id, **metrics}
        )

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
        on_link_down=on_link_down,
        on_peer_metrics=on_metrics,
        on_transfer_progress=on_progress,
        on_transfer_complete=on_complete,
        on_event=on_event,
    )

    return app


async def run_web_server(
    node: SRLTCPNode,
    host: str = "127.0.0.1",
    port: int = WEB_PORT,
) -> tuple[web.AppRunner, web.TCPSite, int]:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("SRLTCP web UI must bind to localhost only for security")

    ssl_ctx = create_ssl_context()
    app = create_app(node)
    runner = web.AppRunner(app, access_log_format='%a %t "%r" %s %b')
    await runner.setup()
    site, bound_port = await start_web_site(
        runner, host if host != "localhost" else "127.0.0.1", port, ssl_context=ssl_ctx
    )
    log.info("Web UI (HTTPS only): https://127.0.0.1:%d", bound_port)
    return runner, site, bound_port


async def shutdown_web_server(
    node: SRLTCPNode,
    runner: web.AppRunner,
    site: web.TCPSite,
) -> None:
    """Stop the HTTPS site, close clients, and tear down aiohttp."""
    await node.close_websockets()
    await site.stop()
    await runner.shutdown()
    await runner.cleanup()