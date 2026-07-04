"""Manual announce transport validation tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from srltcp.core.messaging.announce import AnnounceError, AnnounceMixin


class _AnnounceBackend(AnnounceMixin):
    def __init__(self) -> None:
        self._init_announce()
        self.tcp_transport = MagicMock()
        self.tcp_transport.discovery_port = 7826
        self.tcp_transport.set_announce_payload = MagicMock()
        self.tcp_transport.broadcast_discovery = AsyncMock(return_value=True)
        self.serial_transport = MagicMock()
        self.serial_transport.broadcast = AsyncMock()
        identity = MagicMock()
        identity.hash_id = "a" * 32
        identity.name = "node-a"
        identity.transport = "tcp"
        identity.public_bytes.return_value = b"\x01" * 32
        self.identities = {"tcp": identity}
        self.config = MagicMock(tcp_port=7825, lan_ip="10.0.0.5")
        self.discovery = MagicMock()
        self._on_peer_discovered = None
        self._running = True


@pytest.mark.asyncio
async def test_announce_tcp_broadcasts() -> None:
    backend = _AnnounceBackend()
    announced = await backend.announce("tcp")
    assert announced == ["tcp"]
    assert backend.tcp_transport.broadcast_discovery.await_count == 3


@pytest.mark.asyncio
async def test_announce_serial_extra_bursts() -> None:
    backend = _AnnounceBackend()
    serial_identity = MagicMock()
    serial_identity.hash_id = "b" * 32
    serial_identity.name = "node-b"
    serial_identity.transport = "serial"
    serial_identity.public_bytes.return_value = b"\x02" * 32
    backend.identities["serial"] = serial_identity
    announced = await backend.announce("serial")
    assert announced == ["serial"]
    assert backend.serial_transport.broadcast.await_count == 5


@pytest.mark.asyncio
async def test_announce_tcp_requires_discovery_socket() -> None:
    backend = _AnnounceBackend()
    backend.tcp_transport.broadcast_discovery = AsyncMock(return_value=False)
    with pytest.raises(AnnounceError, match="UDP discovery socket"):
        await backend.announce("tcp")


@pytest.mark.asyncio
async def test_announce_serial_requires_transport() -> None:
    backend = _AnnounceBackend()
    serial_identity = MagicMock()
    serial_identity.hash_id = "b" * 32
    serial_identity.name = "node-b"
    serial_identity.transport = "serial"
    serial_identity.public_bytes.return_value = b"\x02" * 32
    backend.identities["serial"] = serial_identity
    backend.serial_transport = None
    with pytest.raises(AnnounceError, match="Serial transport is not open"):
        await backend.announce("serial")


@pytest.mark.asyncio
async def test_announce_rejects_unknown_transport() -> None:
    backend = _AnnounceBackend()
    with pytest.raises(AnnounceError, match="Unknown transport"):
        await backend.announce("bluetooth")