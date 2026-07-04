"""Logging configuration."""

from __future__ import annotations

import logging
import sys


class _QuietAsyncioSSLFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "SSL connection is closed" not in record.getMessage()


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