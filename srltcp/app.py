"""SRLTCP CLI entry point."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys

from srltcp import __version__
from srltcp.core.messaging.backend import NodeConfig
from srltcp.core.messaging.constants import DEFAULT_TCP_PORT, RELAY_TCP_PORT, WEB_PORT
from srltcp.core.node import SRLTCPNode
from srltcp.core.settings import SettingsStore
from srltcp.utils.logging import get_logger, setup_logging
from srltcp.utils.platform import default_serial_port
from srltcp.web.server import run_web_server

log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="srltcp",
        description="SRLTCP — Secure peer-to-peer communication over Serial and TCP/IP",
    )
    parser.add_argument("--version", action="version", version=f"srltcp {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    web = sub.add_parser("web", help="Start local web UI + P2P node")
    web.add_argument("--name", default="", help="Display name")
    web.add_argument("--host", default="127.0.0.1", help="Web UI bind host")
    web.add_argument("--port", type=int, default=0, help="Web UI port")
    web.add_argument("--tcp-port", type=int, default=0, help="TCP transport port")
    web.add_argument("--bind", default="", help="TCP transport bind address")
    web.add_argument("--serial", action="store_true", help="Enable USB serial transport")
    web.add_argument("--serial-port", default="", help="Serial device path")
    web.add_argument("--no-tcp", action="store_true", help="Disable TCP transport")
    web.add_argument("--announce", action="store_true", help="Enable auto-announce")
    web.add_argument("--no-announce", action="store_true", help="Disable auto-announce")
    web.add_argument("--relay", action="store_true", help="Enable relay/router mode")
    web.add_argument("--log-level", default="INFO", help="Log level")

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


def _config_from_settings(store: SettingsStore, args: argparse.Namespace) -> NodeConfig:
    s = store.settings
    name = args.name or s.display_name
    if args.name:
        store.update(display_name=name)
    announce = s.auto_announce
    if args.announce:
        announce = True
    if args.no_announce:
        announce = False
    enable_serial = s.enable_serial or args.serial
    if args.serial:
        store.update(enable_serial=True)
    return NodeConfig(
        name=name,
        bind_host=args.bind or s.bind_interface,
        tcp_port=args.tcp_port or s.tcp_port,
        relay_mode=args.relay or s.relay_mode,
        enable_tcp=not args.no_tcp and s.enable_tcp,
        enable_serial=enable_serial,
        serial_port=args.serial_port or s.serial_port or default_serial_port(),
        serial_baud=s.serial_baud,
        announce=announce,
    )


async def run_web(args: argparse.Namespace) -> None:
    store = SettingsStore()
    if not store.settings.setup_complete:
        store.update(setup_complete=True, version=__version__)
    config = _config_from_settings(store, args)
    store.update(
        auto_announce=config.announce,
        display_name=config.name,
        tcp_port=config.tcp_port,
        relay_mode=config.relay_mode,
    )
    node = SRLTCPNode(config, store)
    await node.start()
    port = args.port or store.settings.web_port or WEB_PORT
    runner = await run_web_server(node, host=args.host, port=port)

    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _signal_handler)

    log.info("SRLTCP %s — open http://%s:%d", __version__, args.host, port)
    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        await node.stop()
        await runner.cleanup()


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
    node = SRLTCPNode(config)
    await node.start()
    log.info("Headless relay listening on %s:%d", args.bind, args.port)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        await node.stop()


async def run_send(args: argparse.Namespace) -> None:
    config = NodeConfig(name=args.name, enable_serial=False, announce=False)
    node = SRLTCPNode(config)
    from srltcp.core.trusted import TrustedPeer

    node.backend.trusted.add(
        TrustedPeer(hash_id=args.recipient, name="cli-peer", transport="tcp")
    )
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
        identity = store.load_or_create("srltcp-node", transport)  # type: ignore[arg-type]
        print(f"[{transport}] {identity.name}")
        print(f"  hash: {identity.hash_id}")
        print(f"  public_key: {identity.public_bytes().hex()[:32]}…")


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    setup_logging(getattr(args, "log_level", "INFO"))

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