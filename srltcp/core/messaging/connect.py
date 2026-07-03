"""Connection and handshake mixin."""

from __future__ import annotations

from typing import TYPE_CHECKING

from srltcp.core.messaging.links import PeerLink
from srltcp.core.protocol.crypto import KeyExchange, load_public_key
from srltcp.core.protocol.messages import MessageType, build_header, decode_payload, encode_payload
from srltcp.utils.logging import get_logger

if TYPE_CHECKING:
    from srltcp.core.messaging.backend import MessagingBackend

log = get_logger(__name__)


class ConnectMixin:
    """Establish encrypted sessions with peers."""

    _pending_handshakes: dict[str, KeyExchange]

    def _init_connect(self: MessagingBackend) -> None:
        self._pending_handshakes = {}

    async def connect_to_peer(
        self: MessagingBackend,
        hash_id: str,
        *,
        host: str | None = None,
        port: int | None = None,
        transport: str = "tcp",
    ) -> bool:
        """Dial a peer by hash (uses discovery registry for address)."""
        discovered = self.discovery.get(hash_id)
        if not discovered and not host:
            log.warning("Peer %s not in discovery registry", hash_id[:8])
            return False

        target_host = host or (discovered.tcp_host if discovered else "")
        target_port = port or (discovered.tcp_port if discovered else self.config.tcp_port)

        if transport == "tcp" and self.tcp_transport:
            peer_id = await self.tcp_transport.connect(target_host, target_port)
            pub_hex = discovered.public_key if discovered else ""
            pub_bytes = bytes.fromhex(pub_hex) if pub_hex else b"\x00" * 32
            link = PeerLink(
                hash_id=hash_id,
                transport_peer_id=peer_id,
                transport="tcp",
                address=f"{target_host}:{target_port}",
                public_key=pub_bytes,
            )
            self.register_link(link)
            await self._initiate_handshake(hash_id)
            return True
        return False

    async def _initiate_handshake(self: MessagingBackend, hash_id: str) -> None:
        link = self.get_link(hash_id)
        if not link:
            return
        identity = self._identity_for_transport(link.transport)
        kx = KeyExchange(identity.private_key)
        self._pending_handshakes[hash_id] = kx
        body = encode_payload(
            {
                "hash_id": identity.hash_id,
                "name": identity.name,
                "public_key": identity.public_bytes().hex(),
                "ephemeral": kx.ephemeral_public.hex(),
                "signature": kx.sign_ephemeral().hex(),
            }
        )
        packet = build_header(MessageType.HANDSHAKE, body=body)
        await self._send_raw(link.transport_peer_id, link.transport, packet)

    async def _handle_handshake(
        self: MessagingBackend, peer_id: str, body: bytes, *, initiator: bool
    ) -> None:
        data = decode_payload(body)
        remote_hash = data["hash_id"]
        remote_pub = bytes.fromhex(data["public_key"])
        remote_eph = bytes.fromhex(data["ephemeral"])
        remote_sig = bytes.fromhex(data["signature"])

        link = self.get_link_by_peer_id(peer_id)
        if not link:
            link = PeerLink(
                hash_id=remote_hash,
                transport_peer_id=peer_id,
                transport="tcp",
                address="",
                public_key=remote_pub,
            )
            self.register_link(link)
        else:
            link.hash_id = remote_hash
            link.public_key = remote_pub
            self._peer_id_to_hash[peer_id] = remote_hash

        identity = self._identity_for_transport(link.transport)
        if remote_hash in self._pending_handshakes:
            kx = self._pending_handshakes.pop(remote_hash)
            keys = kx.complete(
                remote_eph,
                remote_sig,
                load_public_key(remote_pub),
                initiator=True,
            )
        else:
            kx = KeyExchange(identity.private_key)
            keys = kx.complete(
                remote_eph,
                remote_sig,
                load_public_key(remote_pub),
                initiator=False,
            )
            # Respond with our handshake ack
            ack_body = encode_payload(
                {
                    "hash_id": identity.hash_id,
                    "public_key": identity.public_bytes().hex(),
                    "ephemeral": kx.ephemeral_public.hex(),
                    "signature": kx.sign_ephemeral().hex(),
                }
            )
            ack = build_header(MessageType.HANDSHAKE_ACK, body=ack_body)
            await self._send_raw(peer_id, link.transport, ack)

        self.set_link_keys(remote_hash, keys)
        log.info("Handshake complete with %s (%s)", data.get("name"), remote_hash[:8])

        if self._on_link_up:
            await self._on_link_up(remote_hash, data.get("name", ""))

    async def _handle_handshake_ack(self: MessagingBackend, peer_id: str, body: bytes) -> None:
        data = decode_payload(body)
        remote_hash = data["hash_id"]
        remote_pub = bytes.fromhex(data["public_key"])
        remote_eph = bytes.fromhex(data["ephemeral"])
        remote_sig = bytes.fromhex(data["signature"])

        kx = self._pending_handshakes.pop(remote_hash, None)
        if not kx:
            link = self.get_link_by_peer_id(peer_id)
            if link:
                remote_hash = link.hash_id
                kx = self._pending_handshakes.pop(remote_hash, None)
        if not kx:
            log.warning("Unexpected handshake ack from %s", peer_id[:8])
            return

        keys = kx.complete(
            remote_eph,
            remote_sig,
            load_public_key(remote_pub),
            initiator=True,
        )
        self.set_link_keys(remote_hash, keys)
        log.info("Handshake ack from %s", remote_hash[:8])
        if self._on_link_up:
            await self._on_link_up(remote_hash, data.get("name", ""))