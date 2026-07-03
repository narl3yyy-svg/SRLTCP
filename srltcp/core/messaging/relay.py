"""Headless relay server mixin — forwards E2EE envelopes without decrypting."""

from __future__ import annotations

from typing import TYPE_CHECKING

from srltcp.core.messaging.routing import RoutingTable
from srltcp.core.protocol.crypto import relay_unwrap, relay_wrap
from srltcp.core.protocol.messages import MessageType, build_header, decode_payload, encode_payload
from srltcp.utils.logging import get_logger

if TYPE_CHECKING:
    from srltcp.core.messaging.backend import MessagingBackend

log = get_logger(__name__)


class RelayMixin:
    """Relay mode: opaque envelope forwarding."""

    routing: RoutingTable

    def _init_relay(self: MessagingBackend) -> None:
        self.routing = RoutingTable()

    async def forward_envelope(
        self: MessagingBackend,
        dest_hash: str,
        inner_payload: bytes,
        *,
        src_peer_id: str,
    ) -> bool:
        """Forward an E2EE payload without decrypting."""
        route = self.routing.get(dest_hash)
        if not route:
            log.debug("No route to %s", dest_hash[:8])
            return False
        wrapped = relay_wrap(inner_payload, dest_hash.encode("ascii"))
        body = wrapped
        packet = build_header(MessageType.RELAY_ENVELOPE, flags=0x08, body=body)
        await self._send_raw(route.next_hop_peer_id, route.transport, packet)
        return True

    async def _handle_relay_envelope(
        self: MessagingBackend, peer_id: str, body: bytes
    ) -> None:
        if not self.config.relay_mode:
            return
        dest_token, inner = relay_unwrap(body)
        dest_hash = dest_token.decode("ascii", errors="ignore").strip("\x00")

        # If we are the destination relay edge, deliver to local link
        local_identity = self.identities.get("tcp")
        if local_identity and dest_hash == local_identity.hash_id:
            # Pass through to local handler — still encrypted E2EE
            link = self.get_link(dest_hash)
            if link:
                await self._dispatch_encrypted(peer_id, inner)
            return

        # Otherwise forward
        await self.forward_envelope(dest_hash, inner, src_peer_id=peer_id)

    async def _handle_route_update(self: MessagingBackend, peer_id: str, body: bytes) -> None:
        if not self.config.relay_mode:
            return
        data = decode_payload(body)
        dest = data.get("dest_hash", "")
        hops = int(data.get("hops", 1))
        transport = data.get("transport", "tcp")
        if dest:
            self.routing.update(dest, peer_id, transport, hops)
            log.debug("Route update: %s via %s (%d hops)", dest[:8], peer_id[:8], hops)

    async def publish_route(self: MessagingBackend, dest_hash: str, peer_id: str) -> None:
        if not self.config.relay_mode:
            return
        body = encode_payload(
            {
                "dest_hash": dest_hash,
                "hops": 1,
                "transport": "tcp",
                "relay": self.config.name,
            }
        )
        packet = build_header(MessageType.ROUTE_UPDATE, body=body)
        if self.tcp_transport:
            await self.tcp_transport.broadcast(packet)