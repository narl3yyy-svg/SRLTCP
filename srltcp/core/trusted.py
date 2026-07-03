"""Trusted peer list — required before messaging."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from srltcp.utils.platform import data_dir


@dataclass
class TrustedPeer:
    hash_id: str
    name: str
    transport: str = "tcp"
    public_key: str = ""
    tcp_host: str = ""
    tcp_port: int = 7825
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
        for item in raw.get("peers", []):
            peer = TrustedPeer(**item)
            self._peers[peer.hash_id] = peer

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"peers": [p.to_dict() for p in self._peers.values()]}
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def is_trusted(self, hash_id: str) -> bool:
        return hash_id in self._peers

    def add(self, peer: TrustedPeer) -> TrustedPeer:
        self._peers[peer.hash_id] = peer
        self.save()
        return peer

    def remove(self, hash_id: str) -> bool:
        if hash_id not in self._peers:
            return False
        del self._peers[hash_id]
        self.save()
        return True

    def list_peers(self) -> list[TrustedPeer]:
        return sorted(self._peers.values(), key=lambda p: p.name.lower())

    def get(self, hash_id: str) -> TrustedPeer | None:
        return self._peers.get(hash_id)