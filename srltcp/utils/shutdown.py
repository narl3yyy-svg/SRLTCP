"""Graceful asyncio shutdown helpers."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Awaitable, Callable

from srltcp.utils.logging import get_logger

log = get_logger(__name__)

ShutdownHook = Callable[[], Awaitable[None]]


class GracefulShutdown:
    """Register signal handlers and run cleanup hooks on Ctrl+C / SIGTERM."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._hooks: list[ShutdownHook] = []
        self._registered = False

    @property
    def event(self) -> asyncio.Event:
        return self._event

    def add_hook(self, hook: ShutdownHook) -> None:
        self._hooks.append(hook)

    def register_signals(self) -> None:
        if self._registered:
            return
        loop = asyncio.get_running_loop()

        def _handler(sig: signal.Signals) -> None:
            log.info("Received %s — shutting down…", sig.name)
            self._event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _handler, sig)
            except NotImplementedError:
                # Windows fallback
                signal.signal(sig, lambda _s, _f: self._event.set())
        self._registered = True

    async def wait(self) -> None:
        self.register_signals()
        try:
            await self._event.wait()
        except asyncio.CancelledError:
            log.info("Task cancelled — shutting down…")

    async def run_cleanup(self) -> None:
        for hook in reversed(self._hooks):
            try:
                await hook()
            except Exception as exc:
                log.warning("Cleanup hook error: %s", exc)
        # Cancel stray tasks except current
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        log.info("SRLTCP stopped.")