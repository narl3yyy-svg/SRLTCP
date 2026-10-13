"""Logging configuration."""

from __future__ import annotations

import logging
import sys


class _QuietAsyncioSSLFilter(logging.Filter):
    """Drop asyncio noise about connections the other side already closed:
    TLS shutdown races, and on Windows "WinError 10054" from the proactor
    loop each time a browser tab closes a keep-alive connection."""

    def filter(self, record: logging.LogRecord) -> bool:
        if "SSL connection is closed" in record.getMessage():
            return False
        exc = record.exc_info[1] if record.exc_info else None
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)) and (
            "_call_connection_lost" in record.getMessage()
        ):
            return False
        return True


def setup_logging(level: str = "INFO", *, debug: bool = False) -> None:
    """Configure root logger for CLI and web server."""
    if debug:
        level = "DEBUG"
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
        force=True,
    )
    asyncio_logger = logging.getLogger("asyncio")
    asyncio_logger.addFilter(_QuietAsyncioSSLFilter())
    if debug:
        asyncio_logger.setLevel(logging.DEBUG)
        logging.getLogger("aiohttp").setLevel(logging.DEBUG)
        logging.getLogger("srltcp").setLevel(logging.DEBUG)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)