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
    try:
        from server import GameServer
        import crypto
        import wordsmith
    except ImportError as exc:
        print("Encryption support is required to host a game:")
        print(f"  {exc}")
        return

    deck = dictionary_mod.load(args.dictpath, enabled=not args.no_dict)
    # Strip first, THEN fall back: a whitespace-only --key must not become an
    # empty (trivially guessable) room key.
    key = (args.key or "").strip()
    room_key = key or crypto.gen_room_key()
    turn_limit = max(0, args.turn_limit)
    coach = not args.no_coach
    ai_specs = []
    for level in (args.ai or []):
        level = level.strip().lower()
        if level in wordsmith.LEVELS:
            ai_specs.append(level)
        else:
            print(f"  (ignoring unknown AI level '{level}'; use easy/medium/hard/expert)")
    ip = get_local_ip()
    bar = "=" * 56
    print(bar)
    print("  LAN SCRABBLE  -  server")
    print(bar)
    print(f"  Listening on {args.host}:{args.port}  (encrypted: AES-256-GCM)")
    if args.no_dict:
        print("  Dictionary : disabled (any letters accepted)")
    else:
        print(f"  Dictionary : {len(deck)} words loaded")
    print(f"  Turn limit : {turn_limit}s per move (auto-pass)" if turn_limit
          else "  Turn limit : none")
    print(f"  Coaching   : {'on (top plays + comments after each move)' if coach else 'off'}")
    if ai_specs:
        print(f"  AI players : {', '.join(ai_specs)}  (seated when you join)")
    if args.no_dict and (ai_specs or coach):
        print("  Note       : AI players and coaching need the dictionary; they")
        print("               do little with --no-dict.")
    print()
    print(f"  Room key   : {room_key}")
    print("  Share this key with your players -- nobody can join (or sniff the")
    print("  game) without it.")
    print()
    print("  Players on your LAN can join with:")
    print(f"      python scrabble.py join {ip} --port {args.port} --key {room_key}")
    print()
    print("  You can play too: open a second terminal and run that join command.")
    print("  Press Ctrl+C to stop the server.")
    print(bar)

    server = GameServer(deck, host=args.host, port=args.port, passphrase=room_key,
                        turn_limit=turn_limit, coach=coach, ai_specs=ai_specs)
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        print("\nServer stopped.")


def _connect(args, spectator):
    try:
        from client import Client
        import crypto  # noqa: F401  (surface a missing-dependency error here)
    except ImportError as exc:
        print("Encryption support is required to join a game:")
        print(f"  {exc}")
        return

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
        prompt = "Enter a spectator name: " if spectator else "Enter your name: "
        try:
            name = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            return
    if not name:
        name = "Spectator" if spectator else "Player"

    room_key = (args.key or "").strip()      # strip --key like the prompt path does
    if not room_key:
        try:
            room_key = input("Room key (shown by the host): ").strip()
        except (EOFError, KeyboardInterrupt):
            return
    if not room_key:
        print("A room key is required to join. The host shows it; pass it with --key.")
        return

    uri = f"ws://{address}:{port}"
    # Only use colour when the terminal can actually process ANSI and the user
    # has not opted out; otherwise escape codes would print as literal garbage.
    use_color = ansi_ok and not args.no_color
    verb = "Spectating" if spectator else "Connecting to"
    print(f"{verb} {uri} ...")
    try:
        asyncio.run(Client(uri, name, color=use_color, spectator=spectator,
                           passphrase=room_key).run())
    except KeyboardInterrupt:
        print("\nLeft the game.")


def cmd_join(args):
    _connect(args, spectator=False)


def cmd_spectate(args):
    _connect(args, spectator=True)


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
    host.add_argument("--key", default=None,
                      help="Room key for encryption (default: a strong random key is generated).")
    host.add_argument("--turn-limit", type=int, default=0, dest="turn_limit",
                      help="Seconds per turn before a player is forced to pass (0 = no limit).")
    host.add_argument("--ai", action="append", default=None, metavar="LEVEL",
                      help="Seat an AI player (easy|medium|hard|expert). Repeat for more bots.")
    host.add_argument("--no-coach", action="store_true",
                      help="Disable post-move coaching (top plays + comments).")
    host.set_defaults(func=cmd_host)

    join = sub.add_parser("join", help="Join a game as a player.")
    join.add_argument("address", help="Host IP address, optionally with :port.")
    join.add_argument("--port", type=int, default=DEFAULT_PORT,
                      help=f"Server port (default {DEFAULT_PORT}).")
    join.add_argument("--name", default=None, help="Your display name.")
    join.add_argument("--no-color", action="store_true",
                      help="Disable ANSI colours (for very basic terminals).")
    join.add_argument("--key", default=None,
                      help="Room key shown by the host (you are prompted if omitted).")
    join.set_defaults(func=cmd_join)

    spectate = sub.add_parser("spectate", help="Watch a game without playing.")
    spectate.add_argument("address", help="Host IP address, optionally with :port.")
    spectate.add_argument("--port", type=int, default=DEFAULT_PORT,
                          help=f"Server port (default {DEFAULT_PORT}).")
    spectate.add_argument("--name", default=None, help="Display name shown to players.")
    spectate.add_argument("--no-color", action="store_true",
                          help="Disable ANSI colours (for very basic terminals).")
    spectate.add_argument("--key", default=None,
                          help="Room key shown by the host (you are prompted if omitted).")
    spectate.set_defaults(func=cmd_spectate)

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
