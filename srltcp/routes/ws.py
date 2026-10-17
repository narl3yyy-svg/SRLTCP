"""WebSocket event stream."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from aiohttp import web

from srltcp.web.security import _is_allowed_origin

if TYPE_CHECKING:
    from srltcp.core.node import SRLTCPNode


def register_ws_routes(app: web.Application, node: SRLTCPNode) -> None:
    async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
        host = request.host.split(":")[0].lower()
        if host not in ("127.0.0.1", "localhost", "[::1]"):
            raise web.HTTPForbidden()
        origin = request.headers.get("Origin", "")
        if origin and not _is_allowed_origin(origin, request):
            raise web.HTTPForbidden(text="Invalid origin")
        ws = web.WebSocketResponse(heartbeat=30.0, max_msg_size=1024 * 1024)
        await ws.prepare(request)
        node._ws_clients.add(ws)

        await ws.send_json({"type": "status", "data": node.status()})
        # Opened from the ringing notification: the call is still waiting.
        for event in node.pending_call_events():
            await ws.send_json({"type": "call_signal", "data": event})

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        if data.get("type") == "ping":
                            await ws.send_json({"type": "pong"})
                    except json.JSONDecodeError:
                        pass
                elif msg.type == web.WSMsgType.ERROR:
                    break
        finally:
            node._ws_clients.discard(ws)
        return ws

    app.router.add_get("/ws", websocket_handler)


async def broadcast_event(node: SRLTCPNode, event_type: str, data: dict[str, Any]) -> None:
    """Push event to all connected WebSocket clients."""
    dead: set[Any] = set()
    for ws in list(node._ws_clients):  # the set changes if a socket closes mid-send
        try:
            await ws.send_json({"type": event_type, "data": data})
        except Exception:
            dead.add(ws)
    node._ws_clients -= dead