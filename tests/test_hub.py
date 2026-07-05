"""Hub presence registry and opaque envelope helpers."""

from __future__ import annotations

import pytest

from srltcp.core.messaging.presence import HubPresenceRegistry
from srltcp.core.protocol.crypto import relay_unwrap, relay_wrap


def test_relay_wrap_roundtrip_with_src() -> None:
    dest = "a" * 32
    src = "b" * 32
    inner = b"encrypted-payload-bytes"
    wrapped = relay_wrap(inner, dest, src_hash=src)
    got_dest, got_src, got_inner = relay_unwrap(wrapped)
    assert got_dest == dest
    assert got_src == src
    assert got_inner == inner


def test_relay_wrap_invalid_hash() -> None:
    with pytest.raises(ValueError):
        relay_wrap(b"x", "not-a-hash")


def test_hub_presence_registry_register_and_lookup() -> None:
    reg = HubPresenceRegistry()
    body = b'{"type":"hub_register","hash_id":"' + b"a" * 32 + b'"}'
    hash_id = "a" * 32
    reg.register(hash_id, "conn-1", body)
    assert reg.get_conn(hash_id) == "conn-1"
    member = reg.get_member(hash_id)
    assert member is not None
    assert member.conn_peer_id == "conn-1"


def test_hub_presence_registry_unregister() -> None:
    reg = HubPresenceRegistry()
    hash_id = "c" * 32
    reg.register(hash_id, "conn-2", b"{}")
    removed = reg.unregister_conn("conn-2")
    assert removed is not None
    assert removed.hash_id == hash_id
    assert reg.get_conn(hash_id) is None


def test_hub_presence_list_other_conns() -> None:
    reg = HubPresenceRegistry()
    reg.register("d" * 32, "conn-a", b"{}")
    reg.register("e" * 32, "conn-b", b"{}")
    others = reg.list_other_conns("conn-a")
    assert others == ["conn-b"]