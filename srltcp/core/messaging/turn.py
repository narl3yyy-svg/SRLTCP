"""Local TURN relay that carries WebRTC call media over the SRLTCP session.

Why: WebRTC normally finds its own network path (ICE). Inside the Android
WebView local addresses are hidden behind mDNS names and the VPN interface may
not be offered, so over WireGuard calls ring but never connect. Instead each
node runs this minimal TURN server on 127.0.0.1 and the page uses it with
``iceTransportPolicy: "relay"``. The browser only ever talks to its own phone;
packets it sends to the peer are forwarded inside the existing end-to-end
encrypted session (``CALL_MEDIA`` messages) — over LAN, VPN or a hub alike.

Relayed addresses are virtual: every node gets a stable address in
100.64.0.0/10 derived from its identity hash, and the "relay port" is just an
allocation number. Nothing is bound on the network for media.

Implements the parts of RFC 5389 (STUN) / RFC 5766 (TURN) browsers use:
Binding, Allocate (long-term credentials), Refresh, CreatePermission,
ChannelBind, Send/Data indications and ChannelData, over UDP.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import os
import secrets
import struct
import time
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from srltcp.utils.logging import get_logger

log = get_logger(__name__)

MAGIC = 0x2112A442
REALM = "srltcp"
DEFAULT_LIFETIME = 600
CREDENTIAL_TTL = 6 * 3600

# Methods
BINDING, ALLOCATE, REFRESH, SEND, DATA, CREATE_PERMISSION, CHANNEL_BIND = (
    0x001, 0x003, 0x004, 0x006, 0x007, 0x008, 0x009,
)
# Classes
REQUEST, INDICATION, SUCCESS, ERROR = 0x000, 0x010, 0x100, 0x110

# Attributes
A_MAPPED_ADDRESS = 0x0001
A_USERNAME = 0x0006
A_MESSAGE_INTEGRITY = 0x0008
A_ERROR_CODE = 0x0009
A_CHANNEL_NUMBER = 0x000C
A_LIFETIME = 0x000D
A_XOR_PEER_ADDRESS = 0x0012
A_DATA = 0x0013
A_REALM = 0x0014
A_NONCE = 0x0015
A_XOR_RELAYED_ADDRESS = 0x0016
A_REQUESTED_TRANSPORT = 0x0019
A_XOR_MAPPED_ADDRESS = 0x0020
A_SOFTWARE = 0x8022
A_FINGERPRINT = 0x8028

VIRTUAL_NET = ipaddress.ip_network("100.64.0.0/10")


def virtual_ip(hash_id: str) -> str:
    """Stable virtual relay address for a node (inside 100.64.0.0/10)."""
    offset = int(hashlib.sha256(hash_id.encode()).hexdigest()[:8], 16)
    host = 1 + offset % (VIRTUAL_NET.num_addresses - 2)
    return str(VIRTUAL_NET.network_address + host)


# ---------------------------------------------------------------- STUN codec


@dataclass
class StunMessage:
    method: int
    cls: int
    tid: bytes
    attrs: list[tuple[int, bytes]] = field(default_factory=list)
    raw: bytes = b""

    def get(self, attr: int) -> bytes | None:
        for t, v in self.attrs:
            if t == attr:
                return v
        return None


def _msg_type(method: int, cls: int) -> int:
    m = method
    return (m & 0x000F) | ((m & 0x0070) << 1) | ((m & 0x0F80) << 2) | cls


def _split_type(t: int) -> tuple[int, int]:
    cls = t & 0x0110
    method = (t & 0x000F) | ((t & 0x00E0) >> 1) | ((t & 0x3E00) >> 2)
    return method, cls


def parse_stun(data: bytes) -> StunMessage | None:
    if len(data) < 20 or data[0] & 0xC0:
        return None
    mtype, length, magic = struct.unpack(">HHI", data[:8])
    if magic != MAGIC or length + 20 > len(data) or length % 4:
        return None
    tid = data[8:20]
    attrs: list[tuple[int, bytes]] = []
    pos = 20
    end = 20 + length
    while pos + 4 <= end:
        atype, alen = struct.unpack(">HH", data[pos : pos + 4])
        value = data[pos + 4 : pos + 4 + alen]
        if len(value) != alen:
            return None
        attrs.append((atype, value))
        pos += 4 + alen + (-alen % 4)
    method, cls = _split_type(mtype)
    return StunMessage(method, cls, tid, attrs, data[:end])


def _attr(atype: int, value: bytes) -> bytes:
    return struct.pack(">HH", atype, len(value)) + value + b"\x00" * (-len(value) % 4)


def xor_address(ip: str, port: int, tid: bytes) -> bytes:
    del tid  # IPv4 only: XOR with the magic cookie
    xport = port ^ (MAGIC >> 16)
    xip = int(ipaddress.IPv4Address(ip)) ^ MAGIC
    return struct.pack(">BBHI", 0, 1, xport, xip)


def parse_xor_address(value: bytes) -> tuple[str, int] | None:
    if len(value) < 8 or value[1] != 1:
        return None
    _, _, xport, xip = struct.unpack(">BBHI", value[:8])
    return str(ipaddress.IPv4Address(xip ^ MAGIC)), xport ^ (MAGIC >> 16)


def build_stun(
    method: int,
    cls: int,
    tid: bytes,
    attrs: list[tuple[int, bytes]],
    *,
    key: bytes | None = None,
    fingerprint: bool = True,
) -> bytes:
    body = b"".join(_attr(t, v) for t, v in attrs)
    mtype = _msg_type(method, cls)
    if key is not None:
        length = len(body) + 24  # MESSAGE-INTEGRITY attribute
        header = struct.pack(">HHI", mtype, length, MAGIC) + tid
        mac = hmac.new(key, header + body, hashlib.sha1).digest()
        body += _attr(A_MESSAGE_INTEGRITY, mac)
    if fingerprint:
        length = len(body) + 8
        header = struct.pack(">HHI", mtype, length, MAGIC) + tid
        crc = (zlib.crc32(header + body) & 0xFFFFFFFF) ^ 0x5354554E
        body += _attr(A_FINGERPRINT, struct.pack(">I", crc))
    return struct.pack(">HHI", mtype, len(body), MAGIC) + tid + body


def check_integrity(msg: StunMessage, key: bytes) -> bool:
    """Verify MESSAGE-INTEGRITY over the bytes preceding it (RFC 5389 §15.4)."""
    raw = msg.raw
    pos = 20
    while pos + 4 <= len(raw):
        atype, alen = struct.unpack(">HH", raw[pos : pos + 4])
        if atype == A_MESSAGE_INTEGRITY:
            mac = raw[pos + 4 : pos + 4 + 20]
            covered_len = pos - 20 + 24
            header = raw[:2] + struct.pack(">H", covered_len) + raw[4:20]
            expect = hmac.new(key, header + raw[20:pos], hashlib.sha1).digest()
            return hmac.compare_digest(mac, expect)
        pos += 4 + alen + (-alen % 4)
    return False


def long_term_key(username: str, password: str) -> bytes:
    return hashlib.md5(f"{username}:{REALM}:{password}".encode()).digest()  # noqa: S324 - RFC 5389


# ------------------------------------------------------------- the relay


@dataclass
class Allocation:
    client: tuple[str, int]
    sock: asyncio.DatagramTransport | None
    peer_hash: str
    relay_port: int
    key: bytes
    username: str
    expires: float
    permissions: dict[str, float] = field(default_factory=dict)
    channels: dict[int, tuple[str, int]] = field(default_factory=dict)
    channel_of: dict[tuple[str, int], int] = field(default_factory=dict)


MediaSender = Callable[[str, int, int, bytes], Awaitable[None]]


class _Endpoint(asyncio.DatagramProtocol):
    """One bound socket (loopback or a local interface address)."""

    def __init__(self, server: LocalTurnServer) -> None:
        self.server = server
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if self.transport is not None:
            self.server.handle(data, addr, self.transport)


class LocalTurnServer:
    """TURN server for this node's own browser only.

    One socket per address (loopback + each local interface) so every reply
    leaves from the exact address the browser sent to.
    """

    def __init__(self, own_hash: Callable[[], str], send_media: MediaSender) -> None:
        self._own_hash = own_hash
        self._send_media = send_media
        self.transport: asyncio.DatagramTransport | None = None
        self.port = 0
        self.endpoints: dict[str, tuple[asyncio.DatagramTransport, int]] = {}
        self._credentials: dict[str, tuple[str, str, float]] = {}  # user -> (pw, peer, exp)
        self._nonces: dict[str, float] = {}
        self._allocs: dict[tuple[str, int], Allocation] = {}
        self._by_port: dict[int, Allocation] = {}
        self.relayed_out = 0
        self.relayed_in = 0
        self._sock: asyncio.DatagramTransport | None = None

    # -- lifecycle
    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        transport, bound = await self._bind(host, port)
        self.transport = transport
        self.port = bound
        log.info("Call relay (local TURN) on %s:%d", host, bound)
        return bound

    async def _bind(self, host: str, port: int) -> tuple[asyncio.DatagramTransport, int]:
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _Endpoint(self), local_addr=(host, port)
        )
        bound = int(transport.get_extra_info("sockname")[1])
        self.endpoints[host] = (transport, bound)
        return transport, bound

    async def ensure_interface_endpoints(self, ips: list[str]) -> None:
        """Also listen on the device's own addresses (fallback when a WebView
        cannot reach 127.0.0.1 from its network-bound sockets)."""
        for ip in ips:
            if ip in self.endpoints or ip.startswith("127."):
                continue
            try:
                await self._bind(ip, 0)
            except OSError as exc:
                log.debug("Call relay could not bind %s: %s", ip, exc)

    def urls(self) -> list[str]:
        ordered = sorted(self.endpoints.items(), key=lambda kv: not kv[0].startswith("127."))
        return [f"turn:{ip}:{port}?transport=udp" for ip, (_t, port) in ordered]

    def close(self) -> None:
        for transport, _port in self.endpoints.values():
            transport.close()
        self.endpoints.clear()
        self.transport = None

    # -- credentials handed to the page for one call
    def issue_credentials(self, peer_hash: str) -> dict[str, str]:
        now = time.time()
        self._credentials = {u: c for u, c in self._credentials.items() if c[2] > now}
        username = f"{int(now + CREDENTIAL_TTL)}:{secrets.token_hex(8)}"
        password = secrets.token_urlsafe(18)
        self._credentials[username] = (password, peer_hash, now + CREDENTIAL_TTL)
        return {"username": username, "credential": password}

    # -- network input from the browser
    def handle(self, data: bytes, addr: tuple[str, int], sock: asyncio.DatagramTransport) -> None:
        if not data or not self._is_local(addr[0]):
            return  # only this device's own browser may use the relay
        self._sock = sock
        first = data[0]
        if 0x40 <= first <= 0x7F:
            self._on_channel_data(data, addr)
            return
        msg = parse_stun(data)
        if msg is None:
            return
        try:
            self._on_stun(msg, addr)
        except Exception as exc:  # noqa: BLE001 - never let one packet kill the relay
            log.debug("TURN request failed: %s", exc)

    @staticmethod
    def _is_local(ip: str) -> bool:
        if ip.startswith("127.") or ip == "::1":
            return True
        from srltcp.utils.network import list_interfaces

        return any(i.get("ip") == ip for i in list_interfaces())

    def _reply(self, addr: tuple[str, int], packet: bytes,
               sock: asyncio.DatagramTransport | None = None) -> None:
        target = sock or self._sock or self.transport
        if target is not None:
            target.sendto(packet, addr)

    def _error(self, msg: StunMessage, addr: tuple[str, int], code: int, reason: str,
               *, with_nonce: bool = False) -> None:
        attrs = [(A_ERROR_CODE, struct.pack(">HBB", 0, code // 100, code % 100) + reason.encode())]
        if with_nonce:
            nonce = secrets.token_hex(12)
            self._nonces[nonce] = time.time() + 3600
            attrs += [(A_REALM, REALM.encode()), (A_NONCE, nonce.encode())]
        self._reply(addr, build_stun(msg.method, ERROR, msg.tid, attrs))

    def _authenticate(self, msg: StunMessage, addr: tuple[str, int]) -> tuple[str, bytes] | None:
        user_b, nonce_b = msg.get(A_USERNAME), msg.get(A_NONCE)
        if user_b is None or nonce_b is None or msg.get(A_MESSAGE_INTEGRITY) is None:
            self._error(msg, addr, 401, "Unauthorized", with_nonce=True)
            return None
        username = user_b.decode(errors="replace")
        cred = self._credentials.get(username)
        if not cred or cred[2] < time.time():
            self._error(msg, addr, 401, "Unauthorized", with_nonce=True)
            return None
        if self._nonces.get(nonce_b.decode(errors="replace"), 0) < time.time():
            self._error(msg, addr, 438, "Stale Nonce", with_nonce=True)
            return None
        key = long_term_key(username, cred[0])
        if not check_integrity(msg, key):
            self._error(msg, addr, 401, "Unauthorized", with_nonce=True)
            return None
        return username, key

    def _on_stun(self, msg: StunMessage, addr: tuple[str, int]) -> None:
        if msg.method == BINDING and msg.cls == REQUEST:
            self._reply(addr, build_stun(BINDING, SUCCESS, msg.tid, [
                (A_XOR_MAPPED_ADDRESS, xor_address(addr[0], addr[1], msg.tid)),
            ]))
            return
        if msg.method == SEND and msg.cls == INDICATION:
            self._on_send_indication(msg, addr)
            return
        if msg.cls != REQUEST:
            return
        auth = self._authenticate(msg, addr)
        if not auth:
            return
        username, key = auth
        if msg.method == ALLOCATE:
            self._on_allocate(msg, addr, username, key)
            return
        alloc = self._allocs.get(addr)
        if not alloc or alloc.username != username:
            self._error(msg, addr, 437, "Allocation Mismatch")
            return
        if msg.method == REFRESH:
            lifetime = DEFAULT_LIFETIME
            lt = msg.get(A_LIFETIME)
            if lt and len(lt) == 4:
                lifetime = min(struct.unpack(">I", lt)[0], 3600)
            if lifetime == 0:
                self._remove(alloc)
            else:
                alloc.expires = time.time() + lifetime
            self._reply(addr, build_stun(REFRESH, SUCCESS, msg.tid, [
                (A_LIFETIME, struct.pack(">I", lifetime)),
            ], key=key))
        elif msg.method == CREATE_PERMISSION:
            for t, v in msg.attrs:
                if t == A_XOR_PEER_ADDRESS:
                    peer = parse_xor_address(v)
                    if peer:
                        alloc.permissions[peer[0]] = time.time() + 300
            self._reply(addr, build_stun(CREATE_PERMISSION, SUCCESS, msg.tid, [], key=key))
        elif msg.method == CHANNEL_BIND:
            ch_b, peer_b = msg.get(A_CHANNEL_NUMBER), msg.get(A_XOR_PEER_ADDRESS)
            peer = parse_xor_address(peer_b) if peer_b else None
            if not ch_b or len(ch_b) < 2 or not peer:
                self._error(msg, addr, 400, "Bad Request")
                return
            ch = struct.unpack(">H", ch_b[:2])[0]
            if not 0x4000 <= ch <= 0x7FFF:
                self._error(msg, addr, 400, "Bad Request")
                return
            old = alloc.channels.get(ch)
            if old and old != peer:
                alloc.channel_of.pop(old, None)
            alloc.channels[ch] = peer
            alloc.channel_of[peer] = ch
            alloc.permissions[peer[0]] = time.time() + 300
            self._reply(addr, build_stun(CHANNEL_BIND, SUCCESS, msg.tid, [], key=key))
        else:
            self._error(msg, addr, 400, "Bad Request")

    def _on_allocate(self, msg: StunMessage, addr: tuple[str, int], username: str,
                     key: bytes) -> None:
        existing = self._allocs.get(addr)
        if existing:
            if existing.username != username:
                self._error(msg, addr, 437, "Allocation Mismatch")
                return
            alloc = existing
        else:
            peer_hash = self._credentials[username][1]
            relay_port = self._free_port()
            alloc = Allocation(
                client=addr, sock=self._sock, peer_hash=peer_hash, relay_port=relay_port, key=key,
                username=username, expires=time.time() + DEFAULT_LIFETIME,
            )
            self._allocs[addr] = alloc
            self._by_port[relay_port] = alloc
        own_vip = virtual_ip(self._own_hash())
        self._reply(addr, build_stun(ALLOCATE, SUCCESS, msg.tid, [
            (A_XOR_RELAYED_ADDRESS, xor_address(own_vip, alloc.relay_port, msg.tid)),
            (A_XOR_MAPPED_ADDRESS, xor_address(addr[0], addr[1], msg.tid)),
            (A_LIFETIME, struct.pack(">I", DEFAULT_LIFETIME)),
        ], key=key))

    def _free_port(self) -> int:
        while True:
            port = 49152 + (int.from_bytes(os.urandom(2), "big") % 16000)
            if port not in self._by_port:
                return port

    def _remove(self, alloc: Allocation) -> None:
        self._allocs.pop(alloc.client, None)
        self._by_port.pop(alloc.relay_port, None)

    def _prune(self) -> None:
        now = time.time()
        for alloc in [a for a in self._allocs.values() if a.expires < now]:
            self._remove(alloc)

    # -- data from browser -> peer node
    def _forward(self, alloc: Allocation, peer: tuple[str, int], payload: bytes) -> None:
        if peer[0] != virtual_ip(alloc.peer_hash):
            return  # only the call's peer is reachable through this relay
        self.relayed_out += 1
        asyncio.ensure_future(self._send_media(alloc.peer_hash, alloc.relay_port, peer[1], payload))

    def _on_send_indication(self, msg: StunMessage, addr: tuple[str, int]) -> None:
        alloc = self._allocs.get(addr)
        peer_b, data = msg.get(A_XOR_PEER_ADDRESS), msg.get(A_DATA)
        peer = parse_xor_address(peer_b) if peer_b else None
        if alloc and peer and data is not None:
            self._forward(alloc, peer, data)

    def _on_channel_data(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) < 4:
            return
        ch, length = struct.unpack(">HH", data[:4])
        alloc = self._allocs.get(addr)
        if not alloc or len(data) < 4 + length:
            return
        peer = alloc.channels.get(ch)
        if peer:
            self._forward(alloc, peer, data[4 : 4 + length])

    # -- data from peer node -> browser
    def deliver(self, from_hash: str, src_port: int, dst_port: int, payload: bytes) -> bool:
        alloc = self._by_port.get(dst_port)
        if not alloc or alloc.peer_hash != from_hash:
            return False
        if alloc.expires < time.time():
            self._prune()
            return False
        src_ip = virtual_ip(from_hash)
        if alloc.permissions.get(src_ip, 0) < time.time():
            return False  # browser has not opened a permission for the peer yet
        self.relayed_in += 1
        ch = alloc.channel_of.get((src_ip, src_port))
        if ch is not None:
            packet = struct.pack(">HH", ch, len(payload)) + payload
        else:
            tid = os.urandom(12)
            packet = build_stun(DATA, INDICATION, tid, [
                (A_XOR_PEER_ADDRESS, xor_address(src_ip, src_port, tid)),
                (A_DATA, payload),
            ], fingerprint=False)
        self._reply(alloc.client, packet, alloc.sock)
        return True
