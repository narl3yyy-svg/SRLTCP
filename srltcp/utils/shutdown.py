"""Graceful asyncio shutdown helpers."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Awaitable, Callable

from srltcp.utils.logging import get_logger

log = get_logger(__name__)

ShutdownHook = Callable[[], Awaitable[None]]

CLEANUP_TIMEOUT = 8.0


class GracefulShutdown:
    """Register signal handlers and run cleanup hooks on Ctrl+C / SIGTERM."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._hooks: list[ShutdownHook] = []
        self._registered = False
        self._signal_count = 0

    @property
    def event(self) -> asyncio.Event:
        return self._event

    def add_hook(self, hook: ShutdownHook) -> None:
        self._hooks.append(hook)

    def register_signals(self) -> None:
        if self._registered:
            return
        from srltcp.utils.platform import is_android

        if is_android():
            self._registered = True
            return
        loop = asyncio.get_running_loop()

        def _handler(sig: signal.Signals) -> None:
            self._signal_count += 1
            if self._signal_count == 1:
                log.info("Shutting down server…")
                self._event.set()
            else:
                log.warning("Forced exit")
                os._exit(130)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _handler, sig)
            except NotImplementedError:

                def _fallback_handler(s: signal.Signals = sig) -> None:
                    _handler(s)

                signal.signal(sig, lambda _s, _f, h=_fallback_handler: h())
        self._registered = True

    async def wait(self) -> None:
        self.register_signals()
        try:
            await self._event.wait()
        except asyncio.CancelledError:
            log.info("Shutting down server…")

    async def run_cleanup(self) -> None:
        for hook in reversed(self._hooks):
            try:
                await asyncio.wait_for(hook(), timeout=CLEANUP_TIMEOUT)
            except TimeoutError:
                log.warning("Cleanup hook timed out after %.0fs", CLEANUP_TIMEOUT)
            except Exception as exc:
                log.warning("Cleanup hook error: %s", exc)

        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        log.info("SRLTCP stopped — all ports released.")