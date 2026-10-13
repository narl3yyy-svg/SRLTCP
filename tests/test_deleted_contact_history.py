"""Deleting a contact keeps the chat for 24 hours; after that it's gone."""

from __future__ import annotations

import time
from pathlib import Path

from srltcp.core.messaging.models import ChatMessage
from srltcp.core.trusted import REMOVED_HISTORY_GRACE, TrustedPeer
from tests.test_e2e_nodes import make_node

PEER = "ab" * 16
OTHER = "cd" * 16


def _setup(tmp_path: Path):  # type: ignore[no-untyped-def]
    node = make_node(tmp_path, "me")
    me = "ee" * 16
    node.trusted.add(TrustedPeer(hash_id=PEER, name="Pat"))
    node.trusted.add(TrustedPeer(hash_id=OTHER, name="Sam"))
    node._messages = [
        ChatMessage(id="1", sender_hash=me, recipient_hash=PEER, text="hi pat", transport="tcp"),
        ChatMessage(id="2", sender_hash=PEER, recipient_hash=me, text="hey", transport="tcp"),
        ChatMessage(id="3", sender_hash=me, recipient_hash=OTHER, text="hi sam", transport="tcp"),
        ChatMessage(id="4", sender_hash=PEER, recipient_hash=me, text="group msg",
                    transport="tcp", metadata={"group_id": "g" * 32}),
    ]
    return node


def _texts(node, peer: str) -> list[str]:  # type: ignore[no-untyped-def]
    return [m["text"] for m in node.get_messages(peer=peer)]


def test_readded_within_24h_resumes_chat(tmp_path: Path) -> None:
    node = _setup(tmp_path)
    node.trusted.remove(PEER)
    node.trusted.removed[PEER] = time.time() - (REMOVED_HISTORY_GRACE - 3600)  # 23 h ago
    assert _texts(node, PEER) == ["hi pat", "hey"]
    node.trusted.add(TrustedPeer(hash_id=PEER, name="Pat"))
    node.trusted.removed.clear()
    assert _texts(node, PEER) == ["hi pat", "hey"]
    assert PEER not in node.trusted.removed


def test_deleted_over_24h_ago_starts_fresh(tmp_path: Path) -> None:
    node = _setup(tmp_path)
    node.trusted.remove(PEER)
    node.trusted.removed[PEER] = time.time() - (REMOVED_HISTORY_GRACE + 3600)  # 25 h ago
    assert _texts(node, PEER) == []
    assert _texts(node, OTHER) == ["hi sam"]  # other chats untouched
    assert any(m.text == "group msg" for m in node._messages)  # group chats stay
    node.trusted.add(TrustedPeer(hash_id=PEER, name="Pat"))
    assert _texts(node, PEER) == []


def test_deletion_time_survives_restart(tmp_path: Path) -> None:
    node = _setup(tmp_path)
    node.trusted.remove(PEER)
    again = make_node(tmp_path, "me")
    assert PEER in again.trusted.removed
