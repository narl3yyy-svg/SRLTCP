"""Contact cards for in-person pairing (QR code, share link, NFC tap).

A card is a signed, self-describing contact::

    srltcp://pair#<base64url(JSON)>

    {"v": 1, "h": <hash_id>, "k": <ed25519 pubkey, b64url>, "n": <name>,
     "a": [[host, port], ...], "t": <one-time pairing token?>, "ts": <unix>,
     "s": <ed25519 signature over all other fields, b64url>}

Scanning a card trusts its owner (the hash is derived from the key and the
card is signed, so nothing in it can be altered). If the card carries a
one-time token, the scanner sends it back inside the encrypted session; the
card owner then trusts the scanner too, so a single scan pairs both sides.
"""

from __future__ import annotations

import base64
import json
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from srltcp.core.protocol.crypto import identity_hash, load_public_key, verify_bytes
from srltcp.core.protocol.messages import MessageType, decode_payload, encode_payload
from srltcp.core.trusted import TrustedPeer
from srltcp.utils.logging import get_logger

if TYPE_CHECKING:
    from srltcp.core.messaging.backend import MessagingBackend
    from srltcp.core.messaging.links import PeerLink

log = get_logger(__name__)

CARD_PREFIX = "srltcp://pair#"
PAIRING_TOKEN_TTL = 15 * 60.0
MAX_CARD_LEN = 4096


class PairingError(ValueError):
    pass


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _canonical(fields: dict[str, Any]) -> bytes:
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


@dataclass
class ContactCard:
    hash_id: str
    public_key: bytes
    name: str
    endpoints: list[tuple[str, int]]
    token: str = ""
    timestamp: int = 0

    @property
    def public_key_hex(self) -> str:
        return self.public_key.hex()

    def to_dict(self) -> dict[str, Any]:
        return {
            "hash_id": self.hash_id,
            "public_key": self.public_key_hex,
            "name": self.name,
            "endpoints": [{"host": h, "port": p} for h, p in self.endpoints],
            "has_token": bool(self.token),
            "timestamp": self.timestamp,
        }


def encode_card(
    *,
    private_key: Any,
    public_key: bytes,
    name: str,
    endpoints: list[tuple[str, int]],
    token: str = "",
) -> str:
    fields: dict[str, Any] = {
        "v": 1,
        "h": identity_hash(public_key),
        "k": _b64e(public_key),
        "n": name[:64],
        "a": [[h, int(p)] for h, p in endpoints if h][:4],
        "ts": int(time.time()),
    }
    if token:
        fields["t"] = token
    fields["s"] = _b64e(private_key.sign(_canonical(fields)))
    return CARD_PREFIX + _b64e(_canonical(fields))


def decode_card(text: str) -> ContactCard:
    """Parse and verify a card. Raises PairingError when anything is off."""
    text = (text or "").strip()
    if len(text) > MAX_CARD_LEN:
        raise PairingError("pairing code is too long")
    if text.startswith(CARD_PREFIX):
        text = text[len(CARD_PREFIX) :]
    elif "#" in text and text.split("#", 1)[0].lower() in ("srltcp://pair", "srltcp:pair"):
        text = text.split("#", 1)[1]
    try:
        fields = json.loads(_b64d(text))
    except (ValueError, UnicodeDecodeError) as exc:
        raise PairingError("not a valid SRLTCP pairing code") from exc
    if not isinstance(fields, dict) or fields.get("v") != 1:
        raise PairingError("unsupported pairing code version")
    try:
        sig = _b64d(str(fields.pop("s")))
        pub = _b64d(str(fields["k"]))
        hash_id = str(fields["h"]).lower()
        name = str(fields.get("n", ""))[:64]
        raw_eps = fields.get("a", [])
        timestamp = int(fields.get("ts", 0))
        token = str(fields.get("t", ""))[:64]
    except (KeyError, ValueError, TypeError) as exc:
        raise PairingError("pairing code is missing fields") from exc
    if len(pub) != 32 or identity_hash(pub) != hash_id:
        raise PairingError("pairing code identity does not match its key")
    if not verify_bytes(load_public_key(pub), sig, _canonical(fields)):
        raise PairingError("pairing code signature is invalid")
    endpoints: list[tuple[str, int]] = []
    if isinstance(raw_eps, list):
        for ep in raw_eps[:4]:
            try:
                host, port = str(ep[0])[:255], int(ep[1])
            except (TypeError, ValueError, IndexError):
                continue
            if host and 1 <= port <= 65535:
                endpoints.append((host, port))
    return ContactCard(
        hash_id=hash_id,
        public_key=pub,
        name=name,
        endpoints=endpoints,
        token=token,
        timestamp=timestamp,
    )


class PairingMixin:
    """Create/import contact cards and complete mutual pairing."""

    def _init_pairing(self: MessagingBackend) -> None:
        self._pairing_tokens: dict[str, float] = {}
        self._outgoing_pair_tokens: dict[str, str] = {}

    def _card_endpoints(self: MessagingBackend) -> list[tuple[str, int]]:
        """Every address we can be reached on — VPN first, then LAN/mobile."""
        from srltcp.utils.network import is_vpn_interface, list_interfaces

        if not self.tcp_transport:
            return []
        port = self.config.tcp_port
        ifaces = sorted(list_interfaces(), key=lambda i: 0 if is_vpn_interface(i) else 1)
        ips: list[str] = []
        if self.config.lan_ip:
            ips.append(self.config.lan_ip)
        for iface in ifaces:
            ip = str(iface.get("ip", ""))
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
        return [(ip, port) for ip in ips[:4]]

    def create_pairing_card(self: MessagingBackend, *, one_time: bool = True) -> dict[str, Any]:
        identity = self.identities.get("tcp") or next(iter(self.identities.values()), None)
        if identity is None:
            raise PairingError("no identity available")
        token = ""
        now = time.time()
        self._pairing_tokens = {t: exp for t, exp in self._pairing_tokens.items() if exp > now}
        if one_time:
            token = secrets.token_urlsafe(16)
            self._pairing_tokens[token] = now + PAIRING_TOKEN_TTL
        uri = encode_card(
            private_key=identity.private_key,
            public_key=identity.public_bytes(),
            name=identity.name,
            endpoints=self._card_endpoints(),
            token=token,
        )
        return {
            "uri": uri,
            "hash_id": identity.hash_id,
            "name": identity.name,
            "expires": now + PAIRING_TOKEN_TTL if token else None,
        }

    def _trust_from_card(self: MessagingBackend, card: ContactCard) -> TrustedPeer:
        if card.hash_id in self._own_hash_ids():
            raise PairingError("that is your own pairing code")
        existing = self.trusted.get(card.hash_id)
        host, port = card.endpoints[0] if card.endpoints else ("", 7825)
        alts = [h for h, _p in card.endpoints[1:]]
        if existing:
            if existing.public_key and existing.public_key != card.public_key_hex:
                raise PairingError("contact key changed — remove the old contact first")
            self.trusted.update(
                card.hash_id,
                name=card.name or None,
                blocked=False,
                tcp_host=host or None,
                tcp_port=port if host else None,
                alt_hosts=alts if host else None,
            )
            self.trusted.set_public_key(card.hash_id, card.public_key_hex)
            peer = self.trusted.get(card.hash_id)
            assert peer is not None
            return peer
        return self.trusted.add(
            TrustedPeer(
                hash_id=card.hash_id,
                name=card.name or f"Peer {card.hash_id[:8]}",
                transport="tcp",
                public_key=card.public_key_hex,
                tcp_host=host,
                tcp_port=port,
                alt_hosts=alts,
            )
        )

    async def import_pairing_card(
        self: MessagingBackend, text: str, *, connect: bool = True
    ) -> dict[str, Any]:
        card = decode_card(text)
        peer = self._trust_from_card(card)
        self.discovery.remove(card.hash_id)
        if card.token:
            self._outgoing_pair_tokens[card.hash_id] = card.token
        log.info("Paired with %s (%s) from contact card", peer.name, card.hash_id[:8])
        if connect:
            link = self.get_link(card.hash_id)
            if link and link.handshake_complete:
                await self._send_pending_pair(card.hash_id)
            else:
                self._kick_connect(card.hash_id)
        return peer.to_dict()

    async def _send_pending_pair(self: MessagingBackend, hash_id: str) -> None:
        token = self._outgoing_pair_tokens.get(hash_id)
        link = self.get_link(hash_id)
        if not token or not link or not link.handshake_complete:
            return
        own_card = self.create_pairing_card(one_time=False)["uri"]
        body = encode_payload({"token": token, "card": own_card})
        try:
            packet = self._seal(link, MessageType.PAIR, body)
            await self._send_raw(link.transport_peer_id, link.transport, packet)
            self._outgoing_pair_tokens.pop(hash_id, None)
        except (KeyError, OSError, RuntimeError, ConnectionError) as exc:
            log.debug("Pair confirmation to %s not sent yet: %s", hash_id[:8], exc)

    async def _handle_pair(self: MessagingBackend, link: PeerLink, body: bytes) -> None:
        data = decode_payload(body)
        token = str(data.get("token", ""))
        expiry = self._pairing_tokens.get(token)
        if not token or expiry is None or expiry < time.time():
            log.warning("Pair request from %s with unknown/expired token", link.hash_id[:8])
            return
        try:
            card = decode_card(str(data.get("card", "")))
        except PairingError as exc:
            log.warning("Pair request from %s had a bad card: %s", link.hash_id[:8], exc)
            return
        if card.hash_id != link.hash_id:
            log.warning("Pair card identity does not match session peer %s", link.hash_id[:8])
            return
        del self._pairing_tokens[token]  # single use
        try:
            peer = self._trust_from_card(card)
        except PairingError as exc:
            log.warning("Pairing with %s refused: %s", link.hash_id[:8], exc)
            return
        self.discovery.remove(card.hash_id)
        self._contact_requests.pop(card.hash_id, None)
        log.info("Mutual pairing completed with %s (%s)", peer.name, card.hash_id[:8])
        if self._on_event:
            await self._on_event(
                {"kind": "paired", "hash_id": peer.hash_id, "name": peer.name}
            )
        await self.flush_outbox(card.hash_id)
