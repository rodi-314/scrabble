"""End-to-end test: a real (encrypted) websocket server with real clients.

Exercises the full wire protocol over AES-256-GCM: the cleartext key-agreement
handshake, then join -> start -> play/pass with every frame encrypted.  Also
covers spectators, shuffle, reconnect (incl. the takeover race), and that a
client with the wrong room key is rejected.

Run directly with:  python tests/test_integration.py
"""

import asyncio
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import websockets

import crypto
from dictionary import Dictionary
from server import GameServer
from engine import Engine

HOST = "127.0.0.1"
PORT = 8799
PORT2 = 8800
PORT3 = 8801
PORT4 = 8802
KEY = "test-room-key-7f3a2b"          # shared room key used by the test server


def make_server(port):
    server = GameServer(Dictionary(words=["HA"]), host=HOST, port=port, passphrase=KEY)
    # Deterministic bag so the test is reproducible.
    server.engine = Engine(Dictionary(words=["HA"]), rng=random.Random(7))
    return server


async def secure(ws, key=KEY):
    """Complete the cleartext handshake and return this connection's cipher."""
    hello = await asyncio.wait_for(ws.recv(), timeout=2.0)
    salt, n, r, p = crypto.parse_hello(hello)
    return crypto.cipher_for(key, salt, n, r, p)


async def send(ws, cipher, obj):
    await ws.send(cipher.encrypt(json.dumps(obj)))


async def drain_latest_state(ws, cipher, timeout=0.4):
    """Read all queued messages and return the last STATE payload."""
    state = None
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            return state
        msg = json.loads(cipher.decrypt(raw))
        if msg.get("type") == "state":
            state = msg["state"]


async def drain_latest(ws, cipher, timeout=0.4):
    """Return the latest (state, rack) seen, draining everything queued."""
    state = rack = None
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            return state, rack
        msg = json.loads(cipher.decrypt(raw))
        if msg.get("type") == "state":
            state = msg["state"]
        elif msg.get("type") == "rack":
            rack = msg["rack"]


async def read_welcome(ws, cipher, timeout=2.0):
    """Read messages until the WELCOME handshake arrives."""
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        msg = json.loads(cipher.decrypt(raw))
        if msg.get("type") == "welcome":
            return msg


async def saw_error(ws, cipher, timeout=0.4):
    """True if an ERROR message arrives within the timeout."""
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        if json.loads(cipher.decrypt(raw)).get("type") == "error":
            return True


async def main():
    server = make_server(PORT)
    task = asyncio.create_task(server.run())
    await asyncio.sleep(0.3)
    uri = f"ws://{HOST}:{PORT}"

    try:
        async with websockets.connect(uri) as a, websockets.connect(uri) as b:
            ca = await secure(a)
            cb = await secure(b)
            print("  ok  encrypted handshake completed")
            # Serialize the joins so Alice is unambiguously the first player /
            # host (the per-connection scrypt handshake makes raw join order racy).
            await send(a, ca, {"type": "join", "name": "Alice"})
            await read_welcome(a, ca)
            await send(b, cb, {"type": "join", "name": "Bob"})
            await read_welcome(b, cb)
            await asyncio.sleep(0.2)

            state = await drain_latest_state(a, ca)
            assert state is not None, "no state received after join"
            assert state["phase"] == "lobby", state["phase"]
            assert len(state["players"]) == 2, state["players"]
            assert "last_move" in state and state["last_move"] == [], state.get("last_move")
            alice_id = state["players"][0]["id"]
            bob_id = state["players"][1]["id"]
            print("  ok  two players joined the lobby (over the encrypted link)")

            # Non-host cannot start.
            await send(b, cb, {"type": "start"})
            await asyncio.sleep(0.15)
            state = await drain_latest_state(b, cb)
            assert state["phase"] == "lobby", "Bob should not be able to start"
            print("  ok  non-host cannot start the game")

            # Host starts.
            await send(a, ca, {"type": "start"})
            await asyncio.sleep(0.2)
            state = await drain_latest_state(a, ca)
            assert state["phase"] == "playing", state["phase"]
            assert state["turn"] == alice_id, "Alice should move first"
            assert state["bag"] == 100 - 14, state["bag"]
            print("  ok  host started; racks dealt; Alice to move")

            # Alice passes; turn should move to Bob.
            await send(a, ca, {"type": "pass"})
            await asyncio.sleep(0.2)
            state = await drain_latest_state(a, ca)
            assert state["turn"] == bob_id, "turn should pass to Bob"
            print("  ok  pass advances the turn over the wire")

            # An illegal move returns an error and does not change the turn.
            await send(b, cb, {"type": "play", "row": 0, "col": 0,
                               "direction": "across", "word": "ZZ"})
            await asyncio.sleep(0.2)
            got_error = False
            try:
                while True:
                    raw = await asyncio.wait_for(b.recv(), timeout=0.3)
                    msg = json.loads(cb.decrypt(raw))
                    if msg.get("type") == "error":
                        got_error = True
            except asyncio.TimeoutError:
                pass
            assert got_error, "expected an error for an illegal move"
            print("  ok  illegal move is rejected with an error")

        print("basic flow ok")
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def scenario_spectator_and_shuffle():
    """A spectator watches but cannot play; a player can shuffle their rack."""
    server = make_server(PORT2)
    task = asyncio.create_task(server.run())
    await asyncio.sleep(0.3)
    uri = f"ws://{HOST}:{PORT2}"
    try:
        async with websockets.connect(uri) as a, \
                   websockets.connect(uri) as b, \
                   websockets.connect(uri) as s:
            ca, cb, cs = await secure(a), await secure(b), await secure(s)
            await send(a, ca, {"type": "join", "name": "Alice"})
            await read_welcome(a, ca)
            await send(b, cb, {"type": "join", "name": "Bob"})
            await read_welcome(b, cb)
            await send(s, cs, {"type": "join", "name": "Watcher", "spectator": True})
            sw = await read_welcome(s, cs)
            assert sw.get("spectator") is True, sw
            assert sw["id"] < 0, "spectators should use a separate negative id space"
            print("  ok  spectator joined with a spectator welcome")

            await send(a, ca, {"type": "start"})
            await asyncio.sleep(0.2)
            st, _ = await drain_latest(s, cs)
            assert st["phase"] == "playing", st["phase"]
            assert st.get("spectators") == ["Watcher"], st.get("spectators")
            print("  ok  spectator sees live state with the spectator roster")

            await send(s, cs, {"type": "pass"})
            assert await saw_error(s, cs), "spectator should not be able to act"
            print("  ok  spectator cannot play")

            _, rack0 = await drain_latest(a, ca)
            assert rack0 and len(rack0) == 7, rack0
            await send(a, ca, {"type": "shuffle"})
            _, rack1 = await drain_latest(a, ca)
            assert rack1 is not None, "shuffle should re-send the rack"
            assert sorted(rack1) == sorted(rack0), (rack0, rack1)
            print("  ok  shuffle preserves the rack's tiles")
        print("spectator + shuffle ok")
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def scenario_reconnect():
    """A dropped player reclaims their seat with the token -- even over a still
    live stale connection (the reconnect race that must not evict the seat)."""
    server = make_server(PORT3)
    task = asyncio.create_task(server.run())
    await asyncio.sleep(0.3)
    uri = f"ws://{HOST}:{PORT3}"
    try:
        async with websockets.connect(uri) as b:
            cb = await secure(b)
            a = await websockets.connect(uri)
            ca = await secure(a)
            await send(a, ca, {"type": "join", "name": "Alice"})
            wa = await read_welcome(a, ca)
            token, alice_id = wa["token"], wa["id"]
            await send(b, cb, {"type": "join", "name": "Bob"})
            await read_welcome(b, cb)
            await send(a, ca, {"type": "start"})
            await asyncio.sleep(0.2)
            st, _ = await drain_latest(b, cb)
            assert st["phase"] == "playing", st["phase"]

            # --- clean drop, then reconnect with the token ---
            await a.close()
            await asyncio.sleep(0.4)
            st, _ = await drain_latest(b, cb)
            alice = next(p for p in st["players"] if p["id"] == alice_id)
            assert alice["connected"] is False, "drop should mark Alice offline"

            a2 = await websockets.connect(uri)
            ca2 = await secure(a2)
            await send(a2, ca2, {"type": "join", "name": "Alice", "token": token})
            wa2 = await read_welcome(a2, ca2)
            assert wa2["id"] == alice_id, "reconnect must reuse the same seat id"
            _, rack = await drain_latest(a2, ca2)
            assert rack and len(rack) == 7, "reconnect should restore the rack"
            st, _ = await drain_latest(b, cb)
            alice = next(p for p in st["players"] if p["id"] == alice_id)
            assert alice["connected"] is True, "reconnect should restore the seat"
            print("  ok  dropped player reconnects with the token")

            # --- takeover of a still-live stale seat (the race) ---
            a3 = await websockets.connect(uri)
            ca3 = await secure(a3)
            await send(a3, ca3, {"type": "join", "name": "Alice", "token": token})
            wa3 = await read_welcome(a3, ca3)
            assert wa3["id"] == alice_id
            await asyncio.sleep(0.6)
            st, _ = await drain_latest(b, cb)
            alice = next(p for p in st["players"] if p["id"] == alice_id)
            assert alice["connected"] is True, "reconnect race evicted the live seat"
            print("  ok  fast reconnect takes over without evicting the seat")
            try:
                await a2.close()
            except Exception:
                pass
            await a3.close()
        print("reconnect ok")
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def scenario_wrong_key():
    """A client with the wrong room key cannot read game data or join."""
    server = make_server(PORT4)
    task = asyncio.create_task(server.run())
    await asyncio.sleep(0.3)
    uri = f"ws://{HOST}:{PORT4}"
    try:
        async with websockets.connect(uri) as w:
            # Derive a cipher from a DIFFERENT room key than the server's.
            bad = await secure(w, key="the-wrong-key")
            await send(w, bad, {"type": "join", "name": "Mallory"})
            # The server cannot authenticate our join, so it drops us; any frame
            # it had sent is unreadable with our wrong key.
            rejected = False
            try:
                raw = await asyncio.wait_for(w.recv(), timeout=2.0)
                try:
                    bad.decrypt(raw)
                    assert False, "wrong-key client decrypted a server frame"
                except crypto.DecryptError:
                    rejected = True            # got a frame, but unreadable
            except (asyncio.TimeoutError,
                    websockets.exceptions.ConnectionClosed):
                rejected = True                # dropped, as expected
            assert rejected, "wrong key was not rejected"
        print("  ok  wrong room key is rejected (no readable game data)")
        print("wrong key ok")
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def run_all():
    await main()
    await scenario_spectator_and_shuffle()
    await scenario_reconnect()
    await scenario_wrong_key()
    print("\nIntegration test passed.")


if __name__ == "__main__":
    asyncio.run(run_all())
