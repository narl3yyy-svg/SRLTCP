"""Trusted peer list — required before messaging."""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from srltcp.utils.platform import data_dir

_HASH_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_GENERIC_NAMES = frozenset({"peer", "trusted", "serial-peer", "tcp-peer", "unknown"})


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TrustedStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (data_dir() / "trusted.json")
        self._peers: dict[str, TrustedPeer] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
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
            existing = cleaned.get(peer.hash_id)
            if not existing or peer.added_at >= existing.added_at:
                cleaned[peer.hash_id] = peer
        self._peers = cleaned
        if len(cleaned) != len(raw.get("peers", [])):
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"peers": [p.to_dict() for p in self._peers.values()]}
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def is_trusted(self, hash_id: str) -> bool:
        peer = self._peers.get(hash_id)
        return bool(peer and not peer.blocked)

    def add(self, peer: TrustedPeer) -> TrustedPeer:
        if not is_valid_hash_id(peer.hash_id):
            raise ValueError("invalid peer hash_id")
        peer.hash_id = peer.hash_id.lower()
        if peer.name.strip().lower() in _GENERIC_NAMES:
            peer.name = peer.name.strip() or "Peer"
        self._peers[peer.hash_id] = peer
        self.save()
        return peer

    def remove(self, hash_id: str) -> bool:
        if hash_id not in self._peers:
            return False
        del self._peers[hash_id]
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
        self.save()
        return peer