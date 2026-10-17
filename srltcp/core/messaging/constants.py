"""Messaging constants and timeouts."""

DEFAULT_TCP_PORT = 7825

DISCOVERY_PORT = 7826
WEB_PORT = 9876

# File transfers. Each path gets a chunk size and a window: how many bytes
# may be sent before the receiver confirms them on disk (FILE_ACK). The
# window keeps a fast link full and bounds what a broken path can lose.
CHUNK_SIZE = 1024 * 1024  # direct TCP (LAN / VPN / punched)
CHUNK_WINDOW = 24 * 1024 * 1024
UDP_CHUNK_SIZE = 256 * 1024  # direct UDP: a message waits behind at most one chunk
# Serial radio: the link sends file data in the turns chat and calls leave
# (a message waits behind one turn at most), so pieces can be larger; the
# window is several seconds of radio time.
SERIAL_CHUNK_SIZE = 8 * 1024
SERIAL_CHUNK_DELAY = 0.0  # (the radio link paces itself)
SERIAL_WINDOW = 32 * 1024
TRANSFER_STALL = 12.0  # no confirmation this long: resend from the last confirmed byte
TRANSFER_STALL_SLOW = 40.0  # the same over serial
PROGRESS_EMIT_INTERVAL = 0.3  # seconds between UI progress updates
COMPRESS_THRESHOLD = 64 * 1024  # compress chat payloads >= 64 KiB
OFFER_TIMEOUT = 30.0  # unanswered file offer: re-offer once, then queue
INCOMING_IDLE = 120.0  # incoming file with no data this long: paused, link re-checked
TRANSFER_COOLDOWN = 45.0  # seconds to treat link as stable after transfer ends
PING_INTERVAL = 10.0
ANNOUNCE_INTERVAL = 5.0