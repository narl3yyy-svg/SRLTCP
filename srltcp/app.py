"""SRLTCP CLI entry point."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys

from srltcp.core.messaging.backend import NodeConfig
from srltcp.core.messaging.constants import DEFAULT_TCP_PORT, RELAY_TCP_PORT, WEB_PORT
from srltcp.core.node import SRLTCPNode
from srltcp.utils.logging import get_logger, setup_logging
from srltcp.utils.platform import default_serial_port
from srltcp.web.server import run_web_server

log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="srltcp",
        description="SRLTCP — Secure peer-to-peer communication over Serial and TCP/IP",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # Web UI mode (default interactive)
    web = sub.add_parser("web", help="Start local web UI + P2P node")
    web.add_argument("--name", default="srltcp-node", help="Display name")
    web.add_argument("--host", default="127.0.0.1", help="Web UI bind host")
    web.add_argument("--port", type=int, default=WEB_PORT, help="Web UI port")
    web.add_argument("--tcp-port", type=int, default=DEFAULT_TCP_PORT, help="TCP transport port")
    web.add_argument("--bind", default="0.0.0.0", help="TCP transport bind address")
    web.add_argument("--serial", action="store_true", help="Enable USB serial transport")
    web.add_argument("--serial-port", default="", help="Serial device path")
    web.add_argument("--no-tcp", action="store_true", help="Disable TCP transport")
    web.add_argument("--no-announce", action="store_true", help="Disable auto-announce")
    web.add_argument("--relay", action="store_true", help="Enable relay/router mode")
    web.add_argument("--log-level", default="INFO", help="Log level")

    # Headless relay server
    relay = sub.add_parser("relay", help="Start headless relay server")
    relay.add_argument("--name", default="srltcp-relay", help="Relay name")
    relay.add_argument("--bind", default="0.0.0.0", help="Bind address")
    relay.add_argument("--port", type=int, default=RELAY_TCP_PORT, help="Relay TCP port")
    relay.add_argument("--log-level", default="INFO", help="Log level")

    # Direct CLI messaging
    send = sub.add_parser("send", help="Send a message (CLI)")
    send.add_argument("--recipient", required=True, help="Recipient hash ID")
    send.add_argument("--text", required=True, help="Message text")
    send.add_argument("--host", help="Peer host (if not discovered)")
    send.add_argument("--port", type=int, default=DEFAULT_TCP_PORT)
    send.add_argument("--name", default="srltcp-cli")
    send.add_argument("--log-level", default="WARNING")

    # Identity info
    sub.add_parser("identity", help="Show local identity hashes")

    return parser


async def run_web(args: argparse.Namespace) -> None:
    config = NodeConfig(
        name=args.name,
        bind_host=args.bind,
        tcp_port=args.tcp_port,
        relay_mode=args.relay,
        enable_tcp=not args.no_tcp,
        enable_serial=args.serial,
        serial_port=args.serial_port or default_serial_port(),
        announce=not args.no_announce,
    )
    node = SRLTCPNode(config)
    await node.start()
    runner, web_port = await run_web_server(node, host=args.host, port=args.port)

    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _signal_handler)

    log.info("SRLTCP running — open http://%s:%d", args.host, web_port)
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
    await node.start()
    await node.backend.connect_to_peer(args.recipient, host=args.host, port=args.port)
    await asyncio.sleep(2)  # allow handshake
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