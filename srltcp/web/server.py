"""aiohttp HTTPS web server for SRLTCP local UI (localhost only)."""

from __future__ import annotations

from pathlib import Path

from aiohttp import web

from srltcp.core.messaging.constants import WEB_PORT
from srltcp.core.node import SRLTCPNode
from srltcp.routes.api import register_api_routes
from srltcp.routes.share import register_share_routes
from srltcp.routes.ws import broadcast_event, register_ws_routes
from srltcp.utils.android_notify import post_android_notification
from srltcp.utils.logging import get_logger
from srltcp.utils.ports import start_web_site
from srltcp.utils.tls import create_ssl_context
from srltcp.web.access_log import QuietAccessLogger
from srltcp.web.auth import auth_middleware, load_or_create_token
from srltcp.web.security import quiet_access_log, security_middleware

log = get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
STATIC_FILES = {
    "app.css": "text/css",
    "app.js": "application/javascript",
    "qrcode.js": "application/javascript",
    "calls.js": "application/javascript",
}


def create_app(node: SRLTCPNode, *, token: str | None = None) -> web.Application:
    middlewares = [security_middleware, quiet_access_log]
    if token:
        middlewares.insert(1, auth_middleware(token))
    app = web.Application(middlewares=middlewares)

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

    def notify_if_no_ui(title: str, body: str, tag: str) -> None:
        # With a UI connected, the page itself notifies (and knows if it is visible).
        if not node._ws_clients:
            post_android_notification(title, body, tag)

    def contact_name(hash_id: str) -> str:
        peer = node.backend.trusted.get(hash_id)
        return peer.name if peer else hash_id[:8]

    async def on_message(data: dict) -> None:
        await broadcast_event(node, "message", data)
        own = {i.hash_id for i in node.backend.identities.values()}
        sender = str(data.get("sender_hash", ""))
        if sender and sender not in own and data.get("status") == "delivered":
            kind = data.get("msg_type", "text")
            gid = str((data.get("metadata") or {}).get("group_id", ""))
            if gid:
                if data.get("_new"):
                    group = node.backend.groups.get(gid) or {}
                    who = node.backend.member_name(gid, sender)
                    notify_if_no_ui(
                        str(group.get("name", "Group")),
                        f"{who}: {str(data.get('text', ''))[:200]}",
                        f"group-{gid}",
                    )
                return
            text = str(data.get("text", ""))[:200] if kind == "text" else f"Sent a {kind}"
            notify_if_no_ui(contact_name(sender), text, f"msg-{sender}")

    async def on_peer(data: dict) -> None:
        await broadcast_event(node, "peer_discovered", data)

    async def on_link(hash_id: str, name: str) -> None:
        link = node.backend.get_link(hash_id)
        await broadcast_event(
            node,
            "link_up",
            {
                "hash_id": hash_id,
                "name": name,
                "transport": link.transport if link else "tcp",
                "address": link.address if link else "",
            },
        )

    async def on_link_down(hash_id: str, name: str) -> None:
        link = node.backend.get_link(hash_id)
        await broadcast_event(
            node,
            "link_down",
            {
                "hash_id": hash_id,
                "name": name,
                "transport": link.transport if link else "tcp",
            },
        )

    async def on_metrics(hash_id: str, metrics: dict) -> None:
        await broadcast_event(
            node, "peer_metrics", {"hash_id": hash_id, **metrics}
        )

    async def on_progress(data: dict) -> None:
        await broadcast_event(node, "transfer_progress", data)

    async def on_complete(data: dict) -> None:
        await broadcast_event(node, "transfer_complete", data)

    async def on_event(data: dict) -> None:
        kind = data.get("kind", "")
        hid = str(data.get("hash_id", ""))
        if kind == "call_signal" and (data.get("signal") or {}).get("type") == "offer":
            video = bool((data.get("signal") or {}).get("video"))
            notify_if_no_ui(
                contact_name(hid),
                "Incoming video call — open SRLTCP" if video else "Incoming call — open SRLTCP",
                f"call-{hid}",
            )
        elif kind == "contact_request":
            who = data.get("name") or hid[:8]
            notify_if_no_ui("Contact request", f"{who} wants to chat", f"req-{hid}")
        if kind in (
            "share_offer",
            "share_listing",
            "share_revoked",
            "contact_request",
            "paired",
            "call_signal",
            "group_update",
        ):
            await broadcast_event(node, kind, data)
        else:
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
    *,
    strict: bool = False,
) -> tuple[web.AppRunner, web.TCPSite, int]:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("SRLTCP web UI must bind to localhost only for security")

    ssl_ctx = create_ssl_context()
    app = create_app(node, token=load_or_create_token())
    runner = web.AppRunner(
        app,
        access_log_class=QuietAccessLogger,
        access_log_format='%a %t "%r" %s %b',
    )
    await runner.setup()
    site, bound_port = await start_web_site(
        runner,
        host if host != "localhost" else "127.0.0.1",
        port,
        ssl_context=ssl_ctx,
        strict=strict,
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