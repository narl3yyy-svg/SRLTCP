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

Tor itself: on Android it's bundled (tor-android's TorService). On a PC the
app starts an installed ``tor`` (Tor Browser's or the Tor Expert Bundle's, or
``tor`` on PATH) with its own private data directory.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import os
import re
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from srltcp.utils.logging import get_logger

if TYPE_CHECKING:
    from srltcp.core.messaging.backend import MessagingBackend

log = get_logger(__name__)

ONION_VIRTUAL_PORT = 7825
TOR_DIAL_TIMEOUT = 60.0
TOR_BOOTSTRAP_TIMEOUT = 180.0
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
        self._tor_lock = asyncio.Lock()

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
        from srltcp.utils.platform import is_android

        if is_android():
            await asyncio.to_thread(self._tor_start_android, tor_dir, port_file, cookie)
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
        control = TorControl()
        await control.connect(host, int(port))
        await control.authenticate(cookie)
        self._tor_control = control
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

    def _tor_start_android(self: MessagingBackend, tor_dir: Path, port_file: Path,
                           cookie: Path) -> None:
        """Bundled Tor (tor-android): write its torrc, start TorService."""
        try:
            from java import jclass

            python = jclass("com.chaquo.python.Python")
            ctx = python.getPlatform().getApplication()
            tor_service = jclass("org.torproject.jni.TorService")
        except Exception as exc:  # noqa: BLE001
            raise _TorUnavailable(f"bundled Tor not found ({exc})") from exc
        torrc = Path(str(tor_service.getTorrc(ctx).getAbsolutePath()))
        torrc.write_text(
            "\n".join(
                [
                    "ControlPort 127.0.0.1:auto",
                    f"ControlPortWriteToFile {port_file}",
                    f"CookieAuthFile {cookie}",
                    "ClientOnly 1",
                ]
            )
            + "\n"
        )
        intent = jclass("android.content.Intent")(
            ctx, jclass("java.lang.Class").forName("org.torproject.jni.TorService")
        )
        intent.setAction(tor_service.ACTION_START)
        ctx.startService(intent)

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
