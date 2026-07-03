"""SRLTCP CLI entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys

from srltcp import __version__
from srltcp.core.messaging.backend import NodeConfig
from srltcp.core.messaging.constants import DEFAULT_TCP_PORT, RELAY_TCP_PORT, WEB_PORT
from srltcp.core.node import SRLTCPNode
from srltcp.core.settings import AppSettings, SettingsStore
from srltcp.utils.logging import get_logger, setup_logging
from srltcp.utils.platform import default_serial_port
from srltcp.utils.shutdown import GracefulShutdown
from srltcp.web.server import run_web_server, shutdown_web_server

log = get_logger(__name__)

_android_web_port: dict[str, int] = {"port": WEB_PORT}


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
    web.add_argument("--tcp-port", type=int, default=DEFAULT_TCP_PORT, help="TCP transport port")
    web.add_argument("--bind", default="0.0.0.0", help="TCP transport bind address")
    web.add_argument("--serial", action="store_true", help="Enable USB serial transport")
    web.add_argument("--serial-port", default="", help="Serial device path")
    web.add_argument("--no-tcp", action="store_true", help="Disable TCP transport")
    web.add_argument("--relay", action="store_true", help="Enable relay/router mode")
    web.add_argument("--log-level", default="INFO", help="Log level")
    web.add_argument(
        "--debug",
        action="store_true",
        help="Verbose debug logging (all backend activity)",
    )

    relay = sub.add_parser("relay", help="Start headless relay server")
    relay.add_argument("--name", default="srltcp-relay", help="Relay name")
    relay.add_argument("--bind", default="0.0.0.0", help="Bind address")
    relay.add_argument("--port", type=int, default=RELAY_TCP_PORT, help="Relay TCP port")
    relay.add_argument("--log-level", default="INFO", help="Log level")

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
    return NodeConfig(
        name=name,
        bind_host=args.bind,
        tcp_port=args.tcp_port,
        relay_mode=args.relay,
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

    web_port = args.port or settings.web_port or WEB_PORT
    settings.web_port = web_port
    if args.name:
        settings.display_name = args.name

    config = _node_config_from_settings(settings, args)
    node = SRLTCPNode(config, settings)

    shutdown = GracefulShutdown()
    web_holder: dict = {}

    async def cleanup() -> None:
        if web_holder:
            await shutdown_web_server(
                node, web_holder["runner"], web_holder["site"]
            )
        await node.stop()

    shutdown.add_hook(cleanup)

    await node.start()
    runner, site, bound_port = await run_web_server(node, host="127.0.0.1", port=web_port)
    web_holder["runner"] = runner
    web_holder["site"] = site
    settings.web_port = bound_port
    store.save(settings)
    _android_web_port["port"] = bound_port

    log.info("SRLTCP v%s running — https://127.0.0.1:%d", __version__, bound_port)
    log.info("Press Ctrl+C to stop")

    await shutdown.wait()
    await shutdown.run_cleanup()


async def run_relay(args: argparse.Namespace) -> None:
    config = NodeConfig(
        name=args.name,
        bind_host=args.bind,
        tcp_port=args.port,
        relay_mode=True,
        enable_tcp=True,
        enable_serial=False,
        announce=True,
    )
    settings = AppSettings(display_name=args.name, setup_complete=True)
    node = SRLTCPNode(config, settings)
    shutdown = GracefulShutdown()
    shutdown.add_hook(node.stop)

    await node.start()
    log.info("Headless relay listening on %s:%d (Ctrl+C to stop)", args.bind, args.port)
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


def start_android_server() -> None:
    """Entry point for Chaquopy Android app (background thread)."""
    import os
    import threading

    os.environ["SRLTCP_ANDROID"] = "1"

    def _run() -> None:
        import sys

        sys.argv = ["srltcp", "web", "--log-level", "DEBUG", "--debug"]
        try:
            main()
        except Exception:
            log.exception("Android server failed")

    threading.Thread(target=_run, name="srltcp-server", daemon=False).start()


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    setup_logging(
        getattr(args, "log_level", "INFO"),
        debug=bool(getattr(args, "debug", False)),
    )

    if args.command == "web":
        asyncio.run(run_web(args))
    elif args.command == "relay":
        asyncio.run(run_relay(args))
    elif args.command == "send":
        asyncio.run(run_send(args))
    elif args.command == "identity":
        asyncio.run(run_identity())


if __name__ == "__main__":
    main()