"""Disappearing messages: what you send is deleted from both phones after a while.

*Settings → General → Disappearing messages* sets a timer for the messages
and files **you** send in one-to-one chats. Each message carries its timer
inside the encrypted session; the receiving phone deletes it that long after
it arrived, the sending phone that long after sending. A received file is
deleted from the incoming folder too (copies you saved elsewhere stay).

Your contact's app does this for you, but nothing can force a modified app
or a screenshot to forget: it protects against someone getting hold of a
phone later, not against the person you write to.
"""

from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING, Any

from srltcp.core.messaging.models import TransferState
from srltcp.utils.logging import get_logger

if TYPE_CHECKING:
    from srltcp.core.messaging.backend import MessagingBackend
    from srltcp.core.messaging.models import ChatMessage

log = get_logger(__name__)

CHOICES = (0, 3600, 86400, 7 * 86400, 28 * 86400)  # what the settings offer
MIN_TTL, MAX_TTL = 60, 28 * 86400  # what a contact's message may ask for


def wire_ttl(data: dict[str, Any]) -> int:
    """The timer a received message asks for (0 = keep), within bounds."""
    raw = data.get("ttl")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return raw if MIN_TTL <= raw <= MAX_TTL else 0


def expires_at(msg: ChatMessage) -> float:
    try:
        return float(msg.metadata.get("expires_at") or 0)
    except (TypeError, ValueError):
        return 0.0


class DisappearMixin:
    """Timers on outgoing messages; deleting expired ones (both phones)."""

    def disappear_ttl(self: MessagingBackend) -> int:
        ttl = int(getattr(self.config, "disappear_seconds", 0) or 0)
        return ttl if ttl in CHOICES else 0

    @staticmethod
    def stamp_ttl(msg: ChatMessage, ttl: int, start: float | None = None) -> None:
        if ttl:
            msg.metadata["ttl"] = ttl
            msg.metadata["expires_at"] = (start if start is not None else time.time()) + ttl

    async def expire_messages(self: MessagingBackend, *, notify: bool = True) -> int:
        """Delete messages whose timer ran out (and their received files)."""
        now = time.time()
        gone = [m for m in self._messages if 0 < expires_at(m) <= now]
        if not gone:
            return 0
        ids = {m.id for m in gone}
        self._messages = [m for m in self._messages if m.id not in ids]
        for msg in gone:
            tid = str(msg.metadata.get("transfer_id") or "")
            if tid:
                await self._expire_file(tid)
        self._persist_messages()
        log.info("%d disappearing message(s) deleted", len(ids))
        if notify and self._on_event:
            await self._on_event({"kind": "messages_removed", "ids": sorted(ids)})
        return len(ids)

    async def _expire_file(self: MessagingBackend, tid: str) -> None:
        transfer = self._transfers.get(tid)
        if transfer is None:
            return
        if transfer.state not in (
            TransferState.COMPLETE, TransferState.CANCELLED,
            TransferState.FAILED, TransferState.REJECTED,
        ):
            await self.cancel_transfer(tid)
        if not self._is_outgoing(transfer):
            path = self._incoming_paths.pop(tid, None)
            if path is not None:
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
        self._transfers.pop(tid, None)
