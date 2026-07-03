"""Messaging constants and timeouts."""

DEFAULT_TCP_PORT = 7825
RELAY_TCP_PORT = 7827
DISCOVERY_PORT = 7826
WEB_PORT = 9876

CHUNK_SIZE = 256 * 1024  # 256 KiB — balance throughput and connection stability
CHUNK_SEND_DELAY = 0.005  # seconds between chunks for TCP flow control
SERIAL_CHUNK_SIZE = 8 * 1024  # 8 KiB — serial/RF links have smaller frames
SERIAL_CHUNK_DELAY = 0.02  # seconds between serial chunks
COMPRESS_THRESHOLD = 64 * 1024  # compress payloads >= 64 KiB
LINK_TIMEOUT = 30.0
TRANSFER_COOLDOWN = 45.0  # seconds to treat link as stable after transfer ends
PING_INTERVAL = 15.0
ANNOUNCE_INTERVAL = 5.0
MAX_MESSAGE_SIZE = 16 * 1024 * 1024