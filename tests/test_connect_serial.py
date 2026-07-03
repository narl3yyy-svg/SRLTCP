"""Serial connect transport-mismatch regression tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from srltcp.core.messaging.backend import MessagingBackend, NodeConfig
from srltcp.core.messaging.links import PeerLink
from srltcp.core.trusted import TrustedPeer
from srltcp.transports.base import TransportPeer


@pytest.fixture
def backend() -> MessagingBackend:
    b = MessagingBackend(NodeConfig(name="test-node"))
    b.tcp_transport = MagicMock()
    b.tcp_transport.has_peer = MagicMock(return_value=False)
    b.tcp_transport.disconnect = AsyncMock()
    b.tcp_transport.connect = AsyncMock(return_value="new-tcp-peer")

    b.serial_transport = MagicMock()
    b.serial_transport.has_peer = MagicMock(return_value=True)
    b.serial_transport.peers = MagicMock(
        return_value=[
            TransportPeer(
                peer_id="serial-peer-1",
                address="/dev/ttyUSB0",
                transport="serial",
            )
        ]
    )
    b._initiate_handshake = AsyncMock()  # type: ignore[method-assign]
    return b


@pytest.mark.asyncio
async def test_stale_tcp_link_torn_down_before_serial_connect(backend: MessagingBackend) -> None:
    """Incomplete TCP link with dead peer_id must not block serial connect."""
    hash_id = "ab" * 16
    backend.trusted.add(
        TrustedPeer(
            hash_id=hash_id,
            name="serial-peer",
            public_key="01" * 32,
            transport="serial",
        )
    )
    stale = PeerLink(
        hash_id=hash_id,
        transport_peer_id="dead-tcp-peer",
        transport="tcp",
        address="10.0.0.1:7825",
        public_key=b"\x01" * 32,
        handshake_complete=False,
    )
    backend.register_link(stale)

    ok = await backend.connect_to_peer(hash_id, transport="serial")

    assert ok is True
    backend.tcp_transport.disconnect.assert_awaited_once_with("dead-tcp-peer")
    link = backend.get_link(hash_id)
    assert link is not None
    assert link.transport == "serial"
    assert link.transport_peer_id == "serial-peer-1"
    backend._initiate_handshake.assert_awaited_once_with(hash_id)


@pytest.mark.asyncio
async def test_serial_connect_skips_unreachable_incomplete_link(backend: MessagingBackend) -> None:
    hash_id = "cd" * 16
    backend.trusted.add(
        TrustedPeer(
            hash_id=hash_id,
            name="serial-peer",
            public_key="02" * 32,
            transport="serial",
        )
    )
    incomplete_serial = PeerLink(
        hash_id=hash_id,
        transport_peer_id="gone-serial",
        transport="serial",
        address="/dev/ttyUSB0",
        public_key=b"\x02" * 32,
        handshake_complete=False,
    )
    backend.register_link(incomplete_serial)
    backend.serial_transport.has_peer = MagicMock(return_value=False)

    ok = await backend.connect_to_peer(hash_id, transport="serial")

    assert ok is True
    link = backend.get_link(hash_id)
    assert link is not None
    assert link.transport_peer_id == "serial-peer-1"