"""Port binding helpers with automatic fallback."""

from __future__ import annotations

import errno
import socket
from collections.abc import Callable
from typing import Any

from srltcp.utils.logging import get_logger

log = get_logger(__name__)

DEFAULT_PORT_ATTEMPTS = 20


def port_in_use(host: str, port: int) -> bool:
    """Return True if a TCP port is already bound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return False
        except OSError as exc:
            if exc.errno in (errno.EADDRINUSE, errno.EACCES):
                return True
            raise


async def start_tcp_server(
    factory: Callable[[], Any],
    host: str,
    port: int,
    *,
    max_attempts: int = DEFAULT_PORT_ATTEMPTS,
    strict: bool = False,
) -> tuple[Any, int]:
    """
    Start an asyncio TCP server, trying successive ports if busy.
    Returns (server, bound_port).
    """
    import asyncio

    attempts = 1 if strict else max_attempts
    last_err: OSError | None = None
    for offset in range(attempts):
        try_port = port + offset
        try:
            server = await asyncio.start_server(
                factory, host, try_port, reuse_address=True
            )
            if offset:
                log.warning(
                    "TCP port %d in use — bound to %d instead "
                    "(use --tcp-port or stop the other process)",
                    port,
                    try_port,
                )
            return server, try_port
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            last_err = exc
    hint = (
        " Run ./run.sh stop or: pkill -f 'python -m srltcp'"
        if strict
        else ""
    )
    raise OSError(
        errno.EADDRINUSE,
        f"Could not bind TCP port {port}"
        + ("" if strict else f"–{port + attempts - 1}")
        + ". Another SRLTCP instance may be running."
        + hint,
    ) from last_err


async def bind_udp_port(
    loop: Any,
    protocol_factory: Callable[[], Any],
    host: str,
    port: int,
    *,
    max_attempts: int = DEFAULT_PORT_ATTEMPTS,
    strict: bool = False,
) -> tuple[Any, int]:
    """Bind a UDP datagram endpoint with port fallback."""
    attempts = 1 if strict else max_attempts
    last_err: OSError | None = None
    for offset in range(attempts):
        try_port = port + offset
        try:
            try:
                transport, _ = await loop.create_datagram_endpoint(
                    protocol_factory,
                    local_addr=(host, try_port),
                    reuse_address=True,
                )
            except TypeError:
                transport, _ = await loop.create_datagram_endpoint(
                    protocol_factory,
                    local_addr=(host, try_port),
                )
            if offset:
                log.warning("UDP discovery port %d in use — using %d", port, try_port)
            return transport, try_port
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            last_err = exc
    raise OSError(
        errno.EADDRINUSE,
        f"Could not bind UDP port {port}"
        + ("" if strict else f"–{port + attempts - 1}.")
        + ("." if strict else ""),
    ) from last_err


async def start_web_site(
    runner: Any,
    host: str,
    port: int,
    *,
    ssl_context: Any = None,
    max_attempts: int = DEFAULT_PORT_ATTEMPTS,
    strict: bool = False,
) -> tuple[Any, int]:
    """Start aiohttp TCPSite with port fallback."""
    from aiohttp import web

    attempts = 1 if strict else max_attempts
    last_err: OSError | None = None
    for offset in range(attempts):
        try_port = port + offset
        try:
            site = web.TCPSite(
                runner,
                host,
                try_port,
                ssl_context=ssl_context,
                reuse_address=True,
            )
            await site.start()
            if offset:
                log.warning("Web UI port %d in use — serving on %d", port, try_port)
            return site, try_port
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            last_err = exc
    hint = (
        " Run ./run.sh stop or: pkill -f 'python -m srltcp'"
        if strict
        else ""
    )
    raise OSError(
        errno.EADDRINUSE,
        f"Could not bind web port {port}"
        + ("" if strict else f"–{port + attempts - 1}")
        + ". Stop the other process using this port or disable strict ports."
        + hint,
    ) from last_err