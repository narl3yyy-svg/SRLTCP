"""Two SRLTCP nodes reach each other over Tor only (private Tor network).

    bash scripts/tor_testnet.sh start /tmp/srltcp-tornet     # wait ~60 s
    SRLTCP_TOR_EXTRA_TORRC=/tmp/srltcp-tornet/client.torrc \\
        .venv/bin/python scripts/simulate_tor.py

Checks: onion addresses are exchanged inside the encrypted session; with the
direct path gone, a message is delivered over Tor; a stranger who knows the
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


def node(base: Path, name: str, tor: bool = True) -> MessagingBackend:
    data = base / name
    data.mkdir(parents=True, exist_ok=True)
    os.environ["SRLTCP_DATA_DIR"] = str(data)
    return MessagingBackend(NodeConfig(
        name=name, bind_host="127.0.0.1", tcp_port=0, discovery_port=0, strict_ports=True,
        incoming_dir=str(data / "in"), tor_enabled=tor, relay_enabled=False,
    ))


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
              f"alice={a.tor_status()['state']} {a.tor_status()['detail']}")
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
        await a.send_message(bh, "hello over tor")
        ok = await wait_for(lambda: "hello over tor" in texts(b), 120)
        link = a.get_link(bh)
        check("message delivered over Tor with no direct path", ok,
              f"{time.monotonic() - start:.1f}s via {link.address if link else '-'}")
        await b.send_message(ah, "reply over tor")
        ok = await wait_for(lambda: "reply over tor" in texts(a), 60)
        check("reply delivered back over the same Tor session", ok)

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
    sys.exit(asyncio.run(main()))
