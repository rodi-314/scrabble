"""End-to-end tests for the newer features over the real encrypted server:

  * word checker (the 'check' command);
  * teaming mode with PRIVATE team chat (teammates see it; nobody else does);
  * the per-turn time limit (a stalling player is force-passed);
  * AI players (they actually move) and the post-move coach (top plays + a
    comment), delivered privately to the player who moved.

The AI / coach scenario uses the bundled dictionary so the generator has real
words to work with; if that word list is missing it is skipped with a note.

Run directly with:  python tests/test_features.py
"""

import asyncio
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import websockets

import crypto
import dictionary as dmod
from dictionary import Dictionary
from server import GameServer

HOST = "127.0.0.1"
KEY = "feature-room-key-aa11"


async def secure(ws):
    hello = await asyncio.wait_for(ws.recv(), timeout=3.0)
    salt, n, r, p = crypto.parse_hello(hello)
    return crypto.cipher_for(KEY, salt, n, r, p)


async def send(ws, cipher, obj):
    await ws.send(cipher.encrypt(json.dumps(obj)))


async def drain(ws, cipher, timeout=0.4):
    out = []
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            return out
        out.append(json.loads(cipher.decrypt(raw)))


async def welcome(ws, cipher):
    while True:
        m = json.loads(cipher.decrypt(await asyncio.wait_for(ws.recv(), 3.0)))
        if m.get("type") == "welcome":
            return m


def last_state(msgs):
    st = None
    for m in msgs:
        if m.get("type") == "state":
            st = m["state"]
    return st


async def serve(port, dictionary, **kw):
    srv = GameServer(dictionary, host=HOST, port=port, passphrase=KEY, **kw)
    srv.engine.rng = random.Random(7)
    srv.engine.bag = srv.engine._new_bag()
    srv.ai_rng = random.Random(3)
    srv.ai_delay_range = (0, 0)            # no artificial think delay in tests
    task = asyncio.create_task(srv.run())
    await asyncio.sleep(0.3)
    return srv, task


async def stop(task):
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


SMALL = Dictionary(words=["HELLO", "HA", "QI", "ZA", "CAT", "DOG"])


# -------------------------------------------------------------------- check
async def scenario_check():
    srv, task = await serve(8810, SMALL)
    try:
        async with websockets.connect(f"ws://{HOST}:8810") as a:
            ca = await secure(a)
            await send(a, ca, {"type": "join", "name": "Alice"})
            await welcome(a, ca)
            await send(a, ca, {"type": "check", "words": ["hello", "ZZZZZ", "qi"]})
            checked = [m for m in await drain(a, ca) if m.get("type") == "checked"]
            assert checked, "no 'checked' reply"
            res = {row[0]: row[1] for row in checked[-1]["results"]}
            assert res == {"HELLO": True, "ZZZZZ": False, "QI": True}, res
            # the value (face points) is reported too
            vals = {row[0]: row[2] for row in checked[-1]["results"]}
            assert vals["QI"] == 11, vals       # Q(10) + I(1)
        print("  ok  check reports validity + face value")
    finally:
        await stop(task)


# --------------------------------------------------------------- team chat
async def scenario_team_private_chat():
    srv, task = await serve(8811, SMALL)
    try:
        async with websockets.connect(f"ws://{HOST}:8811") as a, \
                   websockets.connect(f"ws://{HOST}:8811") as b, \
                   websockets.connect(f"ws://{HOST}:8811") as c, \
                   websockets.connect(f"ws://{HOST}:8811") as s:
            ca, cb, cc, cs = (await secure(a), await secure(b),
                              await secure(c), await secure(s))
            await send(a, ca, {"type": "join", "name": "Alice"}); await welcome(a, ca)
            await send(b, cb, {"type": "join", "name": "Bob"}); await welcome(b, cb)
            await send(c, cc, {"type": "join", "name": "Carol"}); await welcome(c, cc)
            await send(s, cs, {"type": "join", "name": "Sam", "spectator": True})
            await welcome(s, cs)
            await send(a, ca, {"type": "setteam", "team": "Red"})
            await send(c, cc, {"type": "setteam", "team": "red"})   # same team, any case
            await send(b, cb, {"type": "setteam", "team": "Blue"})
            await asyncio.sleep(0.2)
            for ws, ci in ((a, ca), (b, cb), (c, cc), (s, cs)):
                await drain(ws, ci)
            # the lobby state shows teaming is active
            await send(a, ca, {"type": "say", "text": "warmup"})
            st = last_state(await drain(a, ca)) or {}
            # Alice (Red) sends a PRIVATE team message.
            await send(a, ca, {"type": "chat", "scope": "team", "text": "PLANALPHA"})
            await asyncio.sleep(0.2)
            am = [m for m in await drain(a, ca) if m.get("type") == "chat"]
            bm = [m for m in await drain(b, cb) if m.get("type") == "chat"]
            cm = [m for m in await drain(c, cc) if m.get("type") == "chat"]
            sm = [m for m in await drain(s, cs) if m.get("type") == "chat"]
            assert any(m["text"] == "PLANALPHA" and m.get("scope") == "team" for m in am), \
                "sender should see their own team message"
            assert any(m["text"] == "PLANALPHA" for m in cm), \
                "a teammate (Red) must receive the private message"
            assert not any(m.get("text") == "PLANALPHA" for m in bm), \
                "the other team (Blue) must NOT receive it"
            assert not any(m.get("text") == "PLANALPHA" for m in sm), \
                "a spectator must NOT receive a team message"
        print("  ok  team chat reaches only teammates (not the other team or spectators)")
    finally:
        await stop(task)


async def scenario_team_chat_requires_team():
    srv, task = await serve(8812, SMALL)
    try:
        async with websockets.connect(f"ws://{HOST}:8812") as a:
            ca = await secure(a)
            await send(a, ca, {"type": "join", "name": "Alice"}); await welcome(a, ca)
            # No team set yet -> a team message is refused with a helpful error.
            await send(a, ca, {"type": "chat", "scope": "team", "text": "hi team"})
            errs = [m for m in await drain(a, ca) if m.get("type") == "error"]
            assert any("not in a team" in m["message"].lower() for m in errs), errs
        print("  ok  team chat without a team is rejected")
    finally:
        await stop(task)


# -------------------------------------------------------------- turn limit
async def scenario_turn_limit():
    srv, task = await serve(8813, SMALL, turn_limit=1)
    try:
        async with websockets.connect(f"ws://{HOST}:8813") as a, \
                   websockets.connect(f"ws://{HOST}:8813") as b:
            ca, cb = await secure(a), await secure(b)
            await send(a, ca, {"type": "join", "name": "Alice"}); wa = await welcome(a, ca)
            await send(b, cb, {"type": "join", "name": "Bob"}); wb = await welcome(b, cb)
            await send(a, ca, {"type": "start"})
            await asyncio.sleep(0.3)
            st = last_state(await drain(a, ca, 0.2))
            assert st["turn"] == wa["id"] and st["turn_limit"] == 1, st
            # Alice stalls; poll until the server force-passes her to Bob.
            forced = False
            turn_after = None
            for _ in range(20):
                st = last_state(await drain(b, cb, 0.2))
                if st:
                    turn_after = st["turn"]
                    if any("ran out of time" in ln for ln in st["log"]):
                        forced = True
                        break
            assert forced, "expected a forced (timed-out) pass in the log"
            assert turn_after == wb["id"], f"turn should have moved to Bob, got {turn_after}"
        print("  ok  a stalling player is force-passed when the clock runs out")
    finally:
        await stop(task)


# ----------------------------------------------------------- AI + coaching
async def scenario_ai_and_coach():
    full = dmod.load()
    if len(full) == 0:
        print("  skip AI/coach (no bundled dictionary found)")
        return
    srv, task = await serve(8814, full)
    try:
        async with websockets.connect(f"ws://{HOST}:8814") as a:
            ca = await secure(a)
            await send(a, ca, {"type": "join", "name": "Alice"}); wa = await welcome(a, ca)
            # Host adds an expert AI, then starts.
            await send(a, ca, {"type": "addai", "level": "expert"})
            await asyncio.sleep(0.2)
            st = last_state(await drain(a, ca))
            ais = [p for p in st["players"] if p.get("is_ai")]
            assert ais and ais[0]["ai_level"] == "expert", st["players"]
            await send(a, ca, {"type": "start"})
            await asyncio.sleep(0.3)
            await drain(a, ca)
            # Alice plays a real, valid first word so the coach has something to grade.
            srv.engine.by_id[wa["id"]].rack = list("HELLOST")
            await send(a, ca, {"type": "play", "row": 7, "col": 7,
                               "direction": "across", "word": "HELLO"})
            # The coach + the AI both build the word trie off-loop on first use
            # (~1-2s), so poll generously.
            coach = None
            ai_moved = False
            for _ in range(60):
                for m in await drain(a, ca, 0.2):
                    if m.get("type") == "coach":
                        coach = m
                    if m.get("type") == "state":
                        s = m["state"]
                        if s["turn"] == wa["id"] and len(s["log"]) >= 2:
                            ai_moved = True
                if coach and ai_moved:
                    break
            assert coach is not None, "expected a private coach message after the play"
            assert coach["played"]["word"] == "HELLO"
            assert isinstance(coach["best"], list) and coach["best"], "coach should list top plays"
            assert isinstance(coach["comment"], str) and coach["comment"]
            assert ai_moved, "the AI player never took its turn"
        print(f"  ok  AI moved + private coach delivered (comment: {coach['comment']!r})")
    finally:
        await stop(task)


async def scenario_turn_limit_keeps_enforcing_on_loopback():
    """Regression: with the only opponent offline, the turn loops back to the
    same human after each force-pass. The clock must keep re-arming (not just
    fire once), so a stalling player can never indefinitely freeze the game."""
    srv, task = await serve(8816, SMALL, turn_limit=1)
    try:
        async with websockets.connect(f"ws://{HOST}:8816") as a, \
                   websockets.connect(f"ws://{HOST}:8816") as b:
            ca, cb = await secure(a), await secure(b)
            await send(a, ca, {"type": "join", "name": "Alice"}); await welcome(a, ca)
            await send(b, cb, {"type": "join", "name": "Bob"}); await welcome(b, cb)
            await send(a, ca, {"type": "start"})
            await asyncio.sleep(0.3)
            # Bob (the non-current opponent) drops; Alice stalls forever.
            await b.close()
            forced = 0
            for _ in range(80):                 # up to ~8s
                await asyncio.sleep(0.1)
                forced = sum(1 for ln in srv.engine.log if "ran out of time" in ln)
                if forced >= 2 or srv.engine.phase == "over":
                    break
            assert forced >= 2, (f"clock stopped re-arming on loop-back: "
                                 f"{forced} forced pass(es), phase={srv.engine.phase}")
        print("  ok  turn limit keeps re-arming when the turn loops back to one player")
    finally:
        await stop(task)


async def scenario_ai_keeps_playing_after_human_leaves():
    """Regression: when the only human disconnects, the turn loops back to the
    AI (every other seat offline). The AI must keep moving (and the game must
    reach its end) instead of freezing forever on the AI's turn."""
    full = dmod.load()
    if len(full) == 0:
        print("  skip AI-freeze regression (no bundled dictionary found)")
        return
    srv, task = await serve(8815, full)
    try:
        a = await websockets.connect(f"ws://{HOST}:8815")
        ca = await secure(a)
        await send(a, ca, {"type": "join", "name": "Alice"}); await welcome(a, ca)
        await send(a, ca, {"type": "addai", "level": "expert"})
        await asyncio.sleep(0.2)
        await drain(a, ca)
        await send(a, ca, {"type": "start"})
        await asyncio.sleep(0.3)
        before = len(srv.engine.log)
        # The human vanishes; the AI is now the only connected seat.
        await a.close()
        # The AI should drive the game forward on its own and run it to the end
        # (no freeze). Poll the in-process engine for progress.
        progressed = False
        for _ in range(80):                  # up to ~8s
            await asyncio.sleep(0.1)
            if srv.engine.phase == "over" or len(srv.engine.log) > before + 3:
                progressed = True
                break
        assert progressed, (f"game froze after the human left: phase={srv.engine.phase} "
                            f"log grew {len(srv.engine.log) - before}")
        print(f"  ok  AI kept playing after the human left (phase={srv.engine.phase})")
    finally:
        await stop(task)


async def run_all():
    await scenario_check()
    await scenario_team_private_chat()
    await scenario_team_chat_requires_team()
    await scenario_turn_limit()
    await scenario_turn_limit_keeps_enforcing_on_loopback()
    await scenario_ai_and_coach()
    await scenario_ai_keeps_playing_after_human_leaves()
    print("\nFeature tests passed.")


if __name__ == "__main__":
    asyncio.run(run_all())
