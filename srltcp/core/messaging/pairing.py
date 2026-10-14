"""Contact cards for in-person pairing (QR code, share link, NFC tap).

A card is a signed, self-describing contact::

    srltcp://pair#<base64url(JSON)>

    {"v": 1, "h": <hash_id>, "k": <ed25519 pubkey, b64url>, "n": <name>,
     "q": <SHA-256 of the ML-DSA-65 pubkey, b64url>,
     "a": [[host, port], ...], "t": <one-time pairing token?>, "ts": <unix>,
     "s": <ed25519 signature over all other fields, b64url>}

Scanning a card trusts its owner (the hash is derived from the key and the
card is signed, so nothing in it can be altered). If the card carries a
one-time token, the scanner sends it back inside the encrypted session; the
card owner then trusts the scanner too, so a single scan pairs both sides.

Pairing in person also *verifies* the contact (see srltcp.core.verify): the
scanner got both key fingerprints straight from the other phone's screen
(or NFC), and the card owner knows that whoever redeemed the token saw its
screen, as long as the link was never shared (copied or sent).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from srltcp.core import verify
from srltcp.core.protocol.crypto import identity_hash, load_public_key, verify_bytes
from srltcp.core.protocol.messages import MessageType, decode_payload, encode_payload
from srltcp.core.trusted import TrustedPeer
from srltcp.utils.jsonstore import load_json, save_json
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
    pq_hash: bytes = b""  # SHA-256 of the ML-DSA key (empty on old cards)

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
    pq_hash: bytes = b"",
) -> str:
    fields: dict[str, Any] = {
        "v": 1,
        "h": identity_hash(public_key),
        "k": _b64e(public_key),
        "n": name[:64],
        "a": [[h, int(p)] for h, p in endpoints if h][:4],
        "ts": int(time.time()),
    }
    if pq_hash:
        fields["q"] = _b64e(pq_hash)
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
        pq_hash = _b64d(str(fields["q"])) if "q" in fields else b""
    except (KeyError, ValueError, TypeError) as exc:
        raise PairingError("pairing code is missing fields") from exc
    if len(pub) != 32 or identity_hash(pub) != hash_id:
        raise PairingError("pairing code identity does not match its key")
    if pq_hash and len(pq_hash) != 32:
        raise PairingError("pairing code has a bad post-quantum key")
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
        pq_hash=pq_hash,
    )


class PairingMixin:
    """Create/import contact cards and complete mutual pairing."""

    def _init_pairing(self: MessagingBackend) -> None:
        # sha256(token) -> expiry. Saved (owner-only) so a QR you showed still
        # pairs both ways if this app restarts before the scanner gets through.
        self._pairing_tokens: dict[str, float] = {}
        # Tokens whose link was copied or shared: redeeming them doesn't prove
        # the contact was in front of us, so it doesn't verify them.
        self._pairing_shared: set[str] = set()
        self._load_pairing_tokens()

    def _pairing_tokens_path(self: MessagingBackend) -> Path:
        return self.trusted.path.parent / "pairing_tokens.json"

    def _load_pairing_tokens(self: MessagingBackend) -> None:
        raw = load_json(self._pairing_tokens_path(), {})
        if not isinstance(raw, dict):
            return
        tokens = raw.get("tokens", raw)  # before 0.9: a flat {key: expiry}
        now = time.time()
        with contextlib.suppress(ValueError, TypeError, AttributeError):
            self._pairing_tokens = {
                str(k): float(v) for k, v in dict(tokens).items()
                if isinstance(v, (int, float)) and float(v) > now
            }
        shared = raw.get("shared", [])
        if isinstance(shared, list):
            self._pairing_shared = {str(k) for k in shared if str(k) in self._pairing_tokens}

    def _save_pairing_tokens(self: MessagingBackend) -> None:
        self._pairing_shared &= set(self._pairing_tokens)
        save_json(
            self._pairing_tokens_path(),
            {"tokens": self._pairing_tokens, "shared": sorted(self._pairing_shared)},
        )

    def mark_pairing_shared(self: MessagingBackend, uri: str) -> None:
        """The user copied or shared this code's link: it may reach anyone."""
        try:
            card = decode_card(uri)
        except PairingError:
            return
        key = self._token_key(card.token) if card.token else ""
        if key in self._pairing_tokens and key not in self._pairing_shared:
            self._pairing_shared.add(key)
            self._save_pairing_tokens()

    @staticmethod
    def _token_key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

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
            self._pairing_tokens[self._token_key(token)] = now + PAIRING_TOKEN_TTL
            self._save_pairing_tokens()
        uri = encode_card(
            private_key=identity.private_key,
            public_key=identity.public_bytes(),
            name=identity.name,
            endpoints=self._card_endpoints(),
            token=token,
            pq_hash=verify.pq_hash(identity.pq.public),
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
        expect = card.pq_hash.hex() if card.pq_hash else ""
        if existing:
            if existing.public_key and existing.public_key != card.public_key_hex:
                raise PairingError("contact key changed — remove the old contact first")
            if expect and existing.pq_key and (
                verify.pq_hash(bytes.fromhex(existing.pq_key)).hex() != expect
            ):
                raise PairingError(
                    "this code's post-quantum key doesn't match the one this contact used "
                    "before — remove the old contact first"
                )
            self.trusted.update(
                card.hash_id,
                name=card.name or None,
                blocked=False,
                tcp_host=host or None,
                tcp_port=port if host else None,
                alt_hosts=alts if host else None,
                pq_expect=expect if expect and not existing.pq_key else None,
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
                pq_expect=expect,
            )
        )

    async def import_pairing_card(
        self: MessagingBackend, text: str, *, connect: bool = True, in_person: bool = False
    ) -> dict[str, Any]:
        """Trust a card's owner. ``in_person``: scanned from their screen or
        read over NFC (not a pasted link), which verifies them too."""
        card = decode_card(text)
        peer = self._trust_from_card(card)
        if in_person and card.pq_hash:
            self._mark_verified(card.hash_id, card.public_key, card.pq_hash)
        self.discovery.remove(card.hash_id)
        if card.token:
            # Kept on disk until delivered: the scanned device may only be
            # reachable later (other network, app restarted in between).
            self.trusted.update(card.hash_id, pair_token_out=card.token)
        log.info("Paired with %s (%s) from contact card", peer.name, card.hash_id[:8])
        if connect:
            link = self.get_link(card.hash_id)
            if link and link.handshake_complete:
                await self._send_pending_pair(card.hash_id)
            else:
                self._kick_connect(card.hash_id)
        return self.trusted_view(card.hash_id) or peer.public_dict()

    async def _send_pending_pair(self: MessagingBackend, hash_id: str) -> None:
        peer = self.trusted.get(hash_id)
        token = peer.pair_token_out if peer else ""
        link = self.get_link(hash_id)
        if not token or not link or not link.handshake_complete:
            return
        own_card = self.create_pairing_card(one_time=False)["uri"]
        body = encode_payload({"token": token, "card": own_card})
        try:
            packet = self._seal(link, MessageType.PAIR, body)
            await self._send_raw(link.transport_peer_id, link.transport, packet)
            self.trusted.update(hash_id, pair_token_out="")
        except (KeyError, OSError, RuntimeError, ConnectionError) as exc:
            log.debug("Pair confirmation to %s not sent yet: %s", hash_id[:8], exc)

    async def _handle_pair(self: MessagingBackend, link: PeerLink, body: bytes) -> None:
        data = decode_payload(body)
        token = str(data.get("token", ""))
        expiry = self._pairing_tokens.get(self._token_key(token)) if token else None
        if not token or expiry is None or expiry < time.time():
            # Old or reused code: still a verified peer who added us, so ask the user.
            log.info("Pair request from %s with unknown/expired token", link.hash_id[:8])
            if not self.is_trusted(link.hash_id):
                await self._raise_contact_request(link)
            return
        try:
            card = decode_card(str(data.get("card", "")))
        except PairingError as exc:
            log.warning("Pair request from %s had a bad card: %s", link.hash_id[:8], exc)
            return
        if card.hash_id != link.hash_id:
            log.warning("Pair card identity does not match session peer %s", link.hash_id[:8])
            return
        key = self._token_key(token)
        in_person = key not in self._pairing_shared
        del self._pairing_tokens[key]  # single use
        self._save_pairing_tokens()
        try:
            peer = self._trust_from_card(card)
        except PairingError as exc:
            log.warning("Pairing with %s refused: %s", link.hash_id[:8], exc)
            return
        if not peer.pq_key and link.pq_public and self._pq_key_ok(card.hash_id, link.pq_public):
            # Not a contact when they connected, so their key wasn't pinned yet.
            peer = self.trusted.get(card.hash_id) or peer
        pinned = peer.pq_key and verify.pq_hash(bytes.fromhex(peer.pq_key)) == card.pq_hash
        if in_person and pinned:
            # Only someone who saw our screen (QR) or touched our phone (NFC)
            # had this token, and they proved their keys in this session.
            self._mark_verified(card.hash_id, card.public_key, card.pq_hash)
        self.discovery.remove(card.hash_id)
        self._contact_requests.pop(card.hash_id, None)
        log.info("Mutual pairing completed with %s (%s)", peer.name, card.hash_id[:8])
        if self._on_event:
            await self._on_event(
                {"kind": "paired", "hash_id": peer.hash_id, "name": peer.name}
            )
        await self.flush_outbox(card.hash_id)

    # ── Safety numbers (verified contacts) ──

    def _own_fingerprint(self: MessagingBackend) -> bytes | None:
        identity = self.identities.get("tcp") or next(iter(self.identities.values()), None)
        if identity is None:
            return None
        return verify.fingerprint(identity.public_bytes(), verify.pq_hash(identity.pq.public))

    def _safety_digest(
        self: MessagingBackend,
        hash_id: str,
        ed_public: bytes | None = None,
        pq_hash: bytes | None = None,
    ) -> bytes | None:
        """The pair's safety number, from the given keys or the contact's
        pinned ones; None until their post-quantum key is known."""
        peer = self.trusted.get(hash_id)
        if ed_public is None and peer and peer.public_key:
            ed_public = bytes.fromhex(peer.public_key)
        if pq_hash is None and peer:
            if peer.pq_key:
                pq_hash = verify.pq_hash(bytes.fromhex(peer.pq_key))
            elif peer.pq_expect:
                pq_hash = bytes.fromhex(peer.pq_expect)
        own = self._own_fingerprint()
        if own is None or not ed_public or not pq_hash:
            return None
        return verify.safety_digest(own, verify.fingerprint(ed_public, pq_hash))

    def _mark_verified(
        self: MessagingBackend, hash_id: str, ed_public: bytes, pq_hash: bytes
    ) -> None:
        digest = self._safety_digest(hash_id, ed_public, pq_hash)
        if digest is not None and self._safety_digest(hash_id) == digest:
            self.trusted.update(hash_id, verified=digest.hex())
            log.info("Verified %s (paired in person)", hash_id[:8])

    def contact_verified(self: MessagingBackend, hash_id: str) -> bool:
        peer = self.trusted.get(hash_id)
        if not peer or not peer.verified:
            return False
        digest = self._safety_digest(hash_id)
        return digest is not None and hmac.compare_digest(peer.verified, digest.hex())

    def trusted_view(self: MessagingBackend, hash_id: str) -> dict[str, Any] | None:
        peer = self.trusted.get(hash_id)
        if not peer:
            return None
        return {**peer.public_dict(), "verified": self.contact_verified(hash_id)}

    def safety_info(self: MessagingBackend, hash_id: str) -> dict[str, Any]:
        digest = self._safety_digest(hash_id)
        own = self.identities.get("tcp") or next(iter(self.identities.values()), None)
        if digest is None or own is None:
            return {"available": False, "verified": False}
        return {
            "available": True,
            "number": verify.safety_digits(digest),
            "code": verify.verify_code(own.hash_id, hash_id, digest),
            "verified": self.contact_verified(hash_id),
        }

    def verify_contact(
        self: MessagingBackend, hash_id: str, *, code: str = "", confirm: bool = False
    ) -> dict[str, Any]:
        """Mark a contact verified: their scanned verify code matches, or the
        user compared the numbers and confirms. Raises PairingError if not."""
        digest = self._safety_digest(hash_id)
        if digest is None:
            raise PairingError("connect with this contact once first")
        if code:
            parsed = verify.parse_verify_code(code)
            if parsed is None:
                raise PairingError("that is not an SRLTCP safety code")
            owner, contact, digits = parsed
            if owner != hash_id:
                raise PairingError("that safety code belongs to a different contact")
            if contact not in self._own_hash_ids():
                raise PairingError(
                    "that is their safety code for another contact — on their phone, "
                    "open the chat with you and show the code from there"
                )
            if not verify.digits_match(digest, digits):
                raise PairingError(
                    "the safety numbers don't match — your connection may not be private. "
                    "Pair again in person."
                )
        elif not confirm:
            raise PairingError("scan their code or confirm the numbers match")
        self.trusted.update(hash_id, verified=digest.hex())
        return self.safety_info(hash_id)

    def unverify_contact(self: MessagingBackend, hash_id: str) -> dict[str, Any]:
        self.trusted.update(hash_id, verified="")
        return self.safety_info(hash_id)
