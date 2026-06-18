#!/usr/bin/env python3
"""LAN Scrabble — command line entry point.

    python scrabble.py host                 # host a game (run the server)
    python scrabble.py join <HOST_IP>       # join a game as a player

Run with no arguments or --help for full options.
"""

import argparse
import asyncio
import sys

import dictionary as dictionary_mod
from util import enable_ansi, get_local_ip

DEFAULT_PORT = 8765


def cmd_host(args):
    from server import GameServer

    deck = dictionary_mod.load(args.dictpath, enabled=not args.no_dict)
    ip = get_local_ip()
    bar = "=" * 56
    print(bar)
    print("  LAN SCRABBLE  -  server")
    print(bar)
    print(f"  Listening on {args.host}:{args.port}")
    if args.no_dict:
        print("  Dictionary : disabled (any letters accepted)")
    else:
        print(f"  Dictionary : {len(deck)} words loaded")
    print()
    print("  Players on your LAN can join with:")
    print(f"      python scrabble.py join {ip} --port {args.port}")
    print()
    print("  You can play too: open a second terminal and run that join command.")
    print("  Press Ctrl+C to stop the server.")
    print(bar)

    server = GameServer(deck, host=args.host, port=args.port)
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        print("\nServer stopped.")


def cmd_join(args):
    from client import Client

    ansi_ok = enable_ansi()
    address = args.address
    port = args.port
    if address.startswith("ws://"):
        address = address[len("ws://"):]
    if address.count(":") == 1:
        address, _, port_str = address.partition(":")
        port = int(port_str)

    name = args.name
    if not name:
        try:
            name = input("Enter your name: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
    if not name:
        name = "Player"

    uri = f"ws://{address}:{port}"
    # Only use colour when the terminal can actually process ANSI and the user
    # has not opted out; otherwise escape codes would print as literal garbage.
    use_color = ansi_ok and not args.no_color
    print(f"Connecting to {uri} ...")
    try:
        asyncio.run(Client(uri, name, color=use_color).run())
    except KeyboardInterrupt:
        print("\nLeft the game.")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="scrabble", description="Multiplayer LAN Scrabble over websockets."
    )
    sub = parser.add_subparsers(dest="command")

    host = sub.add_parser("host", help="Host a game (run the server).")
    host.add_argument("--host", default="0.0.0.0",
                      help="Interface to bind (default 0.0.0.0 = all LAN interfaces).")
    host.add_argument("--port", type=int, default=DEFAULT_PORT,
                      help=f"Port to listen on (default {DEFAULT_PORT}).")
    host.add_argument("--dict", dest="dictpath", default=None,
                      help="Path to a custom word list (one or more words per line).")
    host.add_argument("--no-dict", action="store_true",
                      help="Disable word validation (accept any letters).")
    host.set_defaults(func=cmd_host)

    join = sub.add_parser("join", help="Join a game as a player.")
    join.add_argument("address", help="Host IP address, optionally with :port.")
    join.add_argument("--port", type=int, default=DEFAULT_PORT,
                      help=f"Server port (default {DEFAULT_PORT}).")
    join.add_argument("--name", default=None, help="Your display name.")
    join.add_argument("--no-color", action="store_true",
                      help="Disable ANSI colours (for very basic terminals).")
    join.set_defaults(func=cmd_join)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 1
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
