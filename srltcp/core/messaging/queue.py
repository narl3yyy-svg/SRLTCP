"""Outbound message queue mixin."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from srltcp.core.messaging.models import ChatMessage

if TYPE_CHECKING:
    from srltcp.core.messaging.backend import MessagingBackend


class QueueMixin:
    """Queue messages when links are down; drain when connected."""

    _outbound_queue: list[ChatMessage]
    _queue_lock: asyncio.Lock

    def _init_queue(self: MessagingBackend) -> None:
        self._outbound_queue = []
        self._queue_lock = asyncio.Lock()

    async def enqueue_message(self: MessagingBackend, message: ChatMessage) -> None:
        async with self._queue_lock:
            self._outbound_queue.append(message)

    async def drain_queue(self: MessagingBackend, hash_id: str) -> None:
        async with self._queue_lock:
            pending = [m for m in self._outbound_queue if m.recipient_hash == hash_id]
            self._outbound_queue = [m for m in self._outbound_queue if m.recipient_hash != hash_id]
        for msg in pending:
            await self.send_message(msg.recipient_hash, msg.text, transport=msg.transport)