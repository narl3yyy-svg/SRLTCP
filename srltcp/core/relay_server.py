"""SRLTCP internet relay server (``srltcp relay``).

A deliberately small, dumb forwarder: phones connect over TLS 1.3 (pinned
certificate), present the access key, subscribe to mailbox tokens and send
opaque packets to tokens. It stores nothing, knows no names or identities and
can't read, alter or replay what it forwards (see relay_proto.py).

Abuse limits: connection caps (total and per IP), a deadline for the TLS
handshake and the HELLO, frame size limit, subscription limit, per-connection
bandwidth limit, idle timeout, and slow receivers don't make it buffer
without bound.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import hmac
import os
import secrets
import ssl
import struct
import time
from collections import defaultdict
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from srltcp.core.protocol.framing import FrameReader, pack_frame
from srltcp.core.relay_proto import (
    ACCESS_KEY_SIZE,
    MAX_RELAY_FRAME,
    MAX_SUBSCRIPTIONS,
    PREIMAGE_SIZE,
    R_DELIVER,
    R_ERROR,
    R_HELLO,
    R_NACK,
    R_PING,
    R_PONG,
    R_SEND,
    R_SUB,
    R_WELCOME,
    RELAY_VERSION,
    TOKEN_SIZE,
    RelayCode,
    relay_token,
)
from srltcp.utils.logging import get_logger

log = get_logger(__name__)

HELLO_DEADLINE = 15.0  # TLS handshake + HELLO must finish within this
IDLE_TIMEOUT = 120.0  # clients ping every 25 s
MAX_CONNECTIONS = 2048
MAX_PER_IP = 64  # carrier NAT puts many phones behind one address
MAX_SUBS_PER_MINUTE = 30  # resubscribe storms get the connection closed
RATE_BYTES_PER_SEC = 16 * 1024 * 1024  # per connection
RATE_BURST = 32 * 1024 * 1024
MAX_PENDING_WRITE = 8 * 1024 * 1024  # drop (not buffer) for a receiver this far behind


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def ensure_relay_identity(base: Path) -> tuple[Path, Path, bytes, bytes]:
    """Load or create the relay's TLS key/cert and access key.

    Returns (cert_path, key_path, cert_sha256, access_key).
    """
    cert_path, key_path, access_path = base / "relay.crt", base / "relay.key", base / "access.key"
    if not (cert_path.exists() and key_path.exists()):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SRLTCP relay")])
        now = datetime.datetime.now(datetime.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650 * 3))
            .sign(key, hashes.SHA256())
        )
        _write_private(
            key_path,
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        )
        _write_private(cert_path, cert.public_bytes(serialization.Encoding.PEM))
    if not access_path.exists():
        _write_private(access_path, secrets.token_bytes(ACCESS_KEY_SIZE))
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    access = access_path.read_bytes()
    if len(access) != ACCESS_KEY_SIZE:
        raise RuntimeError(f"{access_path} is damaged; delete it to make a new access key")
    return cert_path, key_path, cert.fingerprint(hashes.SHA256()), access


def server_ssl_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(str(cert_path), str(key_path))
    return ctx


class _Conn:
    __slots__ = (
        "addr",
        "allowance",
        "authed",
        "ip",
        "last_rx",
        "last_sub",
        "stamp",
        "sub_count",
        "tokens",
        "writer",
    )

    def __init__(self, writer: asyncio.StreamWriter, ip: str, addr: str) -> None:
        self.writer = writer
        self.ip = ip
        self.addr = addr
        self.authed = False
        self.tokens: set[bytes] = set()
        self.last_rx = time.monotonic()
        self.last_sub = 0.0
        self.sub_count = 0
        self.allowance = float(RATE_BURST)
        self.stamp = time.monotonic()

    def send(self, kind: int, body: bytes = b"") -> bool:
        transport = self.writer.transport
        if transport.is_closing():
            return False
        if transport.get_write_buffer_size() > MAX_PENDING_WRITE:
            return False  # receiver too slow: drop rather than buffer
        self.writer.write(pack_frame(bytes([kind]) + body))
        return True


class RelayServer:
    def __init__(
        self,
        host: str,
        port: int,
        base: Path,
        *,
        rate: float = RATE_BYTES_PER_SEC,
        burst: float = RATE_BURST,
    ) -> None:
        self.host = host
        self.port = port
        self.base = base
        self.rate = rate
        self.burst = burst
        self.cert_path, self.key_path, self.cert_sha256, self.access_key = ensure_relay_identity(
            base
        )
        self._subs: dict[bytes, _Conn] = {}
        self._conns: set[_Conn] = set()
        self._per_ip: dict[str, int] = defaultdict(int)
        self._server: asyncio.AbstractServer | None = None
        self.stats = {"forwarded": 0, "nack": 0, "dropped": 0, "rejected": 0}

    def code(self, public_host: str, public_port: int | None = None) -> str:
        return RelayCode(
            host=public_host,
            port=public_port or self.port,
            cert_sha256=self.cert_sha256,
            access_key=self.access_key,
        ).encode()

    async def start(self) -> int:
        self._server = await asyncio.start_server(
            self._handle,
            self.host,
            self.port,
            ssl=server_ssl_context(self.cert_path, self.key_path),
            ssl_handshake_timeout=HELLO_DEADLINE,
            limit=MAX_RELAY_FRAME * 2,
        )
        sock = self._server.sockets[0]
        self.port = sock.getsockname()[1]
        log.info("Relay listening on %s:%d", self.host, self.port)
        return self.port

    async def close(self) -> None:
        if self._server:
            self._server.close()
        for conn in list(self._conns):
            with contextlib.suppress(Exception):
                conn.writer.close()
        if self._server:
            with contextlib.suppress(Exception):
                await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("?", 0)
        ip = str(peer[0])
        if len(self._conns) >= MAX_CONNECTIONS or self._per_ip[ip] >= MAX_PER_IP:
            self.stats["rejected"] += 1
            writer.close()
            return
        conn = _Conn(writer, ip, f"{ip}:{peer[1]}")
        self._conns.add(conn)
        self._per_ip[ip] += 1
        frames = FrameReader()
        try:
            deadline = time.monotonic() + HELLO_DEADLINE
            while True:
                timeout = (deadline - time.monotonic()) if not conn.authed else IDLE_TIMEOUT
                if timeout <= 0:
                    break
                try:
                    data = await asyncio.wait_for(reader.read(65536), timeout)
                except TimeoutError:
                    break
                if not data:
                    break
                conn.last_rx = time.monotonic()
                await self._throttle(conn, len(data))
                for frame in frames.feed(data):
                    if not frame or len(frame) > MAX_RELAY_FRAME:
                        raise ConnectionError("bad frame")
                    if not self._on_frame(conn, frame):
                        raise ConnectionError("protocol violation")
                if len(frames._buffer) > MAX_RELAY_FRAME + 64:
                    raise ConnectionError("frame too large")
        except (ConnectionError, OSError, ssl.SSLError, asyncio.IncompleteReadError):
            pass
        finally:
            self._drop(conn)
            with contextlib.suppress(Exception):
                writer.close()

    async def _throttle(self, conn: _Conn, nbytes: int) -> None:
        now = time.monotonic()
        conn.allowance = min(self.burst, conn.allowance + (now - conn.stamp) * self.rate)
        conn.stamp = now
        conn.allowance -= nbytes
        if conn.allowance < 0:
            # Stop reading for a while: TCP back-pressure slows the sender.
            await asyncio.sleep(-conn.allowance / self.rate)

    def _on_frame(self, conn: _Conn, frame: bytes) -> bool:
        kind, body = frame[0], frame[1:]
        if not conn.authed:
            if kind != R_HELLO or len(body) != 1 + ACCESS_KEY_SIZE:
                return False
            if body[0] != RELAY_VERSION or not hmac.compare_digest(body[1:], self.access_key):
                conn.send(R_ERROR, b"\x01access denied")
                self.stats["rejected"] += 1
                return False
            conn.authed = True
            # Tell the phone its public address as seen from here (like STUN):
            # contacts dial it when punching through NATs.
            observed = conn.addr.rsplit(":", 1)
            conn.send(R_WELCOME, bytes([RELAY_VERSION]) + f"{observed[0]} {observed[1]}".encode())
            return True
        if kind == R_SEND:
            if len(body) < TOKEN_SIZE + 1:
                return False
            token = body[:TOKEN_SIZE]
            target = self._subs.get(token)
            if target is None or target is conn:
                self.stats["nack"] += 1
                conn.send(R_NACK, token)
            elif target.send(R_DELIVER, body):
                self.stats["forwarded"] += 1
            else:
                self.stats["dropped"] += 1
            return True
        if kind == R_SUB:
            if len(body) % PREIMAGE_SIZE or len(body) // PREIMAGE_SIZE > MAX_SUBSCRIPTIONS:
                return False
            now = time.monotonic()
            if now - conn.last_sub > 60.0:
                conn.last_sub, conn.sub_count = now, 0
            conn.sub_count += 1
            if conn.sub_count > MAX_SUBS_PER_MINUTE:
                return False
            for token in conn.tokens:
                if self._subs.get(token) is conn:
                    del self._subs[token]
            conn.tokens = {
                relay_token(body[i : i + PREIMAGE_SIZE]) for i in range(0, len(body), PREIMAGE_SIZE)
            }
            for token in conn.tokens:
                self._subs[token] = conn  # newest connection wins (phone reconnected)
            return True
        if kind == R_PING:
            conn.send(R_PONG, body[:16])
            return True
        return False

    def _drop(self, conn: _Conn) -> None:
        if conn not in self._conns:
            return
        self._conns.discard(conn)
        self._per_ip[conn.ip] -= 1
        if self._per_ip[conn.ip] <= 0:
            self._per_ip.pop(conn.ip, None)
        for token in conn.tokens:
            if self._subs.get(token) is conn:
                del self._subs[token]


def relay_base_dir(path: str | None = None) -> Path:
    if path:
        return Path(path).expanduser()
    from srltcp.utils.platform import data_dir

    return data_dir() / "relay"


async def run_relay(host: str, port: int, public_host: str, base_dir: str | None = None) -> None:
    server = RelayServer(host, port, relay_base_dir(base_dir))
    await server.start()
    shown = public_host or "YOUR.SERVER.ADDRESS"
    print("\nSRLTCP relay is running.")
    print("Paste this code into Settings → Network → Internet relay on ONE phone")
    print("(its contacts get it automatically, inside the encrypted connection):\n")
    print("  " + server.code(shown) + "\n")
    if not public_host:
        print("Replace YOUR.SERVER.ADDRESS with this server's public IP or hostname,")
        print("or start with --public-host to print a ready-to-use code.")
    print(f"Open TCP port {server.port} on this server's firewall. Keep the code private:")
    print("it contains the access key.\n")
    try:
        while True:
            await asyncio.sleep(3600)
            log.info("Relay stats: %s, %d connections", server.stats, len(server._conns))
    finally:
        await server.close()


# Wire helpers shared with the client.
def hello_frame(access_key: bytes) -> bytes:
    return pack_frame(bytes([R_HELLO, RELAY_VERSION]) + access_key)


def ping_frame() -> bytes:
    return pack_frame(bytes([R_PING]) + struct.pack(">d", time.time()))
