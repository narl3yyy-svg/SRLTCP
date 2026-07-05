"""HTTP route registration."""

from srltcp.routes.api import register_api_routes
from srltcp.routes.share import register_share_routes
from srltcp.routes.ws import register_ws_routes

__all__ = ["register_api_routes", "register_share_routes", "register_ws_routes"]