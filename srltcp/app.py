"""SRLTCP CLI entry point."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys

from srltcp import __version__
from srltcp.core.messaging.backend import NodeConfig
from srltcp.core.messaging.constants import (
    DEFAULT_TCP_PORT,
    DISCOVERY_PORT,
    WEB_PORT,
)
from srltcp.core.node import SRLTCPNode
from srltcp.core.relay_proto import DEFAULT_RELAY_PORT
from srltcp.core.settings import AppSettings, SettingsStore
from srltcp.utils.logging import get_logger, setup_logging
from srltcp.utils.platform import default_serial_port
from srltcp.utils.shutdown import GracefulShutdown
from srltcp.web.server import run_web_server, shutdown_web_server

log = get_logger(__name__)

_android_web_port: dict[str, int] = {"port": WEB_PORT}
_android_server_ready: bool = False
_android_server_started: bool = False
_running_node: dict[str, object] = {}


def _android_port_open(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="srltcp",
        description="SRLTCP — Secure peer-to-peer communication over Serial and TCP/IP",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    web = sub.add_parser("web", help="Start local HTTPS web UI + P2P node")
    web.add_argument("--name", default="", help="Display name (overrides saved settings)")
    web.add_argument(
        "--port",
        type=int,
        default=0,
        help=f"HTTPS web UI port (default from settings or {WEB_PORT})",
    )
    web.add_argument(
        "--tcp-port",
        type=int,
        default=0,
        help=f"TCP transport port (default from settings or {DEFAULT_TCP_PORT})",
    )
    web.add_argument(
        "--discovery-port",
        type=int,
        default=0,
        help=f"UDP discovery port (default from settings or {DISCOVERY_PORT})",
    )
    web.add_argument("--bind", default="0.0.0.0", help="TCP transport bind address")
    web.add_argument("--serial", action="store_true", help="Enable USB serial transport")
    web.add_argument("--serial-port", default="", help="Serial device path")
    web.add_argument("--no-tcp", action="store_true", help="Disable TCP transport")
    web.add_argument("--log-level", default="INFO", help="Log level")
    web.add_argument(
        "--debug",
        action="store_true",
        help="Verbose debug logging (all backend activity)",
    )

    for name in ("relay", "hub"):  # "hub" kept as an alias for older docs
        relay = sub.add_parser(
            name,
            help="Run an internet relay (on a server with a public address)"
            if name == "relay"
            else "Alias for 'relay'",
        )
        relay.add_argument("--bind", default="0.0.0.0", help="Bind address (:: for IPv6 too)")
        relay.add_argument("--port", type=int, default=DEFAULT_RELAY_PORT, help="TCP port")
        relay.add_argument(
            "--public-host", default="", help="Public IP or hostname to put in the relay code"
        )
        relay.add_argument("--data-dir", default="", help="Where the relay keeps its keys")
        relay.add_argument("--log-level", default="INFO", help="Log level")

    send = sub.add_parser("send", help="Send a message (CLI)")
    send.add_argument("--recipient", required=True, help="Recipient hash ID")
    send.add_argument("--text", required=True, help="Message text")
    send.add_argument("--host", help="Peer host (if not discovered)")
    send.add_argument("--port", type=int, default=DEFAULT_TCP_PORT)
    send.add_argument("--name", default="srltcp-cli")
    send.add_argument("--log-level", default="WARNING")

    sub.add_parser("identity", help="Show local identity hashes")
    url = sub.add_parser("url", help="Print the private web UI link (contains the access token)")
    url.add_argument("--port", type=int, default=0, help="Web UI port (default from settings)")

    return parser


def _node_config_from_settings(settings: AppSettings, args: argparse.Namespace) -> NodeConfig:
    name = args.name or settings.display_name
    tcp_port = args.tcp_port or settings.tcp_port or DEFAULT_TCP_PORT
    discovery_port = args.discovery_port or settings.discovery_port or DISCOVERY_PORT
    return NodeConfig(
        name=name,
        bind_host=args.bind,
        tcp_port=tcp_port,
        discovery_port=discovery_port,
        strict_ports=settings.strict_ports,
        relay_enabled=settings.relay_enabled,
        relay_code=settings.relay_code,
        direct_internet=settings.direct_internet,
        tor_enabled=settings.tor_enabled,
        tor_path=settings.tor_path,
        enable_tcp=not args.no_tcp,
        enable_serial=args.serial or settings.enable_serial,
        serial_port=args.serial_port or settings.serial_port or default_serial_port(),
        serial_baud=settings.serial_baud,
        announce=settings.auto_announce,
        lan_ip=settings.lan_ip,
        incoming_dir=str(settings.resolved_incoming_dir()),
        message_retention_hours=settings.message_retention_hours,
    )


async def run_web(args: argparse.Namespace) -> None:
    store = SettingsStore()
    settings = store.load()

    from srltcp.utils.platform import is_android

    migrate_settings(settings, android=is_android())
    settings.version = __version__

    if is_android():
        settings.strict_ports = False

    requested_web_port = args.port or settings.web_port or WEB_PORT
    if args.name:
        settings.display_name = args.name

    config = _node_config_from_settings(settings, args)

    if is_android():
        config.enable_serial = False
        config.serial_port = ""
    node = SRLTCPNode(config, settings)

    shutdown = GracefulShutdown()
    _running_node["node"] = node
    _running_node["loop"] = asyncio.get_running_loop()
    web_holder: dict = {}
    port_holder = {"web": requested_web_port, "bound": requested_web_port}

    async def cleanup() -> None:
        if web_holder:
            log.info("Closing web UI on port %d", port_holder["bound"])
            await shutdown_web_server(
                node, web_holder["runner"], web_holder["site"]
            )
        await node.stop()

    shutdown.add_hook(cleanup)

    await node.start()
    runner, site, bound_port = await run_web_server(
        node,
        host="127.0.0.1",
        port=requested_web_port,
        strict=settings.strict_ports,
    )
    web_holder["runner"] = runner
    web_holder["site"] = site
    port_holder["bound"] = bound_port
    settings.web_port = requested_web_port
    store.save(settings)
    _android_web_port["port"] = bound_port
    global _android_server_ready
    _android_server_ready = True

    from srltcp.web.auth import load_or_create_token

    log.info("SRLTCP v%s running", __version__)
    if not is_android():
        # Printed (not logged) so it does not end up in shared log files.
        print(
            "\n  Open SRLTCP in your browser (keep this link private):\n"
            f"  https://127.0.0.1:{bound_port}/?token={load_or_create_token()}\n",
            flush=True,
        )
    if bound_port != requested_web_port:
        log.warning(
            "Configured web port %d was busy — bound %d instead. "
            "Stop other SRLTCP instances or enable strict ports.",
            requested_web_port,
            bound_port,
        )
    log.info("Press Ctrl+C to stop")

    await shutdown.wait()
    await shutdown.run_cleanup()


def _version_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in str(version).split(".")[:3]:
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def migrate_settings(settings: AppSettings, *, android: bool) -> bool:
    """One-time fixes when upgrading from older versions. Returns True if changed."""
    changed = False
    if settings.setup_complete and _version_tuple(settings.version) < (0, 3, 0) and android:
        # 0.2.x setup pinned the first interface (usually Wi-Fi), which goes
        # stale when the phone changes networks, and left discovery off.
        if settings.lan_ip:
            settings.lan_ip = ""
            changed = True
        if not settings.auto_announce:
            settings.auto_announce = True
            changed = True
    return changed


async def run_relay_command(args: argparse.Namespace) -> None:
    from srltcp.core.relay_server import run_relay

    await run_relay(args.bind, args.port, args.public_host, args.data_dir or None)


async def run_send(args: argparse.Namespace) -> None:
    settings = AppSettings()
    config = NodeConfig(
        name=args.name, enable_serial=False, announce=False, tcp_port=0, discovery_port=0
    )
    node = SRLTCPNode(config, settings)
    await node.start()
    try:
        backend = node.backend
        if not backend.trusted.is_trusted(args.recipient):
            print("Recipient is not a trusted contact (pair first)", file=sys.stderr)
            sys.exit(1)
        await backend.connect_to_peer(args.recipient, host=args.host, port=args.port)
        msg = await backend.send_message(args.recipient, args.text)
        if not msg:
            print("Failed to send", file=sys.stderr)
            sys.exit(1)
        for _ in range(100):
            if msg.status in ("delivered", "not_trusted"):
                break
            await asyncio.sleep(0.1)
        print(f"{msg.id}: {msg.status}")
        if msg.status != "delivered":
            sys.exit(2)
    finally:
        await node.stop()


async def run_identity() -> None:
    from srltcp.core.identity import IdentityStore

    store = IdentityStore()
    for transport in ("tcp", "serial"):
        identity = store.load_or_create("srltcp-node", transport)
        print(f"[{transport}] {identity.name}")
        print(f"  hash: {identity.hash_id}")
        print(f"  public_key: {identity.public_bytes().hex()[:32]}…")


def run_url(args: argparse.Namespace) -> None:
    from srltcp.web.auth import load_or_create_token

    settings = SettingsStore().load()
    port = args.port or settings.web_port or WEB_PORT
    print(f"https://127.0.0.1:{port}/?token={load_or_create_token()}")


def get_android_web_token() -> str:
    """Access token the Android WebView uses for its first page load."""
    from srltcp.web.auth import load_or_create_token

    return load_or_create_token()


def get_android_cert_sha256() -> str:
    """SHA-256 of the localhost certificate (DER) so the WebView can pin it."""
    from srltcp.utils.tls import localhost_cert_sha256

    return localhost_cert_sha256()


def android_import_pairing_card(uri: str) -> str:
    """Import a contact card received over NFC (called from a Java thread).

    Returns JSON for the web UI: the paired contact, or ``{"error": ...}``.
    NFC requires physical proximity, so the card owner is trusted directly.
    """
    import json

    from srltcp.core.messaging.pairing import PairingError

    node = _running_node.get("node")
    loop = _running_node.get("loop")
    if node is None or not isinstance(loop, asyncio.AbstractEventLoop):
        return json.dumps({"error": "SRLTCP is still starting"})
    assert isinstance(node, SRLTCPNode)
    future = asyncio.run_coroutine_threadsafe(
        node.backend.import_pairing_card(uri), loop
    )
    try:
        peer = future.result(timeout=10)
    except PairingError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        log.warning("NFC pairing import failed: %s", exc)
        return json.dumps({"error": "pairing failed"})
    return json.dumps({"hash_id": peer.get("hash_id"), "name": peer.get("name")})


def android_decline_call() -> None:
    """Decline button on the ringing notification (called from a Java thread)."""
    node = _running_node.get("node")
    loop = _running_node.get("loop")
    if node is None or not isinstance(loop, asyncio.AbstractEventLoop):
        return
    assert isinstance(node, SRLTCPNode)
    future = asyncio.run_coroutine_threadsafe(node.decline_pending_call(), loop)
    try:
        future.result(timeout=10)
    except Exception as exc:  # noqa: BLE001 - best effort
        log.warning("Declining the call failed: %s", exc)


def android_transfer_file(transfer_id: str) -> str:
    """Locate a transfer's file so the Android app can copy it where the user picks.

    Returns JSON ``{"path", "filename", "mime"}`` or ``{"error": ...}``.
    """
    import json
    import mimetypes

    from srltcp.core.protocol.messages import is_valid_transfer_id

    node = _running_node.get("node")
    if node is None:
        return json.dumps({"error": "SRLTCP is still starting"})
    assert isinstance(node, SRLTCPNode)
    if not is_valid_transfer_id(transfer_id):
        return json.dumps({"error": "invalid transfer"})
    path = node.backend.transfer_file_path(transfer_id)
    transfer = node.backend._transfers.get(transfer_id)
    if path is None or transfer is None:
        return json.dumps({"error": "file not ready"})
    name = transfer.filename or path.name
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return json.dumps({"path": str(path), "filename": name, "mime": mime})


def get_android_web_port() -> int:
    """Return bound HTTPS port for Android WebView."""
    return _android_web_port["port"]


def is_android_server_ready() -> bool:
    """True once the HTTPS site is bound and still accepting connections."""
    global _android_server_ready
    if not _android_server_ready:
        return False
    port = _android_web_port["port"]
    if _android_port_open(port):
        return True
    _android_server_ready = False
    return False


def start_android_server() -> None:
    """Entry point for the Android Chaquopy runtime (blocks while the node runs).

    The Kotlin side guarantees a single server thread per process; this guard
    only protects against an accidental second call while one is running.
    """
    import os
    import sys

    global _android_server_ready, _android_server_started
    if _android_server_started:
        return
    _android_server_started = True
    _android_server_ready = False

    os.environ["SRLTCP_ANDROID"] = "1"
    sys.argv = ["srltcp", "web", "--log-level", "INFO"]
    try:
        main()
    except Exception:
        import logging

        logging.exception("Android server failed")
        raise
    finally:
        _android_server_started = False
        _android_server_ready = False


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    setup_logging(
        getattr(args, "log_level", "INFO"),
        debug=bool(getattr(args, "debug", False)),
    )

    if args.command == "web":
        asyncio.run(run_web(args))
    elif args.command in ("relay", "hub"):
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(run_relay_command(args))
    elif args.command == "send":
        asyncio.run(run_send(args))
    elif args.command == "identity":
        asyncio.run(run_identity())
    elif args.command == "url":
        run_url(args)


if __name__ == "__main__":
    main()
