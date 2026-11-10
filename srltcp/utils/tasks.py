"""Fire-and-forget background tasks that can't vanish half-way.

The event loop keeps only a weak reference to a task: one nobody holds can be
garbage-collected before it finishes (the asyncio docs warn about this). A
punch round, a reconnect check or a restart started that way could silently
never happen. :func:`spawn` holds every task until it's done.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

_RUNNING: set[asyncio.Future[Any]] = set()


def spawn(aw: Awaitable[Any]) -> asyncio.Future[Any]:
    """Run ``aw`` in the background, kept alive until it's done."""
    task = asyncio.ensure_future(aw)
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)
    return task
