"""Reach trusted contacts anywhere over Tor — no server, no port forwarding.

Each device runs Tor and publishes a private *onion service* that forwards to
a loopback-only listener. Contacts dial each other's onion address through
Tor's SOCKS port. Tor gives a connection that needs nothing from the
network: it works behind any NAT, carrier NAT or firewall that lets Tor out,
and neither side learns the other's IP address.

Privacy and safety rules:

- The onion address is sent only to trusted contacts, inside the end-to-end
  encrypted session (RENDEZVOUS field ``"o"``), never in discovery or cards.
- Connections that arrive through the onion service (or were dialed over Tor)
  are only accepted from *trusted* identities; a stranger who learned the
  address can't even leave a contact request (see ``ConnectMixin``).
- The session inside is the normal signed SRLTCP handshake, so Tor is just a
  pipe: an onion address can't be used to impersonate a contact.
- Each contact gets its own Tor circuit (SOCKS isolation).
- Speed: one Tor circuit tops out around 0.5 MB/s, so a file is spread over
  ``TOR_LANES`` extra circuits ("lanes", each a separate connection with its
  own SOCKS isolation). A lane opens with ``LANE_JOIN``: a tag derived from
  the session key plus a message sealed with it, so only the contact can
  attach a lane to the session and lanes carry nothing but encrypted file
  chunks. Chat, calls and acknowledgements stay on the main circuit and never
  queue behind file data.

Tor itself: on Android it's bundled (tor-android's TorService). On a PC the
app starts an installed ``tor`` (Tor Browser's or the Tor Expert Bundle's, or
``tor`` on PATH) with its own private data directory.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import os
import re
import secrets
import shutil
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from srltcp.core.messaging.constants import TOR_LANES
from srltcp.core.protocol.messages import Flags, MessageType, build_header, header_aad
from srltcp.transports.base import TransportPeer
from srltcp.utils.logging import get_logger

if TYPE_CHECKING:
    from srltcp.core.messaging.backend import MessagingBackend
    from srltcp.core.messaging.links import PeerLink

log = get_logger(__name__)

ONION_VIRTUAL_PORT = 7825
TOR_DIAL_TIMEOUT = 60.0
TOR_BOOTSTRAP_TIMEOUT = 180.0
LANE_RETRY = 60.0  # wait this long before dialing lanes again after a failure
LANE_IDLE = 60.0  # close a contact's lanes this long after its last transfer
LANE_ACCEPT = 16  # lanes we take from one contact (theirs may use more than ours)
# A lane closed this soon after we opened it was refused (an older app takes
# fewer lanes): use only as many as are up, for a while, instead of redialing.
LANE_REFUSED = 15.0
LANE_CAP_FOR = 600.0
LANE_TAG = b"srltcp-lane"
_ONION_RE = re.compile(r"^[a-z2-7]{56}\.onion$")


def valid_onion(address: str) -> bool:
    """A well-formed v3 onion address (checks version byte and checksum)."""
    address = (address or "").strip().lower()
    if not _ONION_RE.fullmatch(address):
        return False
    try:
        raw = base64.b32decode(address[:56].upper())
    except (ValueError, TypeError):
        return False
    pubkey, checksum, version = raw[:32], raw[32:34], raw[34:35]
    if version != b"\x03":
        return False
    want = hashlib.sha3_256(b".onion checksum" + pubkey + version).digest()[:2]
    return checksum == want


class TorControlError(RuntimeError):
    pass


class TorControl:
    """Just enough of Tor's control protocol (control-spec.txt)."""

    def __init__(self) -> None:
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    async def connect(self, host: str, port: int) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=10
        )

    async def connect_unix(self, path: str) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_unix_connection(path), timeout=10
        )

    @property
    def connected(self) -> bool:
        return bool(self._writer and not self._writer.is_closing())

    async def close(self) -> None:
        if self._writer:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
        self._writer = None

    async def command(self, line: str) -> list[str]:
        """Send one command; return the reply lines (without status codes).
        Raises TorControlError on a non-250 reply."""
        if not self._reader or not self._writer:
            raise TorControlError("not connected")
        async with self._lock:
            self._writer.write(line.encode() + b"\r\n")
            await self._writer.drain()
            out: list[str] = []
            while True:
                raw = await asyncio.wait_for(self._reader.readline(), timeout=30)
                if not raw:
                    raise TorControlError("Tor closed the control connection")
                text = raw.decode("utf-8", "replace").rstrip("\r\n")
                code, sep, rest = text[:3], text[3:4], text[4:]
                if sep == "+":  # data reply, ends with a "." line
                    data = [rest]
                    while True:
                        more = (await self._reader.readline()).decode("utf-8", "replace")
                        more = more.rstrip("\r\n")
                        if more == ".":
                            break
                        data.append(more)
                    out.append("\n".join(data))
                    continue
                if not code.startswith("2"):
                    raise TorControlError(f"{code} {rest}")
                out.append(rest)
                if sep == " ":
                    return out

    async def authenticate(self, cookie_path: Path | None) -> None:
        if cookie_path and cookie_path.exists():
            cookie = cookie_path.read_bytes()
            await self.command(f"AUTHENTICATE {cookie.hex()}")
        else:
            await self.command("AUTHENTICATE")

    async def getinfo(self, key: str) -> str:
        lines = await self.command(f"GETINFO {key}")
        for line in lines:
            if line.startswith(key + "="):
                return line[len(key) + 1:].strip().strip('"')
        return ""


def _tor_binary(configured: str = "") -> str | None:
    """Find a tor executable: the setting, PATH, then where Tor Browser / the
    Tor Expert Bundle usually live on Windows and macOS."""
    if configured and Path(configured).is_file():
        return configured
    found = shutil.which("tor")
    if found:
        return found
    home = Path.home()
    candidates: list[Path] = []
    if sys.platform == "win32":
        app_dir = Path(__file__).resolve().parents[3]
        for base in (
            home / "Desktop" / "Tor Browser",
            home / "OneDrive" / "Desktop" / "Tor Browser",
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Tor Browser",
            Path(os.environ.get("LOCALAPPDATA", str(home))) / "Tor Browser",
        ):
            candidates.append(base / "Browser" / "TorBrowser" / "Tor" / "tor.exe")
        candidates += [app_dir / "tor" / "tor.exe", app_dir / "tor" / "tor" / "tor.exe",
                       Path(r"C:\Tor\tor\tor.exe"), Path(r"C:\Tor\tor.exe")]
    elif sys.platform == "darwin":
        candidates += [Path("/Applications/Tor Browser.app/Contents/MacOS/Tor/tor"),
                       Path("/opt/homebrew/bin/tor"), Path("/usr/local/bin/tor")]
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


class TorMixin:
    """Tor onion transport for :class:`MessagingBackend`."""

    def _init_tor(self: MessagingBackend) -> None:
        self._tor_control: TorControl | None = None
        self._tor_proc: asyncio.subprocess.Process | None = None
        self._tor_socks_port = 0
        self._tor_onion = ""  # our address (without port)
        self._tor_state = "off"  # off | unavailable | starting | ready | error
        self._tor_detail = ""
        self._tor_progress = 0
        self._tor_task: asyncio.Task[None] | None = None
        # Lanes: extra Tor connections carrying file chunks for a session.
        self._tor_lanes: dict[str, list[str]] = {}  # contact -> our lane peer ids
        self._lane_owner: dict[str, str] = {}  # lane peer id (either side) -> contact
        self._lane_dialing: dict[str, asyncio.Task[None]] = {}
        self._lane_tried: dict[str, float] = {}
        self._lane_idle: dict[str, asyncio.TimerHandle] = {}
        self._lane_opened: dict[str, float] = {}  # our lane peer id -> when it opened
        self._lane_cap: dict[str, tuple[int, float]] = {}  # contact -> (lanes it takes, until)

    # ── Settings / status ──

    def tor_enabled(self: MessagingBackend) -> bool:
        return bool(getattr(self.config, "tor_enabled", False)) and self.tcp_transport is not None

    def tor_ready(self: MessagingBackend) -> bool:
        return self._tor_state == "ready" and bool(self._tor_socks_port)

    def tor_status(self: MessagingBackend) -> dict[str, Any]:
        return {
            "enabled": bool(getattr(self.config, "tor_enabled", False)),
            "state": self._tor_state,
            "progress": self._tor_progress,
            "detail": self._tor_detail,
            # Shown only to you; contacts get it inside the encrypted session.
            "onion": self._tor_onion,
        }

    def tor_problem(self: MessagingBackend, hash_id: str) -> str:
        """Why Tor can't carry this contact right now, in plain words ('' = it can)."""
        if not getattr(self.config, "tor_enabled", False):
            return "it's turned off on this device (Settings → Network)"
        if self._tor_state in ("starting", "off"):
            return f"still connecting to the Tor network on this device ({self._tor_progress}%)"
        if self._tor_state in ("unavailable", "error"):
            return f"not working on this device — {self._tor_detail or 'unknown error'}"
        if not self.peer_onion(hash_id):
            return ("this contact hasn't shared a Tor address yet — open this chat once while "
                    "you're both on the same network, with Tor on and connected on both devices")
        return ""

    def _tor_on_android(self: MessagingBackend) -> bool:
        from srltcp.utils.platform import is_android

        return is_android()

    def _tor_dir(self: MessagingBackend) -> Path:
        path = self.trusted.path.parent / "tor"
        path.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o700)
        return path

    # ── Lifecycle ──

    async def start_tor(self: MessagingBackend) -> None:
        if not self.tor_enabled():
            return
        if self._tor_task and not self._tor_task.done():
            return
        self._tor_task = asyncio.create_task(self._tor_run())

    async def stop_tor(self: MessagingBackend) -> None:
        task = self._tor_task
        self._tor_task = None
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self._tor_shutdown()
        self._tor_state = "off"
        self._tor_detail = ""

    async def tor_settings_changed(self: MessagingBackend) -> None:
        if self.tor_enabled():
            await self.start_tor()
            return
        # Tell contacts our onion address is gone while the session (which may
        # itself run over Tor) still works, then stop Tor.
        if self._tor_state == "ready":
            self._tor_state = "stopping"
            await self.addresses_changed()
            await asyncio.sleep(1)
        await self.stop_tor()

    async def _tor_shutdown(self: MessagingBackend) -> None:
        if self._tor_control:
            await self._tor_control.close()  # removes our (non-detached) onion
            self._tor_control = None
        if self._tor_on_android() and not self.tor_enabled():
            await asyncio.to_thread(self._tor_stop_android)
        if self._tor_proc and self._tor_proc.returncode is None:
            self._tor_proc.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._tor_proc.wait(), timeout=5)
            if self._tor_proc.returncode is None:
                self._tor_proc.kill()
        self._tor_proc = None
        self._tor_socks_port = 0
        self._tor_onion = ""
        if self.tcp_transport:
            await self.tcp_transport.stop_onion_listener()

    async def _tor_run(self: MessagingBackend) -> None:
        """Start Tor, publish the onion service, keep watching; restart on loss."""
        backoff = 10.0
        while self.tor_enabled() and self._running:
            try:
                self._tor_state, self._tor_detail, self._tor_progress = "starting", "", 0
                await self._tor_start_once()
                backoff = 10.0
                # Watch: control connection alive = Tor alive.
                while self._tor_control and self._tor_control.connected:
                    await asyncio.sleep(5)
                    try:
                        await self._tor_control.command("GETINFO version")
                    except (TorControlError, OSError, TimeoutError):
                        break
                log.info("Tor connection lost — restarting it")
            except asyncio.CancelledError:
                raise
            except _TorUnavailable as exc:
                self._tor_state, self._tor_detail = "unavailable", str(exc)
                log.info("Tor not available: %s", exc)
                return
            except Exception as exc:  # noqa: BLE001 - keep trying
                self._tor_state, self._tor_detail = "error", str(exc)[:200]
                log.warning("Tor: %s", exc)
            await self._tor_shutdown()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300.0)

    async def _tor_start_once(self: MessagingBackend) -> None:
        tor_dir = self._tor_dir()
        port_file = tor_dir / "control.port"
        cookie = tor_dir / "control_auth_cookie"
        with contextlib.suppress(OSError):
            port_file.unlink()
        control = TorControl()
        if self._tor_on_android():
            # Bundled TorService: talk to it over its control socket, which lives
            # in this app's private storage (no TCP control port at all —
            # TorService turns cookie auth off, so a TCP port would be open to
            # every app on the phone).
            self._tor_detail = "starting the built-in Tor"
            socket_path = await asyncio.to_thread(self._tor_start_android)
            for _ in range(300):
                if Path(socket_path).exists():
                    break
                await asyncio.sleep(0.2)
            else:
                raise RuntimeError("the built-in Tor did not start (no control socket)")
            last: Exception | None = None
            for _ in range(25):  # the socket file appears just before Tor listens
                try:
                    await control.connect_unix(socket_path)
                    break
                except OSError as exc:
                    last = exc
                    await asyncio.sleep(0.2)
            else:
                raise RuntimeError(f"couldn't reach the built-in Tor: {last}")
            await control.command("AUTHENTICATE")
        else:
            await self._tor_start_process(tor_dir, port_file, cookie)
            # Wait for Tor to write its control port.
            for _ in range(300):
                if port_file.exists() and port_file.read_text().strip():
                    break
                if self._tor_proc and self._tor_proc.returncode is not None:
                    raise RuntimeError("tor exited at start — see the log")
                await asyncio.sleep(0.2)
            else:
                raise RuntimeError("Tor did not start in time")
            text = port_file.read_text().strip().splitlines()[0]  # PORT=127.0.0.1:1234
            host, port = text.split("=", 1)[1].rsplit(":", 1)
            await control.connect(host, int(port))
            await control.authenticate(cookie)
        self._tor_control = control
        self._tor_detail = "connecting to the Tor network"
        # Bootstrap (connect to the Tor network).
        deadline = asyncio.get_running_loop().time() + TOR_BOOTSTRAP_TIMEOUT
        while True:
            phase = await control.getinfo("status/bootstrap-phase")
            m = re.search(r"PROGRESS=(\d+)", phase)
            self._tor_progress = int(m.group(1)) if m else 0
            if self._tor_progress >= 100:
                break
            if asyncio.get_running_loop().time() > deadline:
                raise RuntimeError(f"Tor couldn't connect to the network ({self._tor_progress}%)")
            await asyncio.sleep(1)
        socks = await control.getinfo("net/listeners/socks")
        m = re.search(r"127\.0\.0\.1:(\d+)", socks)
        if not m:
            raise RuntimeError("Tor has no SOCKS port")
        self._tor_socks_port = int(m.group(1))
        await self._tor_publish_onion()
        self._tor_state, self._tor_detail = "ready", ""
        log.info("Tor ready; onion service published")
        await self.addresses_changed()  # tell contacts (inside E2EE) where to find us
        self.connect_known_contacts()

    async def _tor_publish_onion(self: MessagingBackend) -> None:
        assert self._tor_control and self.tcp_transport
        local_port = await self.tcp_transport.start_onion_listener()
        key_file = self._tor_dir() / "onion_key"
        key = ""
        with contextlib.suppress(OSError):
            key = key_file.read_text().strip()
        spec = key if key.startswith("ED25519-V3:") else "NEW:ED25519-V3"
        lines = await self._tor_control.command(
            f"ADD_ONION {spec} Port={ONION_VIRTUAL_PORT},127.0.0.1:{local_port}"
        )
        service_id = ""
        for line in lines:
            if line.startswith("ServiceID="):
                service_id = line.split("=", 1)[1]
            elif line.startswith("PrivateKey="):
                fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(line.split("=", 1)[1])
        if not service_id:
            raise RuntimeError("Tor did not create the onion service")
        self._tor_onion = f"{service_id}.onion"

    async def _tor_start_process(
        self: MessagingBackend, tor_dir: Path, port_file: Path, cookie: Path
    ) -> None:
        binary = _tor_binary(str(getattr(self.config, "tor_path", "") or ""))
        if not binary:
            raise _TorUnavailable(
                "Tor isn't installed on this computer. Install Tor Browser (or the Tor "
                "Expert Bundle) and restart SRLTCP, or set its tor.exe in Settings."
            )
        data = tor_dir / "data"
        data.mkdir(exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(data, 0o700)
        lines = [
            f"DataDirectory {data}",
            "SocksPort 127.0.0.1:auto",
            "ControlPort 127.0.0.1:auto",
            f"ControlPortWriteToFile {port_file}",
            "CookieAuthentication 1",
            f"CookieAuthFile {cookie}",
            f"__OwningControllerProcess {os.getpid()}",
            "ClientOnly 1",
            "AvoidDiskWrites 1",
            "Log notice stdout",
        ]
        extra = os.environ.get("SRLTCP_TOR_EXTRA_TORRC", "")
        if extra and Path(extra).is_file():  # tests: private Tor network
            lines += Path(extra).read_text().splitlines()
        torrc = tor_dir / "torrc"
        fd = os.open(torrc, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + "\n")
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            import subprocess

            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._tor_proc = await asyncio.create_subprocess_exec(
            binary, "-f", str(torrc), "--ignore-missing-torrc",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            **kwargs,
        )

    def _tor_start_android(self: MessagingBackend) -> str:
        """Bundled Tor (tor-android): write its torrc, start TorService, return
        the path of its private control socket."""
        try:
            from java import jclass

            ctx = jclass("com.chaquo.python.Python").getPlatform().getApplication()
            tor_service = jclass("org.torproject.jni.TorService")
        except Exception as exc:  # noqa: BLE001
            raise _TorUnavailable(f"built-in Tor not found ({exc})") from exc
        torrc = Path(str(tor_service.getTorrc(ctx).getAbsolutePath()))
        torrc.parent.mkdir(parents=True, exist_ok=True)
        torrc.write_text("SocksPort 127.0.0.1:auto\nClientOnly 1\nAvoidDiskWrites 1\n")
        base = Path(str(ctx.getDir("TorService", 0).getAbsolutePath()))
        intent = jclass("android.content.Intent")()
        intent.setClassName(ctx.getPackageName(), "org.torproject.jni.TorService")
        intent.setAction("org.torproject.android.intent.action.START")
        ctx.startService(intent)
        return str(base / "data" / "ControlSocket")

    def _tor_stop_android(self: MessagingBackend) -> None:
        with contextlib.suppress(Exception):
            from java import jclass

            ctx = jclass("com.chaquo.python.Python").getPlatform().getApplication()
            intent = jclass("android.content.Intent")()
            intent.setClassName(ctx.getPackageName(), "org.torproject.jni.TorService")
            ctx.stopService(intent)

    # ── Dialing ──

    def peer_onion(self: MessagingBackend, hash_id: str) -> str:
        peer = self.trusted.get(hash_id)
        onion = getattr(peer, "onion", "") if peer else ""
        return onion if onion and valid_onion(onion) else ""

    async def dial_onion(self: MessagingBackend, hash_id: str) -> str:
        """Open a Tor connection to a trusted contact's onion service."""
        onion = self.peer_onion(hash_id)
        if not onion or not self.tor_ready() or not self.tcp_transport:
            raise ConnectionError("no Tor path to this contact")
        isolation = hashlib.sha256(b"srltcp-tor-iso" + hash_id.encode()).hexdigest()[:16]
        return await self.tcp_transport.connect_socks(
            self._tor_socks_port, onion, ONION_VIRTUAL_PORT,
            timeout=TOR_DIAL_TIMEOUT, isolation=isolation,
        )

    # ── Lanes: parallel Tor circuits for file data ──

    def _link_on_tor(self: MessagingBackend, link: PeerLink) -> bool:
        return (
            link.transport == "tcp"
            and self.tcp_transport is not None
            and self.tcp_transport.is_tor(link.transport_peer_id)
        )

    def tor_circuits(self: MessagingBackend, hash_id: str) -> int:
        """Tor circuits a file to/from this contact is using right now (the
        main one plus lanes, ours or theirs); 0 when not on Tor."""
        link = self.get_link(hash_id)
        if not link or not self._link_on_tor(link):
            return 0
        return 1 + sum(1 for owner in self._lane_owner.values() if owner == hash_id)

    def _send_lanes(self: MessagingBackend, hash_id: str, link: PeerLink) -> list[str]:
        """Lanes ready to carry this contact's file chunks (opening more if
        needed). Empty off Tor, or until the first lane is up."""
        if not self._link_on_tor(link) or TOR_LANES <= 0 or not self.tcp_transport:
            if self._tor_lanes.get(hash_id):
                self._close_lanes(hash_id)  # moved off Tor (e.g. to the LAN)
            return []
        idle = self._lane_idle.pop(hash_id, None)
        if idle:
            idle.cancel()
        transport = self.tcp_transport
        lanes = [p for p in self._tor_lanes.get(hash_id, []) if transport.has_peer(p)]
        self._tor_lanes[hash_id] = lanes
        # Lanes the contact opened to us carry data just as well (they open
        # them when we don't know their onion address, see request_lanes).
        theirs = [p for p, h in self._lane_owner.items()
                  if h == hash_id and p not in lanes and transport.has_peer(p)]
        dialing = self._lane_dialing.get(hash_id)
        if not lanes and not theirs and (dialing is None or dialing.done()):
            # A file is starting over Tor: look for a direct path right away
            # (LAN, VPN, IPv6), it is many times faster than any Tor circuit.
            self.request_direct_upgrade(hash_id, punch=True)
        self._dial_lanes(hash_id, len(lanes) + len(theirs))
        return lanes + theirs

    def lanes_wanted(self: MessagingBackend, hash_id: str) -> bool:
        """Ask the receiver to open the lanes (FILE_OFFER "lr"): we can't
        without their onion address (e.g. they reached us over Tor before
        telling us theirs)."""
        link = self.get_link(hash_id)
        return bool(link and self._link_on_tor(link) and not self.peer_onion(hash_id))

    def request_lanes(self: MessagingBackend, hash_id: str) -> None:
        """The sender asked us (the receiver) to open the lanes."""
        link = self.get_link(hash_id)
        if not link or not self._link_on_tor(link) or not self.tcp_transport:
            return
        idle = self._lane_idle.pop(hash_id, None)
        if idle:
            idle.cancel()
        transport = self.tcp_transport
        have = sum(1 for p, h in self._lane_owner.items() if h == hash_id and transport.has_peer(p))
        self._dial_lanes(hash_id, have)

    def _dial_lanes(self: MessagingBackend, hash_id: str, have: int) -> None:
        dialing = self._lane_dialing.get(hash_id)
        lanes = self._tor_lanes.get(hash_id, [])
        want = TOR_LANES
        cap = self._lane_cap.get(hash_id)
        if cap and cap[1] > time.monotonic():
            want = min(want, cap[0])
        if (
            max(len(lanes), have) < want
            and (dialing is None or dialing.done())
            and time.monotonic() - self._lane_tried.get(hash_id, 0.0) > LANE_RETRY
            and self.tor_ready()
            and self.peer_onion(hash_id)
        ):
            self._lane_tried[hash_id] = time.monotonic()
            self._lane_dialing[hash_id] = asyncio.create_task(
                self._open_lanes(hash_id, want - max(len(lanes), have))
            )

    async def _open_lanes(self: MessagingBackend, hash_id: str, count: int) -> None:
        async def one() -> None:
            assert self.tcp_transport is not None
            # A fresh isolation token = a fresh circuit, separate from the
            # main one and from the other lanes.
            peer_id = await self.tcp_transport.connect_socks(
                self._tor_socks_port, self.peer_onion(hash_id), ONION_VIRTUAL_PORT,
                timeout=TOR_DIAL_TIMEOUT, isolation="lane-" + secrets.token_hex(8),
            )
            link = self.get_link(hash_id)
            if not link or not link.handshake_complete or not self._link_on_tor(link):
                await self.tcp_transport.disconnect(peer_id)
                return
            self._lane_owner[peer_id] = hash_id
            flags = Flags.ENCRYPTED | Flags.E2EE
            sealed = link.crypto.encrypt(LANE_TAG, header_aad(MessageType.LANE_JOIN, flags))
            body = link.crypto.lane_hint(outgoing=True) + sealed
            await self.tcp_transport.send(
                peer_id, build_header(MessageType.LANE_JOIN, flags=flags, body=body)
            )
            self._tor_lanes.setdefault(hash_id, []).append(peer_id)
            self._lane_opened[peer_id] = time.monotonic()

        results = await asyncio.gather(*(one() for _ in range(count)), return_exceptions=True)
        failed = [r for r in results if isinstance(r, BaseException)]
        up = len(self._tor_lanes.get(hash_id, []))
        log.info("Tor lanes to %s: %d up%s", hash_id[:8], up,
                 f", {len(failed)} failed ({failed[0]!r})" if failed else "")
        if up:
            self._lane_tried.pop(hash_id, None)  # partial: top up on the next chunk

    async def _handle_lane_join(
        self: MessagingBackend, peer: TransportPeer, flags: int, body: bytes
    ) -> None:
        """A contact attaches an extra Tor connection to its session."""
        transport = self.tcp_transport
        if peer.transport != "tcp" or not transport or not transport.is_tor(peer.peer_id):
            return
        if peer.peer_id in self._lane_owner or self.get_link_by_peer_id(peer.peer_id):
            return
        hint, sealed = body[:16], body[16:]
        for hash_id, link in list(self._links.items()):
            if not link.handshake_complete or not self.is_trusted(hash_id):
                continue
            if not hmac.compare_digest(link.crypto.lane_hint(outgoing=False), hint):
                continue
            try:
                tag = link.crypto.decrypt(sealed, header_aad(MessageType.LANE_JOIN, flags))
            except Exception:  # noqa: BLE001 - forged or replayed
                return
            if tag != LANE_TAG:
                return
            lanes = [p for p, h in self._lane_owner.items() if h == hash_id]
            if len(lanes) >= LANE_ACCEPT:
                await transport.disconnect(peer.peer_id)
                return
            self._lane_owner[peer.peer_id] = hash_id
            transport.tune_for_lane(peer.peer_id, 256 * 1024)
            log.info("Tor lane from %s joined (%d)", hash_id[:8], len(lanes) + 1)
            return

    def lane_owner(self: MessagingBackend, peer_id: str) -> str | None:
        return self._lane_owner.get(peer_id)

    def _lane_closed(self: MessagingBackend, peer_id: str) -> None:
        """A lane went away: data in flight on it is lost, so the sender goes
        back to the last confirmed byte."""
        opened = self._lane_opened.pop(peer_id, None)
        hash_id = self._lane_owner.pop(peer_id, None)
        if not hash_id:
            return
        lanes = self._tor_lanes.get(hash_id)
        if lanes and peer_id in lanes:
            lanes.remove(peer_id)
            if opened and time.monotonic() - opened < LANE_REFUSED:
                self._lane_cap[hash_id] = (max(1, len(lanes)), time.monotonic() + LANE_CAP_FOR)
        # Ours or theirs: whatever we sent on it is lost.
        for tid, out in self._out.items():
            transfer = self._transfers.get(tid)
            if transfer and transfer.recipient_hash == hash_id:
                out.rewind = True
                out.wake.set()

    def _lanes_maybe_idle(self: MessagingBackend, hash_id: str) -> None:
        """No more files to or from this contact: close our lanes soon."""
        if not self._tor_lanes.get(hash_id) or hash_id in self._lane_idle:
            return
        if any(
            self._transfers[tid].recipient_hash == hash_id
            for tid, task in self._transfer_tasks.items()
            if not task.done() and tid in self._transfers
        ) or any(
            not rx.closed and tid in self._transfers and self._transfers[tid].sender_hash == hash_id
            for tid, rx in self._in.items()
        ):
            return
        loop = asyncio.get_running_loop()
        self._lane_idle[hash_id] = loop.call_later(LANE_IDLE, self._close_lanes, hash_id)

    def _drop_lane(self: MessagingBackend, hash_id: str, peer_id: str) -> None:
        """Stop using one lane (stuck) without rewinding the transfer; a fresh
        circuit replaces it on the next chunk."""
        lanes = self._tor_lanes.get(hash_id)
        if lanes and peer_id in lanes:
            lanes.remove(peer_id)
        self._lane_owner.pop(peer_id, None)
        self._lane_opened.pop(peer_id, None)
        self._lane_tried.pop(hash_id, None)
        if self.tcp_transport:
            task = asyncio.ensure_future(self.tcp_transport.disconnect(peer_id))
            self.__dict__.setdefault("_lane_close_tasks", set()).add(task)
            task.add_done_callback(self.__dict__["_lane_close_tasks"].discard)

    def _close_lanes(self: MessagingBackend, hash_id: str) -> None:
        self._lane_idle.pop(hash_id, None)
        lanes = self._tor_lanes.pop(hash_id, [])
        transport = self.tcp_transport
        for peer_id in lanes:
            self._lane_owner.pop(peer_id, None)
            self._lane_opened.pop(peer_id, None)
            if transport:
                task = asyncio.ensure_future(transport.disconnect(peer_id))
                self.__dict__.setdefault("_lane_close_tasks", set()).add(task)
                task.add_done_callback(self.__dict__["_lane_close_tasks"].discard)

    def store_peer_onion(self: MessagingBackend, hash_id: str, data: dict[str, Any]) -> None:
        if "o" not in data:
            return
        onion = str(data.get("o", ""))[:70].lower()
        if onion and not valid_onion(onion):
            return
        peer = self.trusted.get(hash_id)
        if peer and getattr(peer, "onion", "") != onion:
            self.trusted.update(hash_id, onion=onion)


class _TorUnavailable(RuntimeError):
    pass
