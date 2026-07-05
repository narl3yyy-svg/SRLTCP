"""Messaging constants and timeouts."""

DEFAULT_TCP_PORT = 7825
DEFAULT_HUB_PORT = 7825
DISCOVERY_PORT = 7826
WEB_PORT = 9876

CHUNK_SIZE = 1024 * 1024  # 1 MiB — LAN throughput (E2EE unchanged)
CHUNK_SEND_DELAY = 0.0  # no artificial delay on TCP; encryption is the limiter
PROGRESS_EMIT_INTERVAL = 0.25  # seconds between UI progress updates
SERIAL_CHUNK_SIZE = 8 * 1024  # 8 KiB — serial/RF links have smaller frames
SERIAL_CHUNK_DELAY = 0.02  # seconds between serial chunks
COMPRESS_THRESHOLD = 64 * 1024  # compress payloads >= 64 KiB
LINK_TIMEOUT = 30.0
TRANSFER_COOLDOWN = 45.0  # seconds to treat link as stable after transfer ends
PING_INTERVAL = 15.0
ANNOUNCE_INTERVAL = 5.0
MAX_MESSAGE_SIZE = 16 * 1024 * 1024