"""Internet relay client: reach contacts when there's no direct path.

Each phone keeps a TLS connection to the relays its contacts use, listens on
its mailboxes there and carries ordinary SRLTCP sessions through them. Direct
connections (LAN / VPN) are always preferred; the relay is the fallback and
we move back to direct as soon as it works again (see ``maintain_paths``).

Pair data per contact (in trusted.json, owner-only file):
  pair_own / pair_peer  — the two 32-byte random contributions, exchanged in
                          the E2EE session (``RENDEZVOUS``) → pair secret
  relay_code_peer       — the contact's relay code, if they have one
The relay used for a pair is the one of the contact with the lower identity
hash when both have one, else whichever exists, so both sides agree.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import secrets
import ssl
import struct
import time
from typing import TYPE_CHECKING, Any

from srltcp.core.messaging.links import PeerLink
from srltcp.core.protocol.framing import FrameReader, pack_frame
from srltcp.core.protocol.messages import MessageType, decode_payload, encode_payload
from srltcp.core.relay_proto import (
    MAX_RELAY_FRAME,
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
    PairBox,
    PairBoxError,
    RelayCode,
    RelayCodeError,
    current_day,
    mailbox_preimage,
    pair_secret,
    relay_token,
)
from srltcp.transports.base import TransportPeer
from srltcp.utils.logging import get_logger

if TYPE_CHECKING:
    from srltcp.core.messaging.backend import MessagingBackend

log = get_logger(__name__)

RELAY_PEER_PREFIX = "relay:"
RELAY_PING_EVERY = 25.0  # keeps carrier NAT mappings alive
RELAY_DEAD_AFTER = 70.0
RELAY_CONNECT_TIMEOUT = 10.0
OFFLINE_AFTER_NACK = 20.0  # don't retry a contact the relay says isn't there
UPGRADE_EVERY = 45.0  # try a direct path this often while on the relay
DIRECT_TIMEOUT = 3.0


class RelayPinError(ConnectionError):
    """The server's certificate isn't the one in the relay code."""


def relay_peer_id(hash_id: str) -> str:
    return f"{RELAY_PEER_PREFIX}{hash_id}"


def relay_peer_hash(peer_id: str) -> str | None:
    if not peer_id.startswith(RELAY_PEER_PREFIX):
        return None
    raw = peer_id[len(RELAY_PEER_PREFIX) :]
    return raw if len(raw) == 32 and all(c in "0123456789abcdef" for c in raw) else None


def client_ssl_context() -> ssl.SSLContext:
    # Trust comes from the pinned certificate hash, not from CAs; the pin is
    # checked right after the TLS handshake, before anything is sent.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class RelayConnection:
    """One TLS connection to one relay."""

    def __init__(self, code: RelayCode, on_frame: Any, on_closed: Any) -> None:
        self.code = code
        self._on_frame = on_frame
        self._on_closed = on_closed
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task[None] | None = None
        self.ready = False
        self.last_rx = 0.0
        self.error = ""

    @property
    def connected(self) -> bool:
        return bool(self.ready and self._writer and not self._writer.transport.is_closing())

    @property
    def local_ip(self) -> str:
        sockname = self._writer.get_extra_info("sockname") if self._writer else None
        return str(sockname[0]) if sockname else ""

    async def connect(self) -> None:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                self.code.host,
                self.code.port,
                ssl=client_ssl_context(),
                server_hostname=None if _is_ip(self.code.host) else self.code.host,
                limit=MAX_RELAY_FRAME * 2,
            ),
            timeout=RELAY_CONNECT_TIMEOUT,
        )
        ssl_obj = writer.get_extra_info("ssl_object")
        der = ssl_obj.getpeercert(binary_form=True) if ssl_obj else None
        if not der or not hmac.compare_digest(hashlib.sha256(der).digest(), self.code.cert_sha256):
            writer.close()
            raise RelayPinError(
                "relay certificate doesn't match the relay code (wrong server or interception)"
            )
        self._reader, self._writer = reader, writer
        writer.write(pack_frame(bytes([R_HELLO, RELAY_VERSION]) + self.code.access_key))
        frames = FrameReader()
        deadline = time.monotonic() + RELAY_CONNECT_TIMEOUT
        while True:
            data = await asyncio.wait_for(reader.read(65536), max(0.1, deadline - time.monotonic()))
            if not data:
                raise ConnectionError("relay closed the connection")
            got = frames.feed(data)
            if not got:
                continue
            first = got[0]
            if first[:1] == bytes([R_WELCOME]):
                break
            if first[:1] == bytes([R_ERROR]):
                raise ConnectionError(first[2:].decode("utf-8", "replace") or "relay refused")
            raise ConnectionError("unexpected relay reply")
        self.ready = True
        self.last_rx = time.monotonic()
        self._task = asyncio.create_task(self._read_loop(frames, got[1:]))

    async def _read_loop(self, frames: FrameReader, pending: list[bytes]) -> None:
        assert self._reader is not None
        try:
            for frame in pending:
                await self._on_frame(self, frame)
            while True:
                data = await self._reader.read(65536)
                if not data:
                    break
                self.last_rx = time.monotonic()
                for frame in frames.feed(data):
                    if frame:
                        await self._on_frame(self, frame)
                if len(frames._buffer) > MAX_RELAY_FRAME + 64:
                    break
        except (ConnectionError, OSError, ssl.SSLError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a bad frame must not kill the node
            log.warning("Relay read loop error: %s", exc)
        finally:
            self.ready = False
            if self._writer:
                with contextlib.suppress(Exception):
                    self._writer.close()
            await self._on_closed(self)

    async def send(self, kind: int, body: bytes = b"") -> None:
        if not self.connected or self._writer is None:
            raise KeyError("relay not connected")
        self._writer.write(pack_frame(bytes([kind]) + body))
        if self._writer.transport.get_write_buffer_size() > 1024 * 1024:
            await self._writer.drain()

    async def close(self) -> None:
        self.ready = False
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        if self._writer:
            with contextlib.suppress(Exception):
                self._writer.close()


def _acceptable_peer_relay_host(host: str) -> bool:
    """A relay code from a contact must not aim us at link-local, multicast or
    unspecified addresses (LAN and loopback relays are legitimate setups)."""
    import ipaddress

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # hostname
    return not (ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved)


def _is_ip(host: str) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


class RelayMixin:
    """Relay client state and the automatic direct/relay path switching."""

    def _init_relay(self: MessagingBackend) -> None:
        self._relay_conns: dict[str, RelayConnection] = {}
        self._relay_connecting: set[str] = set()
        self._relay_errors: dict[str, str] = {}
        # relay_id -> {token: contact}: the mailboxes we listen on, rebuilt on
        # every subscription (so old days' tokens don't pile up)
        self._relay_in_tokens: dict[str, dict[bytes, str]] = {}
        self._relay_out_tokens: dict[bytes, str] = {}  # token -> contact (mailboxes we send to)
        self._relay_subscribed_day: dict[str, int] = {}
        self._pairboxes: dict[str, tuple[bytes, PairBox]] = {}
        self._relay_offline_until: dict[str, float] = {}
        self._relay_task: asyncio.Task[None] | None = None
        self._upgrade_attempts: dict[str, float] = {}
        self._upgrades: dict[str, tuple[str, Any, float]] = {}  # hash -> (tcp peer_id, kx, started)
        self._relay_sub_state: dict[str, tuple[int, bytes]] = {}
        self._rendezvous_changed: dict[str, float] = {}

    # ── Configuration ──

    def own_relay_code(self: MessagingBackend) -> RelayCode | None:
        text = str(getattr(self.config, "relay_code", "") or "").strip()
        if not text:
            return None
        try:
            return RelayCode.parse(text)
        except RelayCodeError:
            return None

    def _peer_relay_code(self: MessagingBackend, hash_id: str) -> RelayCode | None:
        peer = self.trusted.get(hash_id)
        if not peer or not peer.relay_code_peer:
            return None
        try:
            return RelayCode.parse(peer.relay_code_peer)
        except RelayCodeError:
            return None

    def pair_relay(self: MessagingBackend, hash_id: str) -> RelayCode | None:
        """The relay this contact and I meet on (both sides pick the same)."""
        if not getattr(self.config, "relay_enabled", True):
            return None
        mine, theirs = self.own_relay_code(), self._peer_relay_code(hash_id)
        if mine and theirs:
            return mine if self._me_hash() < hash_id else theirs
        return mine or theirs

    def _me_hash(self: MessagingBackend) -> str:
        return self.identities["tcp"].hash_id

    def _pair_secret(self: MessagingBackend, hash_id: str) -> bytes | None:
        peer = self.trusted.get(hash_id)
        if not peer or peer.blocked or not peer.pair_own or not peer.pair_peer:
            return None
        try:
            own, theirs = bytes.fromhex(peer.pair_own), bytes.fromhex(peer.pair_peer)
        except ValueError:
            return None
        if len(own) != 32 or len(theirs) != 32:
            return None
        return pair_secret(own, theirs)

    def _pairbox(self: MessagingBackend, hash_id: str) -> PairBox | None:
        secret = self._pair_secret(hash_id)
        if secret is None:
            self._pairboxes.pop(hash_id, None)
            return None
        cached = self._pairboxes.get(hash_id)
        if cached and cached[0] == secret:
            return cached[1]
        box = PairBox(secret, self._me_hash(), hash_id)
        self._pairboxes[hash_id] = (secret, box)
        return box

    def relay_contacts(self: MessagingBackend) -> dict[str, tuple[RelayCode, list[str]]]:
        """relay_id -> (code, contacts reachable through it)."""
        out: dict[str, tuple[RelayCode, list[str]]] = {}
        for peer in self.trusted.list_peers():
            code = self.pair_relay(peer.hash_id)
            if code is None or self._pair_secret(peer.hash_id) is None:
                continue
            entry = out.setdefault(code.relay_id, (code, []))
            entry[1].append(peer.hash_id)
        own = self.own_relay_code()
        if own and getattr(self.config, "relay_enabled", True):
            out.setdefault(own.relay_id, (own, []))  # stay connected to our own relay
        return out

    def relay_available_for(self: MessagingBackend, hash_id: str) -> bool:
        code = self.pair_relay(hash_id)
        if code is None or self._pair_secret(hash_id) is None:
            return False
        conn = self._relay_conns.get(code.relay_id)
        return bool(conn and conn.connected)

    def relay_status(self: MessagingBackend) -> dict[str, Any]:
        own = self.own_relay_code()
        relays = []
        for relay_id, (code, contacts) in self.relay_contacts().items():
            conn = self._relay_conns.get(relay_id)
            relays.append(
                {
                    "id": relay_id,
                    "address": code.display,
                    "own": bool(own and own.relay_id == relay_id),
                    "connected": bool(conn and conn.connected),
                    "contacts": len(contacts),
                    "error": self._relay_errors.get(relay_id, ""),
                }
            )
        return {
            "enabled": bool(getattr(self.config, "relay_enabled", True)),
            "configured": own is not None,
            "address": own.display if own else "",
            "relays": relays,
        }

    # ── Lifecycle ──

    async def start_relay_client(self: MessagingBackend) -> None:
        if self._relay_task is None or self._relay_task.done():
            self._relay_task = asyncio.create_task(self._relay_loop())

    async def stop_relay_client(self: MessagingBackend) -> None:
        if self._relay_task:
            self._relay_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._relay_task
            self._relay_task = None
        for conn in list(self._relay_conns.values()):
            await conn.close()
        self._relay_conns.clear()

    async def apply_relay_settings(self: MessagingBackend) -> None:
        self._relay_errors.clear()
        await self.sync_relays()
        # Contacts learn our relay (or that we dropped it) right away.
        for hash_id, link in list(self._links.items()):
            if link.handshake_complete and self.is_trusted(hash_id):
                await self.send_rendezvous(hash_id)

    async def _relay_loop(self: MessagingBackend) -> None:
        tick = 0
        while self._running:
            try:
                await self.sync_relays()
                await self._ping_relays()
                if tick % 3 == 0:
                    await self.maintain_paths()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                log.warning("Relay maintenance failed: %s", exc)
            tick += 1
            await asyncio.sleep(15.0)

    async def sync_relays(self: MessagingBackend) -> None:
        wanted = self.relay_contacts()
        for relay_id in list(self._relay_conns):
            if relay_id not in wanted:
                stale = self._relay_conns.pop(relay_id)
                await stale.close()
        day = current_day()
        for relay_id, (code, contacts) in wanted.items():
            conn = self._relay_conns.get(relay_id)
            if conn and conn.code != code:
                self._relay_conns.pop(relay_id, None)
                await conn.close()
                conn = None
            if conn is None or not conn.connected:
                if relay_id not in self._relay_connecting:
                    self._relay_connecting.add(relay_id)
                    asyncio.create_task(self._connect_relay(relay_id, code))
                continue
            await self._subscribe(conn, contacts, day)

    async def _connect_relay(self: MessagingBackend, relay_id: str, code: RelayCode) -> None:
        try:
            old = self._relay_conns.pop(relay_id, None)
            if old:
                await old.close()
            conn = RelayConnection(code, self._on_relay_frame, self._on_relay_closed)
            await conn.connect()
            self._relay_conns[relay_id] = conn
            self._relay_errors.pop(relay_id, None)
            log.info("Connected to relay %s", code.display)
            contacts = self.relay_contacts().get(relay_id, (code, []))[1]
            self._relay_subscribed_day.pop(relay_id, None)
            await self._subscribe(conn, contacts, current_day())
            # Say hello to contacts we have no session with, so both sides see
            # each other online right away.
            for hash_id in contacts:
                link = self.get_link(hash_id)
                if not link or not link.handshake_complete:
                    self._kick_connect(hash_id)
        except RelayPinError as exc:
            self._relay_errors[relay_id] = str(exc)
            log.warning("Relay %s: %s", code.display, exc)
        except (OSError, TimeoutError, ConnectionError, ssl.SSLError) as exc:
            self._relay_errors[relay_id] = f"unreachable ({exc or type(exc).__name__})"
            log.info("Relay %s unreachable: %s", code.display, exc or type(exc).__name__)
        finally:
            self._relay_connecting.discard(relay_id)

    async def _subscribe(
        self: MessagingBackend, conn: RelayConnection, contacts: list[str], day: int
    ) -> None:
        relay_id = conn.code.relay_id
        me = self._me_hash()
        preimages: list[bytes] = []
        in_tokens: dict[bytes, str] = {}
        for hash_id in sorted(contacts):
            secret = self._pair_secret(hash_id)
            if secret is None:
                continue
            for d in (day - 1, day, day + 1):  # clock differences between phones
                pre = mailbox_preimage(secret, me, d)
                preimages.append(pre)
                in_tokens[relay_token(pre)] = hash_id
        signature = hashlib.sha256(b"".join(preimages)).digest()
        key = (day, signature)
        if self._relay_sub_state.get(relay_id) == key:
            return
        self._relay_in_tokens[relay_id] = in_tokens
        await conn.send(R_SUB, b"".join(preimages))
        self._relay_sub_state[relay_id] = key

    async def _ping_relays(self: MessagingBackend) -> None:
        now = time.monotonic()
        for conn in list(self._relay_conns.values()):
            if not conn.connected:
                continue
            if now - conn.last_rx > RELAY_DEAD_AFTER:
                log.info("Relay %s went silent — reconnecting", conn.code.display)
                await conn.close()
                await self._on_relay_closed(conn)
                continue
            with contextlib.suppress(KeyError, OSError, ConnectionError):
                await conn.send(R_PING, struct.pack(">d", time.time()))

    async def relay_network_changed(self: MessagingBackend, ips: set[str]) -> None:
        """Our addresses changed (Wi-Fi <-> mobile data, VPN): a relay connection
        on a vanished address is dead — reconnect now, not after a timeout."""
        for conn in list(self._relay_conns.values()):
            local = conn.local_ip
            if conn.connected and local and local not in ips and local not in ("127.0.0.1", "::1"):
                log.info("Network changed — reconnecting to relay %s", conn.code.display)
                await conn.close()
                await self._on_relay_closed(conn)
        await self.sync_relays()

    async def _on_relay_closed(self: MessagingBackend, conn: RelayConnection) -> None:
        relay_id = conn.code.relay_id
        if self._relay_conns.get(relay_id) is conn:
            self._relay_conns.pop(relay_id, None)
        self._relay_sub_state.pop(relay_id, None)
        for hash_id, link in list(self._links.items()):
            if link.transport != "relay":
                continue
            code = self.pair_relay(hash_id)
            if code is None or code.relay_id == relay_id:
                self.remove_link(hash_id)
                if self._on_link_down:
                    with contextlib.suppress(Exception):
                        await self._on_link_down(hash_id, link.peer_name)
                self._schedule_reconnect(hash_id)

    # ── Packets ──

    async def _on_relay_frame(self: MessagingBackend, conn: RelayConnection, frame: bytes) -> None:
        kind, body = frame[0], frame[1:]
        if kind == R_DELIVER and len(body) > TOKEN_SIZE:
            token, payload = body[:TOKEN_SIZE], body[TOKEN_SIZE:]
            hash_id = self._relay_in_tokens.get(conn.code.relay_id, {}).get(token)
            if not hash_id:
                return
            box = self._pairbox(hash_id)
            code = self.pair_relay(hash_id)
            if box is None or code is None or code.relay_id != conn.code.relay_id:
                return
            try:
                inner = box.open(payload)
            except PairBoxError as exc:
                log.debug("Dropped relayed packet for %s: %s", hash_id[:8], exc)
                return
            self._relay_offline_until.pop(hash_id, None)
            peer = TransportPeer(peer_id=relay_peer_id(hash_id), address="relay", transport="relay")
            await self._on_transport_frame(peer, inner)
        elif kind == R_NACK and len(body) == TOKEN_SIZE:
            hash_id = self._relay_out_tokens.get(body)
            if not hash_id:
                return
            self._relay_offline_until[hash_id] = time.monotonic() + OFFLINE_AFTER_NACK
            link = self.get_link(hash_id)
            if link and link.transport == "relay":
                log.info("Relay: %s is not online there", hash_id[:8])
                self.remove_link(hash_id)
                if link.handshake_complete and self._on_link_down:
                    with contextlib.suppress(Exception):
                        await self._on_link_down(hash_id, link.peer_name)
        elif kind in (R_PONG, R_WELCOME):
            return

    async def send_via_relay(self: MessagingBackend, hash_id: str, packet: bytes) -> None:
        code = self.pair_relay(hash_id)
        box = self._pairbox(hash_id)
        secret = self._pair_secret(hash_id)
        if code is None or box is None or secret is None:
            raise KeyError("no relay for this contact")
        conn = self._relay_conns.get(code.relay_id)
        if not conn or not conn.connected:
            raise KeyError("relay not connected")
        token = relay_token(mailbox_preimage(secret, hash_id, current_day()))
        self._relay_out_tokens[token] = hash_id
        if len(self._relay_out_tokens) > 4096:
            self._relay_out_tokens = dict(list(self._relay_out_tokens.items())[-2048:])
        await conn.send(R_SEND, token + box.seal(packet))

    def relay_link_reachable(self: MessagingBackend, hash_id: str) -> bool:
        if time.monotonic() < self._relay_offline_until.get(hash_id, 0.0):
            return False
        return self.relay_available_for(hash_id)

    async def open_relay_link(self: MessagingBackend, hash_id: str) -> bool:
        """Start a session with a contact through the relay."""
        if not self.relay_link_reachable(hash_id):
            return False
        peer = self.trusted.get(hash_id)
        if not peer or peer.blocked:
            return False
        code = self.pair_relay(hash_id)
        link = PeerLink(
            hash_id=hash_id,
            transport_peer_id=relay_peer_id(hash_id),
            transport="relay",
            address=f"relay {code.display}" if code else "relay",
            public_key=bytes.fromhex(peer.public_key) if peer.public_key else b"\x00" * 32,
            peer_name=peer.name,
        )
        self.register_link(link)
        await self._initiate_handshake(hash_id)
        return True

    # ── Rendezvous: pair secret + relay code, inside the E2EE session ──

    async def send_rendezvous(self: MessagingBackend, hash_id: str) -> None:
        peer = self.trusted.get(hash_id)
        link = self.get_link(hash_id)
        if not peer or peer.blocked or not link or not link.handshake_complete:
            return
        if not peer.pair_own:
            self.trusted.update(hash_id, pair_own=secrets.token_hex(32))
            peer = self.trusted.get(hash_id)
            assert peer is not None
        own = self.own_relay_code() if getattr(self.config, "relay_enabled", True) else None
        body = encode_payload({"v": 1, "c": peer.pair_own, "r": own.encode() if own else ""})
        with contextlib.suppress(KeyError, OSError, RuntimeError, ConnectionError):
            packet = self._seal(link, MessageType.RENDEZVOUS, body)
            await self._send_raw(link.transport_peer_id, link.transport, packet)

    async def _handle_rendezvous(self: MessagingBackend, hash_id: str, body: bytes) -> None:
        peer = self.trusted.get(hash_id)
        if not peer or peer.blocked:
            return
        data = decode_payload(body)
        contrib = str(data.get("c", "")).lower()
        try:
            if len(bytes.fromhex(contrib)) != 32:
                return
        except ValueError:
            return
        relay_text = str(data.get("r", ""))[:512]
        if relay_text:
            try:
                code = RelayCode.parse(relay_text)
                relay_text = code.encode() if _acceptable_peer_relay_host(code.host) else ""
            except RelayCodeError:
                relay_text = ""
        changed = contrib != peer.pair_peer or relay_text != peer.relay_code_peer
        if changed and peer.pair_peer:
            # Pair details are stable; a contact flipping them rapidly only
            # costs us disk writes and reconnects, so accept one change a minute.
            now = time.monotonic()
            last = self._rendezvous_changed.get(hash_id, 0.0)
            if now - last < 60.0:
                return
            self._rendezvous_changed[hash_id] = now
        if changed:
            self.trusted.update(hash_id, pair_peer=contrib, relay_code_peer=relay_text)
            log.info("Relay details updated for %s", peer.name)
        if changed or not peer.pair_own:
            # Answer with ours: they may have trusted us after our first one
            # arrived (and was dropped). Stops once both sides agree.
            await self.send_rendezvous(hash_id)
        if changed:
            await self.sync_relays()

    # ── Path switching ──

    def _direct_candidates(self: MessagingBackend, hash_id: str) -> list[tuple[str, int]]:
        peer = self.trusted.get(hash_id)
        if not peer:
            return []
        out: list[tuple[str, int]] = []
        found = self.discovery.get(hash_id, "tcp")
        for host, port in (
            ((found.tcp_host, found.tcp_port) if found else ("", 0)),
            (peer.tcp_host, peer.tcp_port),
            *((h, peer.tcp_port) for h in peer.alt_hosts),
        ):
            if host and (host, port or self.config.tcp_port) not in out:
                out.append((host, port or self.config.tcp_port))
        return out[:4]

    async def maintain_paths(self: MessagingBackend) -> None:
        """Every ~45 s: on the relay but a direct path might work again → try it."""
        now = time.monotonic()
        for hash_id, (peer_id, _kx, started) in list(self._upgrades.items()):
            if now - started > 10.0:
                self._upgrades.pop(hash_id, None)
                if self.tcp_transport and not self.get_link_by_peer_id(peer_id):
                    await self.tcp_transport.disconnect(peer_id)
        for hash_id, link in list(self._links.items()):
            if link.transport != "relay" or not link.handshake_complete:
                continue
            # One side drives upgrades so both don't race each other.
            if self._me_hash() > hash_id or hash_id in self._upgrades:
                continue
            if now - self._upgrade_attempts.get(hash_id, 0.0) < UPGRADE_EVERY:
                continue
            if self.has_active_transfer_for(hash_id) or self._call_active():
                continue
            self._upgrade_attempts[hash_id] = now
            asyncio.create_task(self._try_direct_upgrade(hash_id))

    def _call_active(self: MessagingBackend) -> bool:
        relay = getattr(self, "call_relay", None)
        return bool(relay is not None and getattr(relay, "_by_port", None))

    async def _try_direct_upgrade(self: MessagingBackend, hash_id: str) -> None:
        if not self.tcp_transport:
            return
        for host, port in self._direct_candidates(hash_id):
            try:
                peer_id = await self.tcp_transport.connect(host, port, timeout=DIRECT_TIMEOUT)
            except (OSError, TimeoutError, ConnectionError):
                continue
            kx = await self._send_upgrade_handshake(hash_id, peer_id)
            if kx is None:
                await self.tcp_transport.disconnect(peer_id)
                return
            self._upgrades[hash_id] = (peer_id, kx, time.monotonic())
            log.info("Trying a direct path to %s at %s:%d", hash_id[:8], host, port)
            return

    def upgrade_for_peer_id(self: MessagingBackend, peer_id: str) -> tuple[str, Any] | None:
        for hash_id, (pid, kx, _started) in self._upgrades.items():
            if pid == peer_id:
                return hash_id, kx
        return None
