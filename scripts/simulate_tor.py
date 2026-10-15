"""Two SRLTCP nodes reach each other over Tor only (private Tor network).

    bash scripts/tor_testnet.sh start /tmp/srltcp-tornet     # wait ~60 s
    SRLTCP_TOR_EXTRA_TORRC=/tmp/srltcp-tornet/client.torrc \\
        .venv/bin/python scripts/simulate_tor.py

Checks: onion addresses are exchanged inside the encrypted session; with the
direct path gone, a message and a file are delivered over Tor; a stranger who knows the
onion address is refused; turning Tor off removes the address from contacts.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import srltcp.core  # noqa: E402,F401  (import order: core before transports)
from srltcp.core.messaging.backend import MessagingBackend, NodeConfig  # noqa: E402
from srltcp.core.trusted import TrustedPeer  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}: {name}{' — ' + detail if detail else ''}", flush=True)
    if not ok:
        FAILS.append(name)


ANDROID_STYLE = "--android-style" in sys.argv


def _mimic_android(backend: MessagingBackend) -> None:
    """Start tor the way tor-android's TorService does (private control socket,
    --CookieAuthentication 0) and drive it through the Android code path."""
    import subprocess

    backend._tor_on_android = lambda: True  # type: ignore[method-assign]

    def start() -> str:
        d = backend._tor_dir() / "android"
        (d / "data").mkdir(parents=True, exist_ok=True)
        os.chmod(d / "data", 0o700)
        sock = d / "data" / "ControlSocket"
        torrc = d / "torrc"
        torrc.write_text("SocksPort 127.0.0.1:auto\nClientOnly 1\nAvoidDiskWrites 1\n")
        extra = Path(os.environ["SRLTCP_TOR_EXTRA_TORRC"]).read_text()
        (d / "torrc-defaults").write_text(extra)
        backend._android_tor = subprocess.Popen(  # type: ignore[attr-defined]
            ["tor", "-f", str(torrc), "--defaults-torrc", str(d / "torrc-defaults"),
             "--DataDirectory", str(d / "data"), "--ControlSocket", str(sock),
             "--CookieAuthentication", "0", "--RunAsDaemon", "0",
             "__OwningControllerProcess", str(os.getpid())],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return str(sock)

    backend._tor_start_android = start  # type: ignore[method-assign]
    backend._tor_stop_android = lambda: backend._android_tor.terminate()  # type: ignore


def node(base: Path, name: str, tor: bool = True) -> MessagingBackend:
    data = base / name
    data.mkdir(parents=True, exist_ok=True)
    os.environ["SRLTCP_DATA_DIR"] = str(data)
    backend = MessagingBackend(NodeConfig(
        name=name, bind_host="127.0.0.1", tcp_port=0, discovery_port=0, strict_ports=True,
        incoming_dir=str(data / "in"), tor_enabled=tor, relay_enabled=False,
    ))
    if ANDROID_STYLE and name == "alice":
        _mimic_android(backend)
    return backend


def trust(a: MessagingBackend, b: MessagingBackend) -> None:
    ident = b.identities["tcp"]
    a.trusted.add(TrustedPeer(hash_id=ident.hash_id, name=ident.name,
                              public_key=ident.public_bytes().hex(),
                              tcp_host="127.0.0.1", tcp_port=b.config.tcp_port))


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def wait_for(pred, timeout: float) -> bool:  # type: ignore[no-untyped-def]
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.5)
    return False


def texts(n: MessagingBackend) -> list[str]:
    return [m["text"] for m in n.get_messages(limit=500)]


async def main() -> int:
    if not os.environ.get("SRLTCP_TOR_EXTRA_TORRC"):
        print("Set SRLTCP_TOR_EXTRA_TORRC (see the docstring).")
        return 2
    base = Path(tempfile.mkdtemp(prefix="srltcp-tor-"))
    a, b, m = node(base, "alice"), node(base, "bob"), node(base, "mallory")
    for n in (a, b, m):
        await n.start()
    try:
        ok = await wait_for(lambda: a.tor_ready() and b.tor_ready(), 240)
        check("both nodes started Tor and published an onion service", ok,
              f"alice={a.tor_status()}, bob={b.tor_status()}")
        if not ok:
            return 1
        trust(a, b)
        trust(b, a)
        ah, bh = a.identities["tcp"].hash_id, b.identities["tcp"].hash_id
        await a.connect_to_peer(bh)
        await a.wait_for_handshake(bh, timeout=10)
        ok = await wait_for(lambda: b.trusted.get(ah).onion == a._tor_onion
                            and a.trusted.get(bh).onion == b._tor_onion, 20)
        check("onion addresses exchanged inside the encrypted session", ok)
        # Different networks now: saved addresses stop working.
        for x, y in ((a, b), (b, a)):
            x.trusted.update(y.identities["tcp"].hash_id, tcp_host="127.0.0.1",
                             tcp_port=closed_port(), alt_hosts=[])
        await a.disconnect_peer(bh)
        await b.disconnect_peer(ah)
        await asyncio.sleep(1)
        start = time.monotonic()
        # Nobody sends anything: they must find each other over Tor by themselves
        # (what happens when one phone leaves the Wi-Fi for mobile data).
        ok = await wait_for(lambda: bool(a.get_link(bh) and a.get_link(bh).handshake_complete), 120)
        check("contacts reconnect over Tor by themselves after the direct path is gone", ok,
              f"{time.monotonic() - start:.0f}s")
        start = time.monotonic()
        await a.send_message(bh, "hello over tor")
        ok = await wait_for(lambda: "hello over tor" in texts(b), 120)
        link = a.get_link(bh)
        check("message delivered over Tor with no direct path", ok,
              f"{time.monotonic() - start:.1f}s via {link.address if link else '-'}")
        await b.send_message(ah, "reply over tor")
        ok = await wait_for(lambda: "reply over tor" in texts(a), 60)
        check("reply delivered back over the same Tor session", ok)
        src = base / "photo.jpg"
        src.write_bytes(os.urandom(8_000_000))
        start = time.monotonic()
        sent = await a.send_file(bh, src)
        tid = sent["id"] if sent else ""
        lanes_seen = 0
        shown = {"a": 0, "b": 0}  # what each side's file bubble says (Tor ×N)

        def done() -> bool:
            nonlocal lanes_seen
            lanes_seen = max(lanes_seen, len(a._tor_lanes.get(bh, [])))
            shown["a"] = max(shown["a"], a.tor_circuits(bh))
            shown["b"] = max(shown["b"], b.tor_circuits(ah))
            return tid in b._transfers and b._transfers[tid].state == "complete"

        ok = await wait_for(done, 180)
        took = time.monotonic() - start
        check("8 MB file delivered over Tor", ok and b._incoming_paths[tid].read_bytes()
              == src.read_bytes(), f"{took:.1f}s, {8 / took:.2f} MB/s")
        check("file data spread over parallel Tor circuits (lanes)", lanes_seen >= 2,
              f"{lanes_seen} lanes")
        check("both sides show how many Tor circuits carry the file",
              shown["a"] == shown["b"] == lanes_seen + 1, f"sender {shown['a']}, "
              f"receiver {shown['b']}")
        ok = await wait_for(lambda: a._transfers[tid].state == "complete", 20)
        check("sender sees the file verified by the receiver", ok)
        await b.send_message(ah, "chat after a file over tor")
        ok = await wait_for(lambda: "chat after a file over tor" in texts(a), 30)
        check("chat still flows on the main circuit", ok)

        # A stranger who somehow knows Alice's onion address gets nothing.
        m.trusted.add(TrustedPeer(hash_id=ah, name="alice", public_key=a.identities[
            "tcp"].public_bytes().hex(), onion=a._tor_onion))
        with contextlib.suppress(Exception):
            await m.connect_to_peer(ah)
        await asyncio.sleep(25)
        check("non-contact refused over Tor (no session, no contact request)",
              a.get_link(m.identities["tcp"].hash_id) is None
              and m.identities["tcp"].hash_id not in a._contact_requests)

        # Turning Tor off: contacts are told the address is gone.
        a.config.tor_enabled = False
        await a.tor_settings_changed()
        ok = await wait_for(lambda: b.trusted.get(ah).onion == "", 30)
        check("Tor off: contacts drop the onion address", ok)
        # Same onion address after a restart (contacts keep using it).
        before = b._tor_onion
        await b.stop()
        b = node(base, "bob")
        await b.start()
        ok = await wait_for(b.tor_ready, 240)
        check("onion address survives an app restart", ok and b._tor_onion == before,
              f"{before[:12]}… -> {b._tor_onion[:12]}…")
    finally:
        for n in (a, b, m):
            await n.stop()
    print("All Tor checks passed." if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    if os.environ.get("SRLTCP_SIM_LOG"):
        import logging

        logging.basicConfig(filename=os.environ["SRLTCP_SIM_LOG"], level=logging.INFO,
                            format="%(asctime)s %(name)s %(levelname)s %(message)s")
    sys.exit(asyncio.run(main()))
