"""Per-transport identity management (Ed25519 keypairs)."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from srltcp.core.protocol.crypto import identity_hash, public_key_bytes
from srltcp.core.protocol.pq import DSA_SEED_SIZE, DsaKey
from srltcp.utils.jsonstore import load_json, save_json
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
    # Post-quantum signing key (ML-DSA-65) from a random seed of its own:
    # never derived from the Ed25519 key, which a quantum computer could
    # recover from the public key.
    pq_seed: bytes = field(default_factory=lambda: secrets.token_bytes(DSA_SEED_SIZE))
    _pq: DsaKey | None = field(default=None, repr=False, compare=False)

    @property
    def pq(self) -> DsaKey:
        if self._pq is None:
            self._pq = DsaKey(self.pq_seed)
        return self._pq

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
            "pq_seed": self.pq_seed.hex(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> Identity:
        identity = cls.from_private_bytes(
            data["name"],
            data["transport"],  # type: ignore[arg-type]
            bytes.fromhex(data["private_key"]),
        )
        seed = bytes.fromhex(data.get("pq_seed", ""))
        if len(seed) == DSA_SEED_SIZE:
            identity.pq_seed = seed
        return identity


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
        data = load_json(path)
        if isinstance(data, dict):
            identity = Identity.from_dict(data)
            changed = "pq_seed" not in data  # identity from before 0.9: add its ML-DSA key
            if name and identity.name != name:
                identity.name = name
                changed = True
            if changed:
                self.save(identity)
            return identity
        identity = Identity.generate(name, transport)
        self.save(identity)
        return identity

    def save(self, identity: Identity) -> None:
        # Encrypted, owner-only and atomic: the file holds the private keys.
        if not save_json(self._path(identity.transport), identity.to_dict()):
            raise OSError("could not save the identity")

    def load(self, transport: TransportKind) -> Identity | None:
        data = load_json(self._path(transport))
        return Identity.from_dict(data) if isinstance(data, dict) else None

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