"""Direct encrypted connections over UDP: the fast path between networks.

Two phones on different networks (two mobile carriers, a phone and a home
Wi-Fi) usually can't open a TCP connection to each other: carrier NAT and
firewalls drop it. UDP gets through far more often, the way video calls do:

1. Each device asks a public STUN server which address and port its UDP
   socket has on the internet (``refresh_mapping``). IPv6 needs no STUN.
2. The devices swap those candidates, a random key contribution each and a
   connection id inside the end-to-end encrypted session they already have
   (Tor, the doorbell: see ``punch.py``).
3. Both send probes to each other's candidates at once. Each NAT sees the
   other's packets as replies to its own and lets them in (hole punching).
   The first authenticated packet that arrives fixes the path.
4. The connection behaves like a TCP connection (an ordered, reliable byte
   stream) and the normal signed SRLTCP handshake runs inside it, so the
   rest of the app treats it as any direct link. Call media can also go as
   unreliable datagrams (late is worse than lost).

On the wire, every packet after the 19-byte header is AES-256-GCM encrypted
with keys from the punch exchange (only the two contacts know them); the
header (magic, version, the receiver's connection id, packet number) is
authenticated. Packets that don't decrypt are dropped without an answer, so
nobody can inject data, reset a connection or use the socket to amplify
traffic. Packet numbers never repeat (they are the nonce) and replays are
dropped. The SRLTCP session inside adds its own end-to-end encryption.

Reliability and speed follow QUIC (RFC 9000/9002) in a small form: packets
are acknowledged by number with ranges, loss is detected by reordering
(3 packets) or time, and the receiver advertises how much it can buffer
(flow control). The sending rate follows BBR (the controller YouTube and
QUIC use on mobile networks): it measures the rate data actually arrives at
and the path's shortest round trip, and paces packets at that rate. A
loss-based window (TCP Reno) mistakes the random losses of a radio link for
congestion and, at mobile round trips, ends up at a few dozen KB/s; BBR
keeps the link full and the queues short. A 1400-byte probe after the start enables bigger packets
when the path allows them.
"""

from __future__ import annotations

import asyncio
import bisect
import collections
import contextlib
import hashlib
import heapq
import hmac
import ipaddress
import os
import secrets
import socket
import struct
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from srltcp.core.protocol.framing import FrameReader
from srltcp.transports.base import TransportPeer
from srltcp.utils.logging import get_logger

log = get_logger(__name__)

MAGIC = b"S\xd7"
VERSION = 1
HEADER = struct.Struct(">2sB8sQ")  # magic, version, receiver's connection id, packet number
TAG = 16
MIN_DATAGRAM = 1200  # works on every IPv6 path and practically every IPv4 one
BIG_DATAGRAM = 1400  # used once a probe of this size got through
MAX_RX_DATAGRAM = 2048

# Frames inside a packet (after decryption).
F_PAD, F_STREAM, F_ACK, F_DGRAM, F_PING, F_CLOSE = 0, 1, 2, 3, 4, 5
F_PATH_CHALLENGE, F_PATH_RESPONSE = 6, 7  # + 8-byte token: proves a new address answers
STREAM_HDR = struct.Struct(">BQH")  # type, offset, length
ACK_HDR = struct.Struct(">BQQIB")  # type, stream received up to, max data, ack delay µs, ranges
ACK_RANGE = struct.Struct(">QQ")  # highest, lowest packet number of a range
DGRAM_HDR = struct.Struct(">BH")
MAX_ACK_RANGES = 16

INITIAL_RTT = 0.3
MAX_ACK_DELAY = 0.01
ACK_EVERY = 4  # packets before an acknowledgement goes out without waiting
INITIAL_WINDOW = 32 * MIN_DATAGRAM
MIN_WINDOW = 4 * MIN_DATAGRAM
MAX_WINDOW = 32 * 1024 * 1024
LOSS_REORDER = 3
LOSS_TIME = 9 / 8
# BBR (see _Bbr)
STARTUP_GAIN = 2.885  # 2/ln 2: doubles the delivery rate every round trip
PROBE_GAINS = (1.25, 0.75, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0)  # one phase per round trip
CWND_GAIN = 2.5  # (2 in BBR; radio links deliver acknowledgements in bursts)
BW_ROUNDS = 10  # bandwidth = the best delivery rate of the last 10 round trips
MIN_RTT_WINDOW = 10.0  # shortest round trip of the last 10 s...
PROBE_RTT_TIME = 0.2  # ...re-measured by sending less for 0.2 s when it expires
RECV_WINDOW = 24 * 1024 * 1024  # bytes a receiver buffers ahead of the app
SEND_HIGH = 2 * 1024 * 1024  # unsent bytes before a writer waits (like a TCP buffer)
SEND_MAX = 48 * 1024 * 1024  # unsent + unacknowledged, hard limit
SKIP_MIN, SKIP_SPREAD = 1000, 2000  # a skipped packet number every 1000-3000
MAX_OOO_PIECES = 32768  # out-of-order pieces held (a full window of normal packets fits)
REPLAY_SPAN = 1 << 16  # packets older than this behind the newest are dropped
IDLE_TIMEOUT = 30.0
KEEPALIVE = 10.0  # carrier NATs forget UDP mappings after ~30 s
MIGRATE_AFTER = 1.0  # path silent this long: follow the peer to a new address
PROBE_EVERY = 0.2
# Birthday punch against a strict NAT: that side opens SPRAY_SOCKETS ports
# towards us; we probe SPRAY_PORTS_PER_ROUND random ports of its address per
# probe round. With 192 ports there and ~1000 probes here, one meets the other
# ~95 % of the time (Tailscale-style "hard NAT" traversal).
SPRAY_SOCKETS = 192
SPRAY_PORTS_PER_ROUND = 40
# Priming: a packet that opens our own NAT towards the contact but expires
# (hop limit) before reaching theirs. If a probe reached their router before
# they sent anything, many routers (Linux-based ones, many carriers) would
# remember it and give their own packets towards us a different port, and
# neither side's packets would match. Hops 2-4 cover a home router and a
# carrier's NAT; the other side's NAT is many more hops away.
PRIME_HOPS = (2, 3, 4)
DEFAULT_HOPS = 64
MAX_CONNECTIONS = 64
MAX_PENDING_PUNCHES = 16

STUN_MAGIC = 0x2112A442
STUN_SERVERS = (
    ("stun.l.google.com", 19302),
    ("stun.cloudflare.com", 3478),
    ("stun1.l.google.com", 19302),
)


def derive_keys(secret: bytes, leader_hash: str, follower_hash: str) -> tuple[bytes, bytes]:
    """(leader→follower, follower→leader) packet keys for one punch round."""
    okm = HKDF(
        algorithm=hashes.SHA256(),
        length=64,
        salt=None,
        info=b"srltcp-udp v1|" + leader_hash.encode() + b"|" + follower_hash.encode(),
    ).derive(secret)
    return okm[:32], okm[32:]


def _nonce(pn: int) -> bytes:
    return b"\0\0\0\0" + pn.to_bytes(8, "big")


@dataclass
class _Sent:
    at: float
    size: int
    ranges: list[tuple[int, int]]
    eliciting: bool
    probe_size: int = 0  # a path-MTU probe of this size
    # Delivery-rate sampling (BBR): the connection's state when this was sent.
    delivered: int = 0
    delivered_at: float = 0.0
    first_sent_at: float = 0.0
    app_limited: bool = False


@dataclass
class _Rtt:
    srtt: float = INITIAL_RTT
    rttvar: float = INITIAL_RTT / 2
    latest: float = INITIAL_RTT
    minimum: float = 0.0
    sampled: bool = False

    def update(self, sample: float, ack_delay: float) -> None:
        self.latest = sample
        self.minimum = sample if not self.minimum else min(self.minimum, sample)
        if sample - ack_delay >= self.minimum:
            sample -= ack_delay
        if not self.sampled:
            self.srtt, self.rttvar, self.sampled = sample, sample / 2, True
        else:
            self.rttvar = 0.75 * self.rttvar + 0.25 * abs(self.srtt - sample)
            self.srtt = 0.875 * self.srtt + 0.125 * sample


class _Bbr:
    """BBR congestion control (Cardwell et al., ACM Queue 2016; RFC draft
    draft-cardwell-iccrg-bbr-congestion-control), in a small form.

    The model is the bottleneck bandwidth (best delivery rate measured over
    the last ``BW_ROUNDS`` round trips) and the round-trip propagation delay
    (shortest round trip over ``MIN_RTT_WINDOW``). Packets are paced at
    gain × bandwidth and at most ``CWND_GAIN`` × bandwidth × delay are in
    flight. Random loss doesn't lower the rate (radio links lose packets
    without being congested); only heavy loss (a policer, a tiny buffer)
    does. States: startup (find the rate fast), drain (empty the queue it
    built), probe-bw (cruise, probing for more every 8 round trips) and
    probe-rtt (briefly send less to re-measure the real round trip).
    """

    def __init__(self, mss: int, now: float) -> None:
        self.mss = mss
        self.delivered = 0
        self.delivered_at = now
        self.first_sent_at = now
        self.app_limited_until = 0  # sampling is app-limited until this much is delivered
        self.round = 0
        self.next_round_delivered = 0
        self.round_lost = 0
        self.round_delivered_start = 0
        self.bw_by_round: list[tuple[int, float]] = []  # (round, best rate in it)
        self.bw = 0.0
        self.min_rtt = 0.0
        self.min_rtt_at = now
        self.state = "startup"
        self.full_bw = 0.0
        self.full_bw_rounds = 0
        self.cycle = 0
        self.cycle_at = now
        self.probe_rtt_until = 0.0
        self.cwnd = float(INITIAL_WINDOW)

    @property
    def pacing_gain(self) -> float:
        if self.state == "startup":
            return STARTUP_GAIN
        if self.state == "drain":
            return 1 / STARTUP_GAIN
        if self.state == "probe_bw":
            return PROBE_GAINS[self.cycle]
        return 1.0

    def bdp(self) -> float:
        return self.bw * self.min_rtt

    def pacing_rate(self, srtt: float) -> float:
        # Floor: a rate measured while there was little to send (a chat reply)
        # must never hold back what comes next.
        floor = (STARTUP_GAIN if self.state == "startup" else 1.0) * INITIAL_WINDOW
        return max(self.pacing_gain * self.bw, floor / max(srtt, 0.001))

    def on_send(self, p: _Sent, now: float, in_flight: int) -> None:
        if in_flight == 0:  # (re)starting from idle: rate intervals start now
            self.first_sent_at = self.delivered_at = now
        p.delivered = self.delivered
        p.delivered_at = self.delivered_at
        p.first_sent_at = self.first_sent_at
        p.app_limited = self.app_limited_until > 0

    def note_app_limited(self, in_flight: int) -> None:
        """Nothing to send while the window has room: samples taken now show
        how fast the app writes, not how fast the path is."""
        self.app_limited_until = max(self.delivered + in_flight, 1)

    def on_loss(self, size: int) -> None:
        self.round_lost += size

    def on_ack(
        self, acked: list[_Sent], newest: _Sent, now: float, rtt: float, in_flight: int
    ) -> None:
        acked_bytes = 0
        for p in acked:
            acked_bytes += p.size
        self.delivered += acked_bytes
        self.delivered_at = now
        if self.app_limited_until and self.delivered > self.app_limited_until:
            self.app_limited_until = 0
        # Round trips are counted in deliveries: a round ends when a packet
        # sent after the previous round ended is acknowledged.
        round_start = False
        if newest.delivered >= self.next_round_delivered:
            round_lost = self.round_lost
            round_bytes = self.delivered - self.round_delivered_start
            self.next_round_delivered = self.delivered
            self.round_delivered_start = self.delivered
            self.round_lost = 0
            self.round += 1
            round_start = True
            self._on_round_loss(round_lost, round_bytes)
        # Delivery rate over the interval this packet's flight took.
        self.first_sent_at = newest.at
        interval = max(newest.at - newest.first_sent_at, now - newest.delivered_at)
        if interval > 0 and (not self.min_rtt or interval >= self.min_rtt):
            rate = (self.delivered - newest.delivered) / interval
            if not newest.app_limited or rate >= self.bw:
                if self.bw_by_round and self.bw_by_round[-1][0] == self.round:
                    if rate > self.bw_by_round[-1][1]:
                        self.bw_by_round[-1] = (self.round, rate)
                else:
                    self.bw_by_round.append((self.round, rate))
                self.bw_by_round = [r for r in self.bw_by_round if r[0] > self.round - BW_ROUNDS]
                self.bw = max(r[1] for r in self.bw_by_round)
        # Shortest round trip (it expires, so a longer path is noticed).
        expired = now - self.min_rtt_at > MIN_RTT_WINDOW
        if rtt > 0 and (not self.min_rtt or rtt <= self.min_rtt or expired):
            self.min_rtt, self.min_rtt_at = rtt, now
        if self.state == "startup" and self._startup_overshoots(rtt):
            self.state = "drain"
            self.full_bw = self.bw
        self._advance(now, in_flight, round_start, expired)
        self._set_cwnd(acked_bytes)

    def _startup_overshoots(self, rtt: float) -> bool:
        """Leave startup as soon as a queue clearly builds (round trip far
        above the minimum while the rate stopped rising) or losses pile up
        within the round, instead of overfilling the buffer for three more
        round trips (each one doubles what's in flight)."""
        if not self.min_rtt or not self.bw:
            return False
        queued = rtt > 2 * self.min_rtt + 0.02 and self.full_bw_rounds >= 1
        in_round = self.delivered - self.round_delivered_start
        lossy = self.round_lost >= 20 * self.mss and self.round_lost > 0.05 * (in_round or 1)
        return queued or lossy

    def _on_round_loss(self, lost: int, delivered: int) -> None:
        total = lost + delivered
        if lost < 10 * self.mss or not total:
            return
        rate = lost / total
        if self.state == "startup" and rate > 0.1:
            self.state = "drain"  # the queue overflowed: we found the limit
            self.full_bw = self.bw
        elif rate > 0.2 and self.bw:
            # Heavy loss even at our measured rate: a policer or a small
            # buffer. Probe from a lower rate.
            self.bw_by_round = [(r, b * 0.85) for r, b in self.bw_by_round]
            self.bw *= 0.85

    def _advance(self, now: float, in_flight: int, round_start: bool, expired: bool) -> None:
        if self.state == "startup" and round_start:
            if self.bw >= self.full_bw * 1.25:
                self.full_bw, self.full_bw_rounds = self.bw, 0
            else:
                self.full_bw_rounds += 1
                if self.full_bw_rounds >= 3:
                    self.state = "drain"
        if self.state == "drain" and in_flight <= self.bdp():
            self._enter_probe_bw(now)
        if self.state == "probe_bw":
            gain = PROBE_GAINS[self.cycle]
            elapsed = now - self.cycle_at > self.min_rtt
            if (gain == 0.75 and in_flight <= self.bdp()) or (
                elapsed and (gain != 0.75 or in_flight <= self.bdp() * 1.1)
            ):
                self.cycle = (self.cycle + 1) % len(PROBE_GAINS)
                self.cycle_at = now
        if expired and self.state in ("probe_bw", "drain") and self.min_rtt:
            self.state, self.probe_rtt_until = "probe_rtt", 0.0
        if self.state == "probe_rtt":
            target = max(self.bdp() / 2, 4.0 * self.mss)
            if not self.probe_rtt_until and in_flight <= target:
                self.probe_rtt_until = now + max(PROBE_RTT_TIME, self.min_rtt)
            elif self.probe_rtt_until and now >= self.probe_rtt_until:
                self.min_rtt_at = now
                self._enter_probe_bw(now)

    def _enter_probe_bw(self, now: float) -> None:
        self.state = "probe_bw"
        self.cycle = secrets.choice((0, 2, 3, 4, 5, 6, 7))  # (not the drain phase)
        self.cycle_at = now

    def _set_cwnd(self, acked: int) -> None:
        floor = 4.0 * self.mss
        if self.state == "probe_rtt":
            self.cwnd = max(floor, min(self.cwnd, self.bdp() / 2))
            return
        gain = STARTUP_GAIN if self.state == "startup" else CWND_GAIN
        target = gain * self.bdp() + 3 * self.mss if self.bw and self.min_rtt else 0.0
        if self.state == "startup" or not target:
            self.cwnd += acked  # grow with every delivery until the rate stops rising
        else:
            self.cwnd = min(self.cwnd + acked, target)
        self.cwnd = min(max(self.cwnd, floor), float(MAX_WINDOW))


@dataclass
class _PunchState:
    candidates: list[tuple[str, int]]
    deadline: float
    done: asyncio.Future[bool] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )


class UdpConnection:
    """One encrypted, reliable connection to a contact over UDP. Same
    surface as :class:`srltcp.transports.base.Connection` so the TCP
    transport can hold it like any other connection."""

    def __init__(
        self,
        endpoint: UdpEndpoint,
        my_id: bytes,
        peer_conn_id: bytes,
        send_key: bytes,
        recv_key: bytes,
    ) -> None:
        self.endpoint = endpoint
        # The sockets this connection lives on (the endpoint may move new
        # punches to a fresh port; this connection keeps its own).
        self.socks = dict(endpoint._socks)
        self.my_id = my_id
        self.peer_conn_id = peer_conn_id
        self._seal = AESGCM(send_key)
        self._open = AESGCM(recv_key)
        self.peer = TransportPeer(peer_id=str(uuid.uuid4()), address="udp", transport="tcp")
        self.writer = None  # (TCP connections have one; nothing uses it for UDP)
        self.remote: tuple[str, int] | None = None
        self.expected_hash: str | None = None  # the contact this connection is for
        self._challenges: dict[tuple[str, int], tuple[bytes, float]] = {}
        self._rx_addr: tuple[str, int] | None = None
        # Optimistic-ack check: we skip a packet number now and then; a peer
        # acknowledging one claims data it never got (to make us send faster).
        self._skipped: set[int] = set()
        self._next_skip = SKIP_MIN + secrets.randbelow(SKIP_SPREAD)
        self._remote_rx = 0.0
        self.punch: _PunchState | None = None
        # Birthday punch (one side behind a strict NAT that picks a new port
        # for every destination): that side probes from many extra sockets,
        # the other probes many random ports of its address.
        self.spray: list[asyncio.DatagramTransport] = []
        self.spray_hosts: list[str] = []
        self._adopted: asyncio.DatagramTransport | None = None  # (ours to close)
        self.opened = asyncio.Event()
        self._closed = False
        self._on_frame: Callable[[TransportPeer, bytes], Awaitable[None]] | None = None
        self._on_close: Callable[[TransportPeer], Awaitable[None]] | None = None
        self._on_datagram: Callable[[TransportPeer, bytes], None] | None = None
        loop = asyncio.get_running_loop()
        self._loop = loop
        now = time.monotonic()
        # sending
        self._pn = 0
        # Send buffer: the frames as written (no copying into one big buffer),
        # from the first byte the peer hasn't confirmed up to _send_end.
        self._segs: list[bytes] = []
        self._seg_at: list[int] = []  # stream offset of each segment
        self._seg_head = 0  # segments before this one are confirmed and freed
        # Frames written but not started yet: they get their place in the
        # stream when their first byte is sent, small ones first.
        self._urgent: collections.deque[bytes] = collections.deque()
        self._bulk: collections.deque[bytes] = collections.deque()
        self._queued = 0  # bytes in _urgent and _bulk
        self._send_end = 0
        self._snext = 0  # next new byte to send
        self._peer_cum = 0  # peer has every byte below this
        self._peer_max = RECV_WINDOW  # flow control: don't send at or past this
        self._retx: list[tuple[int, int]] = []
        self._sent: dict[int, _Sent] = {}
        self._in_flight = 0
        self._largest_acked = -1
        self._cc = _Bbr(MIN_DATAGRAM, now)
        self._cwnd: float = self._cc.cwnd
        self._rtt = _Rtt()
        self._pto_count = 0
        self._last_eliciting_sent = 0.0
        self._last_sent = now
        self._tokens = float(INITIAL_WINDOW)
        self._tokens_at = now
        self._max_datagram = MIN_DATAGRAM
        self._pmtu_probed = False
        self._dgrams: list[bytes] = []
        self._writable = asyncio.Event()
        self._writable.set()
        self._timer: asyncio.TimerHandle | None = None
        self._timer_at = 0.0
        self._pump_soon = False
        self._tasks: set[asyncio.Future[None]] = set()
        # receiving
        self._rx_ranges: list[list[int]] = []  # received packet numbers, newest range first
        self._rx_highest = -1
        self._rx_highest_at = now
        self._ack_pending = 0  # ack-eliciting packets not yet acknowledged
        self._ack_due = 0.0
        self._rcum = 0  # stream bytes received in order
        self._ooo: dict[int, bytes] = {}
        self._ooo_keys: list[int] = []  # heap of the offsets in _ooo (may hold stale ones)
        self._ooo_bytes = 0
        self._app = bytearray()  # in order, not yet handed to the app
        self._app_event = asyncio.Event()
        self._consumed = 0
        self._max_sent = RECV_WINDOW  # the window we last advertised
        self._frames = FrameReader()
        self._reader: asyncio.Task[None] | None = None
        self._last_rx = now
        self.stats = {"sent": 0, "received": 0, "lost": 0, "retransmitted": 0}

    # ── Connection surface (used by TCPTransport) ──

    def set_frame_handler(self, handler: Callable[[TransportPeer, bytes], Awaitable[None]]) -> None:
        self._on_frame = handler

    def set_close_handler(self, handler: Callable[[TransportPeer], Awaitable[None]] | None) -> None:
        self._on_close = handler

    def set_datagram_handler(self, handler: Callable[[TransportPeer, bytes], None]) -> None:
        self._on_datagram = handler

    async def start_reading(self) -> None:
        if self._reader is None:
            self._reader = asyncio.create_task(self._read_loop())

    async def send(self, payload: bytes) -> None:
        """Messages (chat, acknowledgements, call signalling...): they go out
        before queued file data, so a message never waits behind a video.
        (The session layer accepts messages a little out of order, and a
        file's completion arriving before its last data is fine.)"""
        from srltcp.core.protocol.framing import FrameWriter

        await self._queue(FrameWriter.write(payload), urgent=True)

    async def send_framed(self, frame: bytes) -> None:
        """File data (framed in a worker thread)."""
        await self._queue(frame, urgent=False)

    async def _queue(self, frame: bytes, *, urgent: bool) -> None:
        if self._closed:
            raise ConnectionError("connection closed")
        while not self._room(urgent):
            self._writable.clear()
            await self._writable.wait()
            if self._closed:
                raise ConnectionError("connection closed")
        (self._urgent if urgent else self._bulk).append(frame)
        self._queued += len(frame)
        self._schedule_pump()

    def send_datagram(self, payload: bytes) -> bool:
        """Unreliable, unordered (call media). False if it doesn't fit."""
        if self._closed or len(payload) > self._max_datagram - HEADER.size - TAG - DGRAM_HDR.size:
            return False
        if len(self._dgrams) < 256:
            self._dgrams.append(payload)
            self._schedule_pump()
        return True

    def buffered(self) -> int:
        return self._send_end - self._snext + self._queued

    def _room(self, urgent: bool = False) -> bool:
        held = self._send_end - (
            self._seg_at[self._seg_head] if self._seg_head < len(self._segs) else self._send_end
        )
        if held + self._queued >= SEND_MAX:
            return False
        return urgent or self.buffered() < SEND_HIGH

    def _commit_next(self) -> bool:
        """Give the next queued frame (small ones first) its place in the stream."""
        queue = self._urgent or self._bulk
        if not queue:
            return False
        frame = queue.popleft()
        self._queued -= len(frame)
        self._segs.append(frame)
        self._seg_at.append(self._send_end)
        self._send_end += len(frame)
        return True

    def _read(self, off: int, ln: int) -> bytes:
        i = bisect.bisect_right(self._seg_at, off, self._seg_head) - 1
        seg_off = off - self._seg_at[i]
        seg = self._segs[i]
        if seg_off + ln <= len(seg):
            return seg[seg_off : seg_off + ln]
        parts = []
        while ln > 0:
            seg = self._segs[i]
            piece = seg[seg_off : seg_off + ln]
            parts.append(piece)
            ln -= len(piece)
            i += 1
            seg_off = 0
        return b"".join(parts)

    def tune_for_lane(self, high: int) -> None:  # Tor lanes only
        return

    @property
    def address(self) -> str:
        if not self.remote:
            return "udp"
        host, port = self.remote
        return f"udp:[{host}]:{port}" if ":" in host else f"udp:{host}:{port}"

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.remote and self.opened.is_set():
            with contextlib.suppress(OSError):
                for _ in range(2):
                    self._send_packet(bytes([F_CLOSE]), eliciting=False)
        self._shutdown()
        if self._reader and self._reader is not asyncio.current_task():
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
        if self._on_close:
            handler, self._on_close = self._on_close, None
            await handler(self.peer)

    def _close_spray(self, keep: asyncio.DatagramTransport | None = None) -> None:
        for transport in self.spray:
            if transport is not keep:
                transport.close()
        self.spray = []
        self.spray_hosts = []

    def _shutdown(self) -> None:
        self._close_spray()
        if self._adopted is not None:
            self._adopted.close()
            self._adopted = None
        self._closed = True
        if self._timer:
            self._timer.cancel()
        self._writable.set()
        self._app_event.set()
        self.endpoint._forget(self)
        if self.punch and not self.punch.done.done():
            self.punch.done.set_result(False)

    def _abort(self, why: str, *, tell: bool = True) -> None:
        if self._closed:
            return
        log.info("UDP connection %s closed: %s", self.peer.peer_id[:8], why)
        if tell and self.remote and self.opened.is_set():
            # Both sides drop it together (never a half-open link one side
            # still trusts while the other looks for another way).
            with contextlib.suppress(OSError):
                self._send_packet(bytes([F_CLOSE]), eliciting=False)
        self._shutdown()
        if self._on_close:
            handler, self._on_close = self._on_close, None
            self._spawn(handler(self.peer))

    def _spawn(self, aw: Awaitable[None]) -> None:
        task = asyncio.ensure_future(aw)
        self._tasks.add(task)  # keep a reference until it's done
        task.add_done_callback(self._tasks.discard)

    # ── Receiving ──

    def on_datagram(
        self, header: bytes, pn: int, body: bytes, addr: tuple[str, int],
        via: asyncio.DatagramTransport | None = None,
    ) -> None:
        if self._closed:
            return
        if (
            pn <= self._rx_highest - REPLAY_SPAN
            or self._seen(pn)
            or (len(self._rx_ranges) >= 64 and pn < self._rx_ranges[-1][1])
        ):
            return  # replayed, or too old to tell
        try:
            plain = self._open.decrypt(_nonce(pn), body, header)
        except InvalidTag:
            return  # not from our contact: ignore silently
        now = time.monotonic()
        self._last_rx = now
        self._record_pn(pn, now)
        self.stats["received"] += 1
        if self.remote is None:
            self.remote = addr  # the punch: first authenticated packet fixes the path
            if via is not None and via in self.spray:
                # One of the extra sockets got through a strict NAT (the
                # birthday punch): the connection lives on that one now.
                self.socks = {**self.socks, socket.AF_INET: via}
                self._adopted = via
                _set_hops(via, socket.AF_INET, DEFAULT_HOPS)
            self._close_spray(keep=self.socks.get(socket.AF_INET))
        if addr == self.remote:
            self._remote_rx = now
        elif now - self._remote_rx > MIGRATE_AFTER:
            # Their address changed (new network)? Move only once the new
            # address answers a challenge: a packet "from" another address
            # can't redirect our traffic there.
            self._challenge(addr, now)
        self._rx_addr = addr
        if not self.opened.is_set():
            self.opened.set()
            if self.punch and not self.punch.done.done():
                self.punch.done.set_result(True)
        eliciting = self._parse(plain, now)
        if eliciting:
            self._ack_pending += 1
            out_of_order = pn != self._rx_highest
            if self._ack_pending >= ACK_EVERY or out_of_order:
                self._ack_due = now
            elif not self._ack_due:
                self._ack_due = now + MAX_ACK_DELAY
        self._schedule_pump()

    def _challenge(self, addr: tuple[str, int], now: float) -> None:
        sent = self._challenges.get(addr)
        if sent and now - sent[1] < 0.5:
            return
        if len(self._challenges) >= 8:
            self._challenges.clear()
        token = secrets.token_bytes(8)
        self._challenges[addr] = (token, now)
        self._send_packet(
            bytes([F_PATH_CHALLENGE]) + token, eliciting=False, with_ack=False, to=addr
        )

    def _seen(self, pn: int) -> bool:
        for hi, lo in self._rx_ranges:
            if pn > hi:
                return False
            if pn >= lo:
                return True
        return False

    def _record_pn(self, pn: int, now: float) -> None:
        ranges = self._rx_ranges
        if pn > self._rx_highest:
            self._rx_highest, self._rx_highest_at = pn, now
        for i, (hi, lo) in enumerate(ranges):
            if pn == hi + 1:
                ranges[i][0] = pn
                if i > 0 and ranges[i - 1][1] == pn + 1:
                    ranges[i - 1][1] = ranges[i][1]
                    del ranges[i]
                return
            if pn == lo - 1:
                ranges[i][1] = pn
                if i + 1 < len(ranges) and ranges[i + 1][0] == pn - 1:
                    ranges[i][1] = ranges[i + 1][1]
                    del ranges[i + 1]
                return
            if pn > hi:
                ranges.insert(i, [pn, pn])
                del ranges[64:]
                return
        ranges.append([pn, pn])
        del ranges[64:]

    def _parse(self, plain: bytes, now: float) -> bool:
        eliciting = False
        i, n = 0, len(plain)
        view = memoryview(plain)
        try:
            while i < n:
                kind = plain[i]
                if kind == F_PAD:
                    i += 1
                    continue
                if kind == F_STREAM:
                    _k, off, ln = STREAM_HDR.unpack_from(plain, i)
                    i += STREAM_HDR.size
                    if i + ln > n:
                        return eliciting
                    self._on_stream(off, bytes(view[i : i + ln]))
                    i += ln
                    eliciting = True
                elif kind == F_ACK:
                    _k, cum, max_data, delay_us, count = ACK_HDR.unpack_from(plain, i)
                    i += ACK_HDR.size
                    if count > MAX_ACK_RANGES or i + count * ACK_RANGE.size > n:
                        return eliciting
                    ranges = [
                        ACK_RANGE.unpack_from(plain, i + k * ACK_RANGE.size) for k in range(count)
                    ]
                    i += count * ACK_RANGE.size
                    self._on_ack(cum, max_data, delay_us / 1e6, ranges, now)
                elif kind == F_DGRAM:
                    _k, ln = DGRAM_HDR.unpack_from(plain, i)
                    i += DGRAM_HDR.size
                    if i + ln > n:
                        return eliciting
                    self._on_dgram(bytes(view[i : i + ln]))
                    i += ln
                    eliciting = True
                elif kind == F_PING:
                    i += 1
                    eliciting = True
                elif kind in (F_PATH_CHALLENGE, F_PATH_RESPONSE):
                    token = plain[i + 1 : i + 9]
                    i += 9
                    if len(token) != 8 or self._rx_addr is None:
                        return eliciting
                    if kind == F_PATH_CHALLENGE:  # answer to wherever it came from
                        self._send_packet(
                            bytes([F_PATH_RESPONSE]) + token,
                            eliciting=False,
                            with_ack=False,
                            to=self._rx_addr,
                        )
                    else:
                        sent = self._challenges.get(self._rx_addr)
                        if sent and hmac.compare_digest(sent[0], token):
                            log.info(
                                "UDP path to %s moved to %s",
                                self.peer.peer_id[:8],
                                self._rx_addr[0],
                            )
                            self.remote, self._remote_rx = self._rx_addr, now
                            self._challenges.clear()
                            # A new network: its rate and delay are unknown.
                            self._cc = _Bbr(self._max_datagram, now)
                            self._cwnd = self._cc.cwnd
                            self._rtt = _Rtt()
                elif kind == F_CLOSE:
                    self._abort("closed by the contact", tell=False)
                    return False
                else:
                    return eliciting  # unknown frame: ignore the rest
        except struct.error:
            return eliciting
        return eliciting

    def _on_stream(self, off: int, data: bytes) -> None:
        end = off + len(data)
        if end <= self._rcum:
            self._ack_due = time.monotonic()  # a retransmission: confirm again now
            return
        if end > self._max_sent:
            return  # beyond the window we offered: drop (a well-behaved sender never does this)
        if off > self._rcum:
            # Early data waits for the gap. Bounded in bytes and in pieces, so
            # not even the contact can make us hold more than the window (or
            # millions of tiny overlapping pieces).
            if (
                off not in self._ooo
                and self._ooo_bytes + len(data) <= RECV_WINDOW
                and len(self._ooo) < MAX_OOO_PIECES
            ):
                self._ooo[off] = data
                self._ooo_bytes += len(data)
                heapq.heappush(self._ooo_keys, off)
            return
        if off < self._rcum:
            data = data[self._rcum - off :]
        self._app += data
        self._rcum += len(data)
        while self._ooo:
            block = self._ooo.pop(self._rcum, None)
            if block is not None:
                self._ooo_bytes -= len(block)
                self._app += block
                self._rcum += len(block)
                continue
            # Pieces that start behind us now: keep only their new part.
            # (The heap finds them without scanning every piece.)
            moved = False
            keys = self._ooo_keys
            while keys and keys[0] < self._rcum:
                k = heapq.heappop(keys)
                block = self._ooo.pop(k, None)
                if block is None:
                    continue  # delivered already
                self._ooo_bytes -= len(block)
                if k + len(block) > self._rcum and self._rcum not in self._ooo:
                    self._ooo[self._rcum] = block[self._rcum - k :]
                    self._ooo_bytes += k + len(block) - self._rcum
                    heapq.heappush(keys, self._rcum)
                    moved = True
            if not moved:
                break
        if not self._ooo:
            self._ooo_keys.clear()
        self._app_event.set()

    def _on_dgram(self, data: bytes) -> None:
        if self._on_datagram:
            self._on_datagram(self.peer, data)

    async def _read_loop(self) -> None:
        try:
            while not self._closed:
                await self._app_event.wait()
                self._app_event.clear()
                if not self._app:
                    continue
                data, self._app = bytes(self._app), bytearray()
                for frame in self._frames.feed(data):
                    if self._on_frame:
                        await self._on_frame(self.peer, frame)
                self._consumed += len(data)
                if self._consumed + RECV_WINDOW - self._max_sent >= RECV_WINDOW // 4:
                    self._ack_due = time.monotonic()  # tell the sender it may send more
                    self._schedule_pump()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a bad frame closes the connection, like TCP
            log.warning("UDP connection %s: %r", self.peer.peer_id[:8], exc)
            self._abort("bad data")

    # ── Acknowledgements, loss and congestion ──

    def _on_ack(
        self, cum: int, max_data: int, delay: float, ranges: list[tuple[int, int]], now: float
    ) -> None:
        if cum > self._peer_cum:
            self._peer_cum = min(cum, self._snext)
            self._free_acked()
        if max_data > self._peer_max:
            self._peer_max = max_data
        if not ranges:
            return
        newly_largest = False
        acked: list[_Sent] = []
        newest: _Sent | None = None
        newest_pn = -1
        largest_info: _Sent | None = None
        lowest_out = next(iter(self._sent), None)
        for hi, lo in ranges:
            hi = min(hi, self._pn - 1)  # never trust an ack for a packet we didn't send
            if self._skipped and any(lo <= s <= hi for s in self._skipped):
                self._abort("acknowledged packets it never received")
                return
            if hi < lo or lowest_out is None:
                continue
            if hi > self._largest_acked:
                self._largest_acked = hi
                newly_largest = True
            bottom, top = max(lo, lowest_out), min(hi, self._pn - 1)
            numbers = (
                range(bottom, top + 1)
                if top - bottom <= len(self._sent)
                else [p for p in self._sent if bottom <= p <= top]
            )
            for pn in numbers:
                info = self._sent.pop(pn, None)
                if info is None:
                    continue
                self._in_flight -= info.size
                acked.append(info)
                if pn > newest_pn:
                    newest, newest_pn = info, pn
                if pn == hi and hi == self._largest_acked:
                    largest_info = info
                if info.probe_size:
                    self._max_datagram = max(self._max_datagram, info.probe_size)
                    self._cc.mss = self._max_datagram
                    log.debug(
                        "UDP %s: %d-byte packets get through",
                        self.peer.peer_id[:8],
                        info.probe_size,
                    )
        sample = 0.0
        if newly_largest and largest_info is not None and largest_info.eliciting:
            sample = now - largest_info.at
            self._rtt.update(sample, min(delay, MAX_ACK_DELAY * 4))
        if acked and newest is not None:
            self._pto_count = 0
            self._cc.on_ack(acked, newest, now, sample, self._in_flight)
            self._cwnd = self._cc.cwnd
        if self._skipped and min(self._skipped) < self._largest_acked - 4096:
            # Long acknowledged around: they had their chance to lie about it.
            self._skipped = {s for s in self._skipped if s >= self._largest_acked - 4096}
        self._detect_loss(now)
        self._maybe_writable()

    def _free_acked(self) -> None:
        head, segs, at = self._seg_head, self._segs, self._seg_at
        while head < len(segs) and at[head] + len(segs[head]) <= self._peer_cum:
            segs[head] = b""  # free the memory now
            head += 1
        if head > 256:
            del segs[:head], at[:head]
            head = 0
        self._seg_head = head
        self._retx = [(o, ln) for o, ln in self._retx if o + ln > self._peer_cum]

    def _loss_delay(self) -> float:
        return max(LOSS_TIME * max(self._rtt.srtt, self._rtt.latest), 0.002)

    def _detect_loss(self, now: float) -> None:
        if self._largest_acked < 0:
            return
        limit = now - self._loss_delay()
        lost: list[int] = []
        for pn, info in self._sent.items():
            if pn >= self._largest_acked:
                break
            if self._largest_acked - pn >= LOSS_REORDER or info.at <= limit:
                lost.append(pn)
            else:
                break
        if not lost:
            return
        for pn in lost:
            info = self._sent.pop(pn)
            self._in_flight -= info.size
            if info.probe_size:
                continue  # a too-big probe: just means big packets don't fit
            self.stats["lost"] += 1
            self._cc.on_loss(info.size)
            for off, ln in info.ranges:
                if off + ln > self._peer_cum:
                    self._retx.append((off, ln))
        self._retx.sort()

    def _on_timer(self) -> None:
        self._timer = None
        if self._closed:
            return
        now = time.monotonic()
        if now - self._last_rx > IDLE_TIMEOUT and self.opened.is_set():
            self._abort("no answer from the contact", tell=False)
            return
        self._detect_loss(now)
        pto = self._pto_deadline()
        if pto and now >= pto:
            self._pto_count += 1
            self._probe_timeout()
        if self.opened.is_set() and now - self._last_sent >= KEEPALIVE:
            self._send_packet(bytes([F_PING]), eliciting=True)
        self._pump()

    def _pto_deadline(self) -> float:
        if not self._sent:  # (only ack-eliciting packets are tracked)
            return 0.0
        rtt = self._rtt
        period = rtt.srtt + max(4 * rtt.rttvar, 0.002) + MAX_ACK_DELAY
        return float(self._last_eliciting_sent + period * (1 << min(self._pto_count, 6)))

    def _probe_timeout(self) -> None:
        """No acknowledgement for too long: resend the oldest data now (or a
        ping), without waiting for the window."""
        oldest = next((i for i in self._sent.values() if i.ranges), None)
        if oldest:
            for off, ln in oldest.ranges:
                if off + ln > self._peer_cum and (off, ln) not in self._retx:
                    self._retx.insert(0, (off, ln))
            self._send_one(force=True)
        else:
            self._send_packet(bytes([F_PING]), eliciting=True)

    # ── Sending ──

    def _schedule_pump(self) -> None:
        if not self._pump_soon and not self._closed:
            self._pump_soon = True
            self._loop.call_soon(self._pump)

    def _pump(self) -> None:
        self._pump_soon = False
        if self._closed or self.remote is None:
            self._arm_timer(time.monotonic())
            return
        now = time.monotonic()
        rate = self._cc.pacing_rate(self._rtt.srtt)
        self._tokens = min(
            self._tokens + (now - self._tokens_at) * rate,
            max(10 * self._max_datagram, rate * 0.003),
        )
        self._tokens_at = now
        sent_any = True
        while sent_any:
            if self._tokens < self._max_datagram and self._has_data():
                break  # paced: continue when enough budget built up
            sent_any = self._send_one()
        if not self._has_data() and self._in_flight + self._max_datagram <= self._cwnd:
            self._cc.note_app_limited(self._in_flight)
        if self._ack_due and now >= self._ack_due:
            self._send_packet(b"", eliciting=False)  # an acknowledgement on its own
        if self.opened.is_set() and not self._pmtu_probed and self._rtt.sampled:
            self._pmtu_probed = True
            self._send_packet(bytes([F_PING]), eliciting=True, pad_to=BIG_DATAGRAM)
        self._arm_timer(now)

    def _has_data(self) -> bool:
        return bool(self._retx or self._dgrams or self._snext < self._send_end or self._queued)

    def _send_one(self, *, force: bool = False) -> bool:
        """One packet of data (retransmissions first, then new bytes) if the
        window allows. Returns whether something went out."""
        if not self._has_data():
            return False
        if not force and self._in_flight + self._max_datagram > self._cwnd and not self._dgrams:
            return False
        room = self._max_datagram - HEADER.size - TAG
        parts: list[bytes] = []
        if self._dgrams:
            # Call media first, in packets of their own: never behind file data.
            while self._dgrams and room >= DGRAM_HDR.size + len(self._dgrams[0]):
                d = self._dgrams.pop(0)
                parts.append(DGRAM_HDR.pack(F_DGRAM, len(d)) + d)
                room -= DGRAM_HDR.size + len(d)
            if not parts:  # (the path's packets got smaller): can't go
                self._dgrams.pop(0)
                return True
            self._send_packet(b"".join(parts), eliciting=True, with_ack=False)
            return True
        ack = self._ack_frame() if self._ack_due else b""
        if ack:
            parts.append(ack)
            room -= len(ack)
        ranges: list[tuple[int, int]] = []
        while room > STREAM_HDR.size + 16:
            piece = self._next_range(room - STREAM_HDR.size)
            if not piece:
                break
            off, ln = piece
            parts.append(STREAM_HDR.pack(F_STREAM, off, ln) + self._read(off, ln))
            ranges.append(piece)
            room -= STREAM_HDR.size + ln
        if not ranges and len(parts) == (1 if ack else 0):
            return False
        self._send_packet(b"".join(parts), eliciting=True, ranges=ranges, with_ack=False)
        if ack:
            self._ack_sent()
        return True

    def _next_range(self, room: int) -> tuple[int, int] | None:
        while self._retx:
            off, ln = self._retx[0]
            if off + ln <= self._peer_cum:
                self._retx.pop(0)
                continue
            if off < self._peer_cum:
                ln -= self._peer_cum - off
                off = self._peer_cum
            take = min(ln, room)
            if take == ln:
                self._retx.pop(0)
            else:
                self._retx[0] = (off + take, ln - take)
            self.stats["retransmitted"] += 1
            return off, take
        if self._snext >= self._send_end and not self._commit_next():
            return None
        limit = min(self._send_end, self._peer_max)
        if self._snext >= limit:
            return None
        take = min(room, limit - self._snext)
        off = self._snext
        self._snext += take
        self._maybe_writable()
        return off, take

    def _maybe_writable(self) -> None:
        if self._room():
            self._writable.set()

    def _ack_frame(self) -> bytes:
        ranges = self._rx_ranges[:MAX_ACK_RANGES]
        self._max_sent = max(self._max_sent, self._consumed + RECV_WINDOW)
        delay = max(0.0, time.monotonic() - self._rx_highest_at)
        return ACK_HDR.pack(
            F_ACK, self._rcum, self._max_sent, min(int(delay * 1e6), 0xFFFFFFFF), len(ranges)
        ) + b"".join(ACK_RANGE.pack(hi, lo) for hi, lo in ranges)

    def _ack_sent(self) -> None:
        self._ack_pending = 0
        self._ack_due = 0.0

    def _send_packet(
        self,
        payload: bytes,
        *,
        eliciting: bool,
        ranges: list[tuple[int, int]] | None = None,
        with_ack: bool = True,
        pad_to: int = 0,
        to: tuple[str, int] | None = None,
    ) -> None:
        if self.remote is None and self.punch is None and to is None:
            return
        if with_ack and self._rx_ranges:
            ack = self._ack_frame()
            payload = ack + payload
        elif not payload:
            return
        if self._pn >= self._next_skip:
            self._skipped.add(self._pn)  # never sent: must never be acknowledged
            self._pn += 1
            self._next_skip = self._pn + SKIP_MIN + secrets.randbelow(SKIP_SPREAD)
        pn = self._pn
        self._pn += 1
        header = HEADER.pack(MAGIC, VERSION, self.peer_conn_id, pn)
        if pad_to:
            payload += bytes(max(0, pad_to - HEADER.size - TAG - len(payload)))
        packet = header + self._seal.encrypt(_nonce(pn), payload, header)
        now = time.monotonic()
        if to is not None:
            targets = [to]
        elif self.remote:
            targets = [self.remote]
        else:
            targets = self.punch.candidates if self.punch else []
        for addr in targets:
            self.endpoint.sendto(packet, addr, self.socks)
        self.stats["sent"] += 1
        self._last_sent = now
        if with_ack and self._rx_ranges:
            self._ack_sent()
        size = len(packet)
        if eliciting or ranges:
            # (Acknowledgements aren't paced: they'd starve our own data.)
            self._tokens = max(self._tokens - size, -10.0 * self._max_datagram)
            info = _Sent(now, size, ranges or [], True, pad_to)
            self._cc.on_send(info, now, self._in_flight)
            self._sent[pn] = info
            self._in_flight += size
            self._last_eliciting_sent = now

    def _arm_timer(self, now: float) -> None:
        if self._closed:
            return
        due = [now + KEEPALIVE, self._last_rx + IDLE_TIMEOUT]
        if self._ack_due:
            due.append(self._ack_due)
        pto = self._pto_deadline()
        if pto:
            due.append(pto)
        if self._sent and self._largest_acked >= 0:
            first = next(iter(self._sent.values()))
            due.append(first.at + self._loss_delay())
        if self._has_data() and self.remote is not None:
            rate = self._cc.pacing_rate(self._rtt.srtt)
            if self._tokens < self._max_datagram:
                due.append(now + (self._max_datagram - self._tokens) / max(rate, 1.0))
        at = max(min(due), now + 0.0005)
        if self._timer and self._timer_at <= at + 0.0005:
            return
        if self._timer:
            self._timer.cancel()
        self._timer_at = at
        self._timer = self._loop.call_at(self._loop.time() + (at - now), self._on_timer)

    # ── Punching ──

    def _probe_packet(self) -> bytes:
        """One authenticated probe (any copy of it opens the connection)."""
        pn = self._pn
        self._pn += 1
        header = HEADER.pack(MAGIC, VERSION, self.peer_conn_id, pn)
        return header + self._seal.encrypt(_nonce(pn), bytes([F_PING]), header)

    def _spray_round(self, punch: _PunchState, rounds: int) -> None:
        """Birthday punch, one round. Behind the strict NAT: every extra
        socket sends to their address (each gets its own NAT port; every
        other round, to keep those mappings open). The other side: probes to
        a fresh batch of random ports of the strict side's address (each
        opens its own router for an answer from that port)."""
        packet = self._probe_packet()
        if self.spray and rounds % 2 == 0:
            v4 = [c for c in punch.candidates if ":" not in c[0]]
            for transport in self.spray:
                for addr in v4:
                    with contextlib.suppress(OSError):
                        transport.sendto(packet, addr)
        sock = self.socks.get(socket.AF_INET)
        if self.spray_hosts and sock is not None:
            for host in self.spray_hosts:
                for _ in range(SPRAY_PORTS_PER_ROUND):
                    with contextlib.suppress(OSError):
                        sock.sendto(packet, (host, 1024 + secrets.randbelow(64512)))

    def start_probing(self, candidates: list[tuple[str, int]], deadline: float) -> _PunchState:
        self.punch = _PunchState(candidates, deadline)
        self._spawn(self._probe_loop())
        return self.punch

    async def _probe_loop(self) -> None:
        punch = self.punch
        assert punch is not None
        rounds = 0
        while not self._closed and not self.opened.is_set() and time.monotonic() < punch.deadline:
            self._send_packet(bytes([F_PING]), eliciting=False, with_ack=False)
            if self.spray or self.spray_hosts:
                self._spray_round(punch, rounds)
            rounds += 1
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.opened.wait(), PROBE_EVERY)
        if self.opened.is_set():
            self._send_packet(bytes([F_PING]), eliciting=True)  # tell them which path we took
        elif not punch.done.done():
            punch.done.set_result(False)


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, endpoint: UdpEndpoint) -> None:
        self.endpoint = endpoint
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.endpoint._received(data, (str(addr[0]), int(addr[1])), self.transport)

    def error_received(self, exc: Exception) -> None:
        log.debug("UDP socket error: %r", exc)


def _set_hops(transport: asyncio.DatagramTransport, family: int, limit: int) -> None:
    sock = transport.get_extra_info("socket")
    level, opt = (
        (socket.IPPROTO_IPV6, socket.IPV6_UNICAST_HOPS)
        if family == socket.AF_INET6
        else (socket.IPPROTO_IP, socket.IP_TTL)
    )
    with contextlib.suppress(OSError, AttributeError):
        sock.setsockopt(level, opt, limit)


def _port_of(transport: asyncio.DatagramTransport) -> int:
    return int(transport.get_extra_info("sockname")[1])


class UdpEndpoint:
    """This device's UDP sockets (IPv4 and IPv6, same port), the STUN
    client and all UDP connections."""

    def __init__(
        self, port: int = 0, stun_servers: tuple[tuple[str, int], ...] = STUN_SERVERS
    ) -> None:
        self.requested_port = port
        self.port = 0
        self.stun_servers = stun_servers
        self._socks: dict[int, asyncio.DatagramTransport] = {}  # for new punches and STUN
        self._retired: list[dict[int, asyncio.DatagramTransport]] = []  # still used by connections
        self.ports: dict[int, int] = {}
        self._conns: dict[bytes, UdpConnection] = {}
        self._stun_waiting: dict[bytes, asyncio.Future[tuple[str, int]]] = {}
        self.mapped: dict[int, tuple[str, int]] = {}  # family -> public address:port
        self.mapped_at = 0.0
        self.symmetric = False  # different ports towards different servers: punching rarely works
        self.stun_answers: dict[int, int] = {}  # family -> how many STUN servers answered

    async def start(self) -> None:
        self._socks = await self._open(self.requested_port)
        self.ports = {f: _port_of(t) for f, t in self._socks.items()}
        self.port = next(iter(self.ports.values()), 0)
        if self._socks:
            log.info("Direct UDP connections on port %d", self.port)

    async def rotate(self) -> bool:
        """Move new punches to a fresh local port (and learn its public
        mapping). After a failed round, routers on the way may hold stale
        state for the old pair of ports for ~30 s, refreshed by every retry:
        a new port is a new pair, so the next round can work at once.
        Existing connections keep their sockets."""
        fresh = await self._open(0)
        if not fresh:
            return False
        self._retired.append(self._socks)
        self._socks = fresh
        self.ports = {f: _port_of(t) for f, t in fresh.items()}
        self._close_unused()
        self.mapped, self.mapped_at = {}, 0.0
        await self.refresh_mapping()
        log.info("UDP: new punches from port %s", ", ".join(map(str, self.ports.values())))
        return True

    def _close_unused(self) -> None:
        used = {id(t) for c in self._conns.values() for t in c.socks.values()}
        keep = []
        for socks in self._retired:
            if any(id(t) in used for t in socks.values()):
                keep.append(socks)
            else:
                for transport in socks.values():
                    transport.close()
        self._retired = keep

    async def _open(self, wanted: int) -> dict[int, asyncio.DatagramTransport]:
        """An IPv4 and an IPv6 socket on one port (``wanted``, else any)."""
        loop = asyncio.get_running_loop()
        out: dict[int, asyncio.DatagramTransport] = {}
        bound = 0
        for family, host in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
            try:
                sock = socket.socket(family, socket.SOCK_DGRAM)
            except OSError:
                continue  # no IPv6 (or IPv4) on this device
            try:
                if family == socket.AF_INET6:
                    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                with contextlib.suppress(OSError):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
                port = bound or wanted
                try:
                    sock.bind((host, port))
                except OSError:
                    if not port:
                        raise
                    sock.bind((host, 0))  # taken: any port works (candidates carry it)
                sock.setblocking(False)
            except OSError as exc:
                sock.close()
                log.debug(
                    "UDP %s not available: %s", "IPv6" if family == socket.AF_INET6 else "IPv4", exc
                )
                continue
            transport, _ = await loop.create_datagram_endpoint(lambda: _Protocol(self), sock=sock)
            out[family] = transport
            bound = bound or int(sock.getsockname()[1])
        return out

    def stop(self) -> None:
        for conn in list(self._conns.values()):
            conn._shutdown()
        for socks in (self._socks, *self._retired):
            for transport in socks.values():
                transport.close()
        self._socks.clear()
        self._retired.clear()

    @property
    def families(self) -> set[int]:
        return set(self._socks)

    def sendto(
        self,
        packet: bytes,
        addr: tuple[str, int],
        socks: dict[int, asyncio.DatagramTransport] | None = None,
    ) -> None:
        family = socket.AF_INET6 if ":" in addr[0] else socket.AF_INET
        sock = (self._socks if socks is None else socks).get(family)
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.sendto(packet, addr)

    def _forget(self, conn: UdpConnection) -> None:
        if self._conns.get(conn.my_id) is conn:
            del self._conns[conn.my_id]

    def _received(
        self, data: bytes, addr: tuple[str, int], via: asyncio.DatagramTransport | None = None
    ) -> None:
        if len(data) >= HEADER.size + TAG and data[:2] == MAGIC and data[2] == VERSION:
            _m, _v, conn_id, pn = HEADER.unpack_from(data)
            conn = self._conns.get(conn_id)
            if conn is not None:
                conn.on_datagram(data[: HEADER.size], pn, data[HEADER.size :], addr, via)
            return
        if len(data) >= 20 and data[0] == 0x01 and data[1] == 0x01:
            self._on_stun(data, addr)
        # Anything else: dropped without an answer.

    # ── Connections ──

    def new_connection(
        self, peer_conn_id: bytes, send_key: bytes, recv_key: bytes, my_id: bytes | None = None
    ) -> UdpConnection | None:
        if len(self._conns) >= MAX_CONNECTIONS:
            return None
        pending = sum(1 for c in self._conns.values() if not c.opened.is_set())
        if pending >= MAX_PENDING_PUNCHES:
            return None
        my_id = my_id or secrets.token_bytes(8)
        if my_id in self._conns:
            return None
        conn = UdpConnection(self, my_id, peer_conn_id, send_key, recv_key)
        self._conns[my_id] = conn
        return conn

    async def open_spray(self, count: int) -> list[asyncio.DatagramTransport]:
        """``count`` extra IPv4 sockets (any port) for a birthday punch."""
        loop = asyncio.get_running_loop()
        out: list[asyncio.DatagramTransport] = []
        for _ in range(count):
            try:
                transport, _ = await loop.create_datagram_endpoint(
                    lambda: _Protocol(self), local_addr=("0.0.0.0", 0)
                )
            except OSError:
                break  # (out of sockets: as many as we got)
            out.append(transport)
        return out

    def prime(
        self,
        candidates: list[tuple[str, int]],
        hops: tuple[int, ...] = PRIME_HOPS,
        socks: dict[int, asyncio.DatagramTransport] | None = None,
    ) -> None:
        """Open our NAT towards each candidate without reaching theirs."""
        for host, port in candidates:
            family = socket.AF_INET6 if ":" in host else socket.AF_INET
            transport = (self._socks if socks is None else socks).get(family)
            if transport is None or getattr(transport, "get_write_buffer_size", lambda: 0)():
                continue  # (queued packets would go out with the low hop limit)
            sock = transport.get_extra_info("socket")
            level, opt = (
                (socket.IPPROTO_IPV6, socket.IPV6_UNICAST_HOPS)
                if family == socket.AF_INET6
                else (socket.IPPROTO_IP, socket.IP_TTL)
            )
            try:
                for limit in hops:
                    sock.setsockopt(level, opt, limit)
                    transport.sendto(b"\0", (host, port))  # dropped by anyone it reaches
            except OSError:
                pass
            finally:
                with contextlib.suppress(OSError):
                    sock.setsockopt(level, opt, DEFAULT_HOPS)

    async def punch(
        self,
        conn: UdpConnection,
        candidates: list[tuple[str, int]],
        window: float,
        *,
        delay: float = 0.0,
        hops: tuple[int, ...] = PRIME_HOPS,
        spray_sockets: int = 0,
        spray_hosts: list[str] | None = None,
    ) -> bool:
        """Probe every candidate until one answers (authenticated) or the
        window closes. ``delay``: the answering side waits that long after
        priming, so its probes never reach the other NAT first.
        ``spray_sockets``: we're behind a strict NAT, probe from that many
        extra sockets too; ``spray_hosts``: they are, probe random ports of
        these addresses too (the birthday punch)."""
        if spray_sockets:
            conn.spray = await self.open_spray(spray_sockets)
            # Their probes are to find our extra sockets' NAT ports; ours must
            # not reach their router (a Linux-based router that saw a packet
            # from that port first gives its phone's probe to it another
            # port, and it misses). Low hop limits, spread like priming.
            for i, transport in enumerate(conn.spray):
                _set_hops(transport, socket.AF_INET, hops[i % len(hops)] if hops else DEFAULT_HOPS)
        conn.spray_hosts = list(spray_hosts or [])
        usable = [
            c
            for c in candidates
            if (socket.AF_INET6 if ":" in c[0] else socket.AF_INET) in conn.socks
        ]
        if not usable:
            conn._shutdown()
            return False
        self.prime(usable, hops, conn.socks)
        if delay:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(conn.opened.wait(), delay)
        if conn.opened.is_set():  # their probe already got through: answered by the ack
            return not conn._closed
        state = conn.start_probing(usable, time.monotonic() + window)
        ok = await state.done
        if not ok:
            conn._shutdown()
        return ok

    # ── STUN (RFC 5389 Binding) ──

    async def refresh_mapping(self, timeout: float = 1.5) -> dict[int, tuple[str, int]]:
        """Ask STUN servers for our public address:port (per family)."""
        if not self._socks:
            return {}
        loop = asyncio.get_running_loop()
        results: dict[int, list[tuple[str, int]]] = {}

        async def ask(family: int, server: tuple[str, int]) -> None:
            try:
                infos = await asyncio.wait_for(
                    loop.getaddrinfo(server[0], server[1], family=family, type=socket.SOCK_DGRAM),
                    timeout,
                )
            except (OSError, TimeoutError):
                return
            if not infos:
                return
            target = infos[0][4]
            txid = secrets.token_bytes(12)
            fut: asyncio.Future[tuple[str, int]] = loop.create_future()
            self._stun_waiting[txid] = fut
            request = struct.pack(">HHI", 0x0001, 0, STUN_MAGIC) + txid
            try:
                for _ in range(2):
                    self.sendto(request, (str(target[0]), int(target[1])))
                    with contextlib.suppress(TimeoutError):
                        mapped = await asyncio.wait_for(asyncio.shield(fut), timeout / 2)
                        results.setdefault(family, []).append(mapped)
                        return
            finally:
                self._stun_waiting.pop(txid, None)
                if not fut.done():
                    fut.cancel()

        await asyncio.gather(
            *(ask(family, server) for family in self._socks for server in self.stun_servers)
        )
        mapped: dict[int, tuple[str, int]] = {}
        symmetric = False
        for family, found in results.items():
            ports = {p for _h, p in found}
            symmetric = symmetric or len(ports) > 1
            mapped[family] = found[0]
        self.mapped, self.mapped_at, self.symmetric = mapped, time.monotonic(), symmetric
        self.stun_answers = {family: len(found) for family, found in results.items()}
        return mapped

    def _on_stun(self, data: bytes, addr: tuple[str, int]) -> None:
        msg_type, length, cookie = struct.unpack_from(">HHI", data)
        txid = data[8:20]
        fut = self._stun_waiting.get(txid)
        if fut is None or fut.done() or cookie != STUN_MAGIC or 20 + length > len(data):
            return
        i = 20
        while i + 4 <= 20 + length:
            atype, alen = struct.unpack_from(">HH", data, i)
            value = data[i + 4 : i + 4 + alen]
            i += 4 + alen + (-alen % 4)
            if atype == 0x0020 and len(value) >= 8:  # XOR-MAPPED-ADDRESS
                family = value[1]
                port = struct.unpack(">H", value[2:4])[0] ^ (STUN_MAGIC >> 16)
                if family == 0x01 and len(value) >= 8:
                    raw = bytes(
                        a ^ b
                        for a, b in zip(value[4:8], struct.pack(">I", STUN_MAGIC), strict=True)
                    )
                    fut.set_result((str(ipaddress.IPv4Address(raw)), port))
                    return
                if family == 0x02 and len(value) >= 20:
                    key = struct.pack(">I", STUN_MAGIC) + txid
                    raw = bytes(a ^ b for a, b in zip(value[4:20], key, strict=True))
                    fut.set_result((str(ipaddress.IPv6Address(raw)), port))
                    return


# Public DNS resolvers people mistake for STUN servers (they don't answer STUN).
_DNS_RESOLVERS = frozenset({
    "8.8.8.8", "8.8.4.4", "1.1.1.1", "1.0.0.1", "9.9.9.9", "149.112.112.112",
    "208.67.222.222", "208.67.220.220", "2001:4860:4860::8888", "2001:4860:4860::8844",
    "2606:4700:4700::1111", "2606:4700:4700::1001",
})


def parse_stun_servers(text: str) -> tuple[tuple[str, int], ...]:
    """``host:port`` entries separated by commas or spaces (port 3478 if
    left out, ``[v6]:port`` for IPv6 literals); empty = the defaults.
    Raises ValueError for anything that isn't a plain host name or address."""
    text = (text or "").strip()
    if not text:
        return STUN_SERVERS
    out: list[tuple[str, int]] = []
    for item in text.replace(",", " ").split():
        host, port = item, 3478
        if item.startswith("["):
            host, _, rest = item[1:].partition("]")
            if rest:
                if not rest.startswith(":"):
                    raise ValueError(f"bad STUN server: {item}")
                port = int(rest[1:])
            ipaddress.IPv6Address(host)
        elif item.count(":") == 1:
            host, port_text = item.split(":")
            if not port_text.isdigit():
                raise ValueError(f"bad STUN server: {item}")
            port = int(port_text)
        if not 0 < port < 65536 or not host or len(host) > 253:
            raise ValueError(f"bad STUN server: {item}")
        if not all(c.isalnum() or c in ".-:" for c in host):
            raise ValueError(f"bad STUN server: {item}")
        if host in _DNS_RESOLVERS:
            raise ValueError(
                f"{host} is a DNS server, not a STUN server — leave the field empty "
                "for the defaults"
            )
        out.append((host, port))
    if len(out) > 4:
        raise ValueError("at most 4 STUN servers")
    return tuple(out)


def new_punch_secret() -> bytes:
    return os.urandom(32)


def combine_secrets(offer: bytes, answer: bytes) -> bytes:
    return hmac.new(offer, answer, hashlib.sha256).digest()
