"""Trusted peer list — required before messaging."""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from srltcp.utils.platform import data_dir

_HASH_ID_RE = re.compile(r"^[0-9a-f]{32}$")
REMOVED_HISTORY_GRACE = 24 * 3600.0  # deleted contact's chat is kept this long

_GENERIC_NAMES = frozenset({"peer", "trusted", "serial-peer", "tcp-peer", "unknown"})
_FIXTURE_HASHES = frozenset(
    {
        "deadbeefdeadbeefdeadbeefdeadbeef",
        "abababababababababababababababab",
        "cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    }
)


def is_fixture_peer(peer: TrustedPeer) -> bool:
    """Detect pytest/dev fixture peers that leaked into the live data dir.

    A contact that ever completed a handshake has a pinned public key (and
    usually pair secrets); those are never fixtures, whatever their name —
    a real contact added before its name was known is still a contact."""
    if peer.public_key or peer.pair_own or peer.pair_peer:
        return False
    return peer.hash_id in _FIXTURE_HASHES or (
        peer.name.strip().lower() in _GENERIC_NAMES
        and peer.tcp_host in {"", "127.0.0.1", "10.0.0.5"}
    )


def is_valid_hash_id(hash_id: str) -> bool:
    return bool(hash_id and _HASH_ID_RE.fullmatch(hash_id.lower()))


@dataclass
class TrustedPeer:
    hash_id: str
    name: str
    transport: str = "tcp"
    public_key: str = ""
    tcp_host: str = ""
    tcp_port: int = 7825
    wan_host: str = ""
    wan_port: int = 7825
    wan_enabled: bool = False
    connection_mode: str = "auto"  # auto | lan | wan
    blocked: bool = False
    added_at: float = field(default_factory=time.time)
    # Other addresses from the contact card (e.g. Wi-Fi + WireGuard), tried in order.
    alt_hosts: list[str] = field(default_factory=list)
    # Internet relay (relay.py): our and their random contribution to the pair
    # secret, and their relay code. Secret material — never sent to the UI.
    pair_own: str = ""
    pair_peer: str = ""
    relay_code_peer: str = ""
    # One-time token from their QR/link that we still have to send back
    # (inside the encrypted session) so they add us too. Cleared once sent.
    pair_token_out: str = ""
    # Their onion address (Tor), learnt inside the encrypted session.
    onion: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def public_dict(self) -> dict[str, Any]:
        """What the local UI/API may see: no pair secrets or relay keys."""
        data = asdict(self)
        for key in ("pair_own", "pair_peer", "relay_code_peer", "pair_token_out"):
            data.pop(key, None)
        data["relay_ready"] = bool(self.pair_own and self.pair_peer)
        return data


class TrustedStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (data_dir() / "trusted.json")
        self._peers: dict[str, TrustedPeer] = {}
        # hash_id -> when the contact was deleted. Their chat history is kept
        # for REMOVED_HISTORY_GRACE (re-adding resumes it), then purged.
        self.removed: dict[str, float] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        removed = raw.get("removed", {})
        if isinstance(removed, dict):
            self.removed = {
                str(k).lower(): float(v)
                for k, v in removed.items()
                if is_valid_hash_id(str(k)) and isinstance(v, (int, float))
            }
        cleaned: dict[str, TrustedPeer] = {}
        for item in raw.get("peers", []):
            item = {k: v for k, v in item.items() if k in TrustedPeer.__dataclass_fields__}
            try:
                peer = TrustedPeer(**item)
            except TypeError:
                continue
            if not is_valid_hash_id(peer.hash_id):
                continue
            peer.hash_id = peer.hash_id.lower()
            if is_fixture_peer(peer):
                continue
            existing = cleaned.get(peer.hash_id)
            if not existing or peer.added_at >= existing.added_at:
                cleaned[peer.hash_id] = peer
        self._peers = cleaned
        if len(cleaned) != len(raw.get("peers", [])):
            self.save()

    def save(self) -> None:
        # Owner-only and atomic: the file holds the relay pair secrets.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {"peers": [p.to_dict() for p in self._peers.values()]}
        if self.removed:
            data["removed"] = self.removed
        tmp = self.path.with_suffix(".json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)

    def is_trusted(self, hash_id: str) -> bool:
        peer = self._peers.get(hash_id)
        return bool(peer and not peer.blocked)

    def add(self, peer: TrustedPeer) -> TrustedPeer:
        if not is_valid_hash_id(peer.hash_id):
            raise ValueError("invalid peer hash_id")
        peer.hash_id = peer.hash_id.lower()
        self.removed.pop(peer.hash_id, None)  # re-added: history resumes
        if peer.public_key:
            from srltcp.core.protocol.signed import verify_identity_binding

            peer.public_key = peer.public_key.strip().lower()
            if verify_identity_binding(peer.hash_id, peer.public_key) is None:
                raise ValueError("public_key does not match hash_id")
        if peer.name.strip().lower() in _GENERIC_NAMES:
            peer.name = peer.name.strip() or "Peer"
        self._peers[peer.hash_id] = peer
        self.save()
        return peer

    def take_expired_removals(self, now: float | None = None) -> list[str]:
        """Contacts deleted more than REMOVED_HISTORY_GRACE ago (and not added
        back): returned once, so their chat history can be purged for good."""
        now = time.time() if now is None else now
        expired = [
            h for h, ts in self.removed.items()
            if now - ts >= REMOVED_HISTORY_GRACE and h not in self._peers
        ]
        if expired:
            for h in expired:
                self.removed.pop(h, None)
            self.save()
        return expired

    def remove(self, hash_id: str) -> bool:
        if hash_id not in self._peers:
            return False
        del self._peers[hash_id]
        self.removed[hash_id] = time.time()
        self.save()
        return True

    def list_peers(self, *, include_blocked: bool = False) -> list[TrustedPeer]:
        peers = [
            p for p in self._peers.values()
            if is_valid_hash_id(p.hash_id) and (include_blocked or not p.blocked)
        ]
        return sorted(peers, key=lambda p: p.name.lower())

    def get(self, hash_id: str) -> TrustedPeer | None:
        return self._peers.get(hash_id)

    def set_public_key(self, hash_id: str, public_key_hex: str) -> None:
        """Pin a verified public key for a contact added by hash only."""
        peer = self._peers.get(hash_id)
        if peer and not peer.public_key:
            peer.public_key = public_key_hex
            self.save()

    def update(
        self,
        hash_id: str,
        *,
        name: str | None = None,
        blocked: bool | None = None,
        tcp_host: str | None = None,
        tcp_port: int | None = None,
        wan_host: str | None = None,
        wan_port: int | None = None,
        wan_enabled: bool | None = None,
        connection_mode: str | None = None,
        alt_hosts: list[str] | None = None,
        pair_own: str | None = None,
        pair_peer: str | None = None,
        relay_code_peer: str | None = None,
        pair_token_out: str | None = None,
        onion: str | None = None,
    ) -> TrustedPeer | None:
        peer = self._peers.get(hash_id)
        if not peer:
            return None
        if name is not None:
            peer.name = name.strip() or peer.name
        if blocked is not None:
            peer.blocked = blocked
        if tcp_host is not None:
            peer.tcp_host = tcp_host.strip()
        if tcp_port is not None:
            peer.tcp_port = tcp_port
        if wan_host is not None:
            peer.wan_host = wan_host.strip()
        if wan_port is not None:
            peer.wan_port = wan_port
        if wan_enabled is not None:
            peer.wan_enabled = wan_enabled
        if connection_mode is not None and connection_mode in ("auto", "lan", "wan"):
            peer.connection_mode = connection_mode
        if alt_hosts is not None:
            peer.alt_hosts = [h.strip() for h in alt_hosts if h.strip()][:4]
        if pair_own is not None:
            peer.pair_own = pair_own
        if pair_peer is not None:
            peer.pair_peer = pair_peer
        if relay_code_peer is not None:
            peer.relay_code_peer = relay_code_peer
        if pair_token_out is not None:
            peer.pair_token_out = pair_token_out
        if onion is not None:
            peer.onion = onion
        self.save()
        return peer