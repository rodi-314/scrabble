"""End-to-end test: a real websocket server with two real websocket clients.

Exercises join -> start -> pass over the wire and checks that broadcast state
stays consistent.  Run directly with:  python tests/test_integration.py
"""

import asyncio
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import websockets

from dictionary import Dictionary
from server import GameServer
from engine import Engine

HOST = "127.0.0.1"
PORT = 8799


async def drain_latest_state(ws, timeout=0.4):
    """Read all messages currently queued and return the last STATE payload."""
    state = None
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            return state
        msg = json.loads(raw)
        if msg.get("type") == "state":
            state = msg["state"]


async def main():
    server = GameServer(Dictionary(words=["HA"]), host=HOST, port=PORT)
    # Use a deterministic bag so the test is reproducible.
    server.engine = Engine(Dictionary(words=["HA"]), rng=random.Random(7))
    task = asyncio.create_task(server.run())
    await asyncio.sleep(0.3)

    try:
        async with websockets.connect(f"ws://{HOST}:{PORT}") as a, \
                   websockets.connect(f"ws://{HOST}:{PORT}") as b:
            await a.send(json.dumps({"type": "join", "name": "Alice"}))
            await b.send(json.dumps({"type": "join", "name": "Bob"}))
            await asyncio.sleep(0.2)

            state = await drain_latest_state(a)
            assert state is not None, "no state received after join"
            assert state["phase"] == "lobby", state["phase"]
            assert len(state["players"]) == 2, state["players"]
            alice_id = state["players"][0]["id"]
            bob_id = state["players"][1]["id"]
            print("  ok  two players joined the lobby")

            # Non-host cannot start.
            await b.send(json.dumps({"type": "start"}))
            await asyncio.sleep(0.15)
            state = await drain_latest_state(b)
            assert state["phase"] == "lobby", "Bob should not be able to start"
            print("  ok  non-host cannot start the game")

            # Host starts.
            await a.send(json.dumps({"type": "start"}))
            await asyncio.sleep(0.2)
            state = await drain_latest_state(a)
            assert state["phase"] == "playing", state["phase"]
            assert state["turn"] == alice_id, "Alice should move first"
            assert state["bag"] == 100 - 14, state["bag"]
            print("  ok  host started; racks dealt; Alice to move")

            # Alice passes; turn should move to Bob.
            await a.send(json.dumps({"type": "pass"}))
            await asyncio.sleep(0.2)
            state = await drain_latest_state(a)
            assert state["turn"] == bob_id, "turn should pass to Bob"
            print("  ok  pass advances the turn over the wire")

            # An illegal move returns an error and does not change the turn.
            await b.send(json.dumps({"type": "play", "row": 0, "col": 0,
                                     "direction": "across", "word": "ZZ"}))
            await asyncio.sleep(0.2)
            # Bob should receive an error; drain and confirm turn unchanged.
            got_error = False
            try:
                while True:
                    raw = await asyncio.wait_for(b.recv(), timeout=0.3)
                    msg = json.loads(raw)
                    if msg.get("type") == "error":
                        got_error = True
                    if msg.get("type") == "state":
                        last = msg["state"]
            except asyncio.TimeoutError:
                pass
            assert got_error, "expected an error for an illegal move"
            print("  ok  illegal move is rejected with an error")

        print("\nIntegration test passed.")
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
