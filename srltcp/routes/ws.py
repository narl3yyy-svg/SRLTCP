"""WebSocket event stream."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from srltcp.core.node import SRLTCPNode


def register_ws_routes(app: web.Application, node: SRLTCPNode) -> None:
    async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        node._ws_clients.add(ws)

        await ws.send_json({"type": "status", "data": node.status()})

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
    for ws in node._ws_clients:
        try:
            await ws.send_json({"type": event_type, "data": data})
        except Exception:
            dead.add(ws)
    node._ws_clients -= dead