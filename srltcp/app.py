"""SRLTCP CLI entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys

from srltcp import __version__
from srltcp.core.messaging.backend import NodeConfig
from srltcp.core.messaging.constants import (
    DEFAULT_HUB_PORT,
    DEFAULT_TCP_PORT,
    DISCOVERY_PORT,
    WEB_PORT,
)
from srltcp.core.node import SRLTCPNode
from srltcp.core.settings import AppSettings, SettingsStore
from srltcp.utils.logging import get_logger, setup_logging
from srltcp.utils.platform import default_serial_port
from srltcp.utils.shutdown import GracefulShutdown
from srltcp.web.server import run_web_server, shutdown_web_server

log = get_logger(__name__)

_android_web_port: dict[str, int] = {"port": WEB_PORT}
_android_server_ready: bool = False
_android_server_started: bool = False


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

    hub = sub.add_parser("hub", help="Start headless connection hub server")
    hub.add_argument("--name", default="srltcp-hub", help="Hub display name (logs only)")
    hub.add_argument("--bind", default="0.0.0.0", help="Bind address")
    hub.add_argument("--port", type=int, default=DEFAULT_HUB_PORT, help="Hub TCP port")
    hub.add_argument("--log-level", default="INFO", help="Log level")

    send = sub.add_parser("send", help="Send a message (CLI)")
    send.add_argument("--recipient", required=True, help="Recipient hash ID")
    send.add_argument("--text", required=True, help="Message text")
    send.add_argument("--host", help="Peer host (if not discovered)")
    send.add_argument("--port", type=int, default=DEFAULT_TCP_PORT)
    send.add_argument("--name", default="srltcp-cli")
    send.add_argument("--log-level", default="WARNING")

    sub.add_parser("identity", help="Show local identity hashes")

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
        hub_enabled=settings.hub_enabled,
        hub_host=settings.hub_host,
        hub_port=settings.hub_port or DEFAULT_HUB_PORT,
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
    settings.version = __version__

    from srltcp.utils.platform import is_android

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

    log.info("SRLTCP v%s running — https://127.0.0.1:%d", __version__, bound_port)
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


async def run_hub(args: argparse.Namespace) -> None:
    config = NodeConfig(
        name=args.name,
        bind_host=args.bind,
        tcp_port=args.port,
        hub_mode=True,
        enable_tcp=True,
        enable_serial=False,
        announce=False,
    )
    settings = AppSettings(display_name=args.name, setup_complete=True)
    node = SRLTCPNode(config, settings)
    shutdown = GracefulShutdown()
    shutdown.add_hook(node.stop)

    await node.start()
    log.info("Headless hub listening on %s:%d (Ctrl+C to stop)", args.bind, args.port)
    await shutdown.wait()
    await shutdown.run_cleanup()


async def run_send(args: argparse.Namespace) -> None:
    settings = AppSettings()
    config = NodeConfig(name=args.name, enable_serial=False, announce=False)
    node = SRLTCPNode(config, settings)
    await node.start()
    await node.backend.connect_to_peer(args.recipient, host=args.host, port=args.port)
    await asyncio.sleep(2)
    msg = await node.backend.send_message(args.recipient, args.text)
    if msg:
        print(f"Sent: {msg.id} ({msg.status})")
    else:
        print("Failed to send", file=sys.stderr)
        sys.exit(1)
    await asyncio.sleep(1)
    await node.stop()


async def run_identity() -> None:
    from srltcp.core.identity import IdentityStore

    store = IdentityStore()
    for transport in ("tcp", "serial"):
        identity = store.load_or_create("srltcp-node", transport)
        print(f"[{transport}] {identity.name}")
        print(f"  hash: {identity.hash_id}")
        print(f"  public_key: {identity.public_bytes().hex()[:32]}…")


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
    """Entry point for Android Chaquopy runtime."""
    import os
    import sys

    global _android_server_ready, _android_server_started
    if _android_server_started and is_android_server_ready():
        return
    _android_server_started = False
    _android_server_ready = False
    _android_server_started = True

    os.environ["SRLTCP_ANDROID"] = "1"
    sys.argv = ["srltcp", "web", "--log-level", "INFO"]

    try:
        main()
    except Exception:
        _android_server_started = False
        _android_server_ready = False
        import logging

        logging.exception("Android server failed")
        raise

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    setup_logging(
        getattr(args, "log_level", "INFO"),
        debug=bool(getattr(args, "debug", False)),
    )

    if args.command == "web":
        asyncio.run(run_web(args))
    elif args.command == "hub":
        asyncio.run(run_hub(args))
    elif args.command == "send":
        asyncio.run(run_send(args))
    elif args.command == "identity":
        asyncio.run(run_identity())


if __name__ == "__main__":
    main()
