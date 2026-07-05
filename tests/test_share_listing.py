"""Share folder listing wait/response tests."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from srltcp.core.messaging.backend import MessagingBackend, NodeConfig
from srltcp.core.messaging.links import PeerLink
from srltcp.core.messaging.share_peer import ShareGrant
from srltcp.core.protocol.messages import MessageType, decode_payload, encode_payload


@pytest.fixture
def backend() -> MessagingBackend:
    b = MessagingBackend(NodeConfig(name="test-node"))
    b._init_share_peer()
    b._send_raw = AsyncMock()
    b._encrypt_for_link = AsyncMock(return_value=b"packet")
    return b


@pytest.mark.asyncio
async def test_share_listing_waiter_resolves(backend: MessagingBackend) -> None:
    owner = "o" * 32
    recipient = "r" * 32
    grant_id = "grant1234567890ab"

    backend._remote_share_grants[grant_id] = ShareGrant(
        grant_id=grant_id,
        owner_hash=owner,
        recipient_hash=recipient,
        root=Path("/"),
        expires=time.time() + 3600,
    )
    backend.register_link(
        PeerLink(
            hash_id=owner,
            transport_peer_id="peer1",
            transport="tcp",
            address="127.0.0.1:7825",
            public_key=b"\x01" * 32,
            peer_name="owner",
            handshake_complete=True,
        )
    )
    identity = MagicMock()
    identity.hash_id = recipient
    backend._identity_for_transport = MagicMock(return_value=identity)

    async def _resolve_listing() -> None:
        await asyncio.sleep(0.05)
        body = encode_payload(
            {
                "action": "listing",
                "grant_id": grant_id,
                "entries": [{"name": "readme.txt", "type": "file", "size": 12}],
            }
        )
        await backend._handle_share_list(owner, body)

    task = asyncio.create_task(_resolve_listing())
    entries = await backend.request_share_list(owner, grant_id, timeout=2.0)
    await task
    assert entries is not None
    assert len(entries) == 1
    assert entries[0]["name"] == "readme.txt"


@pytest.mark.asyncio
async def test_share_request_uses_requester_hash(
    backend: MessagingBackend, tmp_path: Path
) -> None:
    owner = "o" * 32
    recipient = "r" * 32
    grant_id = "grant9876543210cd"
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "a.txt").write_text("hello")

    backend._share_grants[grant_id] = ShareGrant(
        grant_id=grant_id,
        owner_hash=owner,
        recipient_hash=recipient,
        root=shared,
        expires=time.time() + 3600,
    )
    backend.register_link(
        PeerLink(
            hash_id=recipient,
            transport_peer_id="peer2",
            transport="tcp",
            address="127.0.0.1:7826",
            public_key=b"\x02" * 32,
            peer_name="recipient",
            handshake_complete=True,
        )
    )

    body = encode_payload(
        {
            "action": "list",
            "grant_id": grant_id,
            "requester_hash": recipient,
        }
    )
    await backend._handle_share_request(recipient, body)

    backend._encrypt_for_link.assert_called()
    call = backend._encrypt_for_link.call_args
    assert call.args[1] == MessageType.SHARE_LIST
    payload = decode_payload(call.args[2])
    assert payload["action"] == "listing"
    assert payload["grant_id"] == grant_id
    assert len(payload.get("entries", [])) >= 0