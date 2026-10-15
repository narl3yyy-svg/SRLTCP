"""Messaging constants and timeouts."""

DEFAULT_TCP_PORT = 7825

DISCOVERY_PORT = 7826
WEB_PORT = 9876

# File transfers. Each path gets a chunk size and a window: how many bytes
# may be sent before the receiver confirms them on disk (FILE_ACK). The
# window keeps a fast link full and bounds what a broken path can lose.
CHUNK_SIZE = 1024 * 1024  # direct TCP (LAN / VPN / punched)
CHUNK_WINDOW = 24 * 1024 * 1024
TOR_CHUNK_SIZE = 64 * 1024  # small: chat and pings never queue behind a big chunk
# Tor lets each stream have only ~250 KB in flight per round trip (often
# 0.5-1 s on onion circuits), so one circuit tops out around 0.3 MB/s:
# speed comes from many circuits at once.
TOR_WINDOW = 8 * 1024 * 1024  # spread over the parallel Tor circuits
TOR_LANES = 8  # extra Tor circuits that carry file data (chat stays on the main one)
# One stuck circuit holds the chunk the receiver needs next and so the whole
# window. Nothing confirmed this long: send that circuit's chunks on the
# others and replace it (much sooner than the full-window resend below).
TOR_REPAIR = 6.0
RELAY_CHUNK_SIZE = 128 * 1024  # stays under the relay's 256 KiB frame limit
RELAY_WINDOW = 2 * 1024 * 1024
SERIAL_CHUNK_SIZE = 8 * 1024  # serial/RF links have smaller frames
SERIAL_CHUNK_DELAY = 0.02  # seconds between serial chunks
SERIAL_WINDOW = 64 * 1024
TRANSFER_STALL = 12.0  # no confirmation this long: resend from the last confirmed byte
TRANSFER_STALL_SLOW = 40.0  # the same over Tor, the relay or serial
PROGRESS_EMIT_INTERVAL = 0.3  # seconds between UI progress updates
COMPRESS_THRESHOLD = 64 * 1024  # compress chat payloads >= 64 KiB
OFFER_TIMEOUT = 30.0  # unanswered file offer: re-offer once, then queue
INCOMING_IDLE = 120.0  # incoming file with no data this long: paused, link re-checked
TRANSFER_COOLDOWN = 45.0  # seconds to treat link as stable after transfer ends
PING_INTERVAL = 10.0
ANNOUNCE_INTERVAL = 5.0