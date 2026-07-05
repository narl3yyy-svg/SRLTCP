"""Messaging backend — mixin-composed like chatx5."""

from srltcp.core.messaging.backend import MessagingBackend
from srltcp.core.messaging.constants import (
    CHUNK_SIZE,
    COMPRESS_THRESHOLD,
    DEFAULT_HUB_PORT,
    DEFAULT_TCP_PORT,
)
from srltcp.core.messaging.models import ChatMessage, FileTransfer, TransferState

__all__ = [
    "CHUNK_SIZE",
    "COMPRESS_THRESHOLD",
    "DEFAULT_HUB_PORT",
    "DEFAULT_TCP_PORT",
    "MessagingBackend",
    "ChatMessage",
    "FileTransfer",
    "TransferState",
]