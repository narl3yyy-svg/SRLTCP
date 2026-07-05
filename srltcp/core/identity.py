"""Per-transport identity management (Ed25519 keypairs)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from srltcp.core.protocol.crypto import identity_hash, public_key_bytes
from srltcp.utils.platform import data_dir

TransportKind = Literal["tcp", "serial"]


@dataclass
class Identity:
    """Node identity for a specific transport."""

    name: str
    transport: TransportKind
    private_key: Ed25519PrivateKey
    public_key: Ed25519PublicKey
    hash_id: str

    @classmethod
    def generate(cls, name: str, transport: TransportKind) -> Identity:
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key()
        pub_bytes = public_key_bytes(public_key)
        return cls(
            name=name,
            transport=transport,
            private_key=private_key,
            public_key=public_key,
            hash_id=identity_hash(pub_bytes),
        )

    @classmethod
    def from_private_bytes(
        cls, name: str, transport: TransportKind, raw: bytes
    ) -> Identity:
        private_key = Ed25519PrivateKey.from_private_bytes(raw)
        public_key = private_key.public_key()
        pub_bytes = public_key_bytes(public_key)
        return cls(
            name=name,
            transport=transport,
            private_key=private_key,
            public_key=public_key,
            hash_id=identity_hash(pub_bytes),
        )

    def private_bytes(self) -> bytes:
        return self.private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )

    def public_bytes(self) -> bytes:
        return public_key_bytes(self.public_key)

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "transport": self.transport,
            "hash_id": self.hash_id,
            "public_key": self.public_bytes().hex(),
            "private_key": self.private_bytes().hex(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> Identity:
        return cls.from_private_bytes(
            data["name"],
            data["transport"],  # type: ignore[arg-type]
            bytes.fromhex(data["private_key"]),
        )


class IdentityStore:
    """Persist identities per transport under ~/.srltcp/identities/."""

    def __init__(self, base: Path | None = None) -> None:
        self.base = (base or data_dir()) / "identities"
        self.base.mkdir(parents=True, exist_ok=True)

    def _path(self, transport: TransportKind) -> Path:
        return self.base / f"identity_{transport}.json"

    def load_or_create(
        self, name: str, transport: TransportKind
    ) -> Identity:
        path = self._path(transport)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            identity = Identity.from_dict(data)
            if name and identity.name != name:
                identity.name = name
                self.save(identity)
            return identity
        identity = Identity.generate(name, transport)
        self.save(identity)
        return identity

    def save(self, identity: Identity) -> None:
        path = self._path(identity.transport)
        path.write_text(
            json.dumps(identity.to_dict(), indent=2),
            encoding="utf-8",
        )

    def load(self, transport: TransportKind) -> Identity | None:
        path = self._path(transport)
        if not path.exists():
            return None
        return Identity.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def regenerate(self, name: str, transport: TransportKind) -> Identity:
        identity = Identity.generate(name, transport)
        self.save(identity)
        return identity

    def delete(self, transport: TransportKind) -> bool:
        path = self._path(transport)
        if not path.exists():
            return False
        path.unlink()
        return True