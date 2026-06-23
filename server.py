"""Async websocket server that hosts one Scrabble game for a LAN.

All game mutations run through :class:`engine.Engine` while holding a single lock,
so moves are processed one at a time.  Outbound messages are never sent while
the lock is held: each connection has its own outbound queue drained by a
dedicated writer task.  Broadcasting is therefore a fast, synchronous enqueue,
and a slow or dead client can never stall the rest of the game.
"""

import asyncio
import concurrent.futures
import random

import websockets

import crypto
import protocol as P
import wordsmith
from constants import BOARD_SIZE, RACK_SIZE, TILE_VALUES
from engine import Engine, MoveError

QUEUE_LIMIT = 128        # per-connection outbound backlog before we shed frames
SEND_TIMEOUT = 5         # seconds before a stalled send drops its connection
AI_DELAY_RANGE = (0.7, 1.8)   # seconds an AI "thinks" before moving (feels human)

_AI_NAMES = ["Robo", "Botworth", "Tilebot", "Lexi", "Wordy", "Qwerty",
             "Maxbot", "Vowelle", "Scrabbot", "Anagram"]


class GameServer:
    def __init__(self, dictionary, host="0.0.0.0", port=8765, passphrase="",
                 turn_limit=0, coach=True, ai_specs=None):
        self.engine = Engine(dictionary)
        self.host = host
        self.port = port
        self.passphrase = passphrase    # shared room key; every frame is encrypted
        self.turn_limit = max(0, int(turn_limit or 0))   # 0 == no per-turn clock
        self.coach = bool(coach)        # post-move "top plays + comment" coaching
        self.pending_ai = list(ai_specs or [])  # AI levels to seat once a host joins
        self._seeded_ai = False
        self.conns = {}          # conn id -> websocket (players and spectators)
        self.queues = {}         # conn id -> asyncio.Queue of serialized frames
        self.writers = {}        # conn id -> writer task
        self.spectators = {}     # conn id (negative) -> spectator display name
        self._next_spec = 0      # spectator id generator (counts down: -1, -2, ...)
        self.lock = asyncio.Lock()
        # Turn-driven async work (the per-turn clock and AI movers), keyed by a
        # generation counter so a stale timer/mover can never act on a turn that
        # has already moved on.
        self.ai_rng = random.Random()
        self.ai_delay_range = AI_DELAY_RANGE
        self._turn_gen = 0
        self._last_turn_sig = None
        self._turn_task = None    # per-turn countdown -> force pass
        self._ai_task = None      # AI "think then move" task
        # A small dedicated pool for game compute (move generation, coaching, the
        # one-time trie build) so it never contends with the per-connection
        # scrypt handshake on the default executor (which would stall new joins).
        self._compute_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="scrabble-compute")

    # ---------------------------------------------------------- board snapshot
    def _board_cells(self):
        """A plain 15x15 grid (None or (LETTER, is_blank)) for the move generator."""
        g = self.engine.board.grid
        return [[(g[r][c].letter, g[r][c].is_blank) if g[r][c] else None
                 for c in range(BOARD_SIZE)] for r in range(BOARD_SIZE)]

    def _is_host(self, pid):
        """True if ``pid`` is the host -- the first *human* player, who alone may
        start the game and add/remove AI seats."""
        host = self.engine.first_human()
        return host is not None and host.id == pid

    def _ai_name(self, level):
        """A unique, friendly display name for a new AI seat."""
        for base in _AI_NAMES:
            cand = f"{base}({level})"
            if cand.lower() not in self.engine.by_name:
                return cand
        i = 1
        while f"AI-{level}-{i}".lower() in self.engine.by_name:
            i += 1
        return f"AI-{level}-{i}"

    def _seed_ai(self):
        """Add any AI players requested on the command line, once the first human
        host has joined (so the host -- not a bot -- owns seat #1 / 'start')."""
        self._seeded_ai = True
        for level in self.pending_ai:
            if level not in wordsmith.LEVELS:
                continue
            try:
                self.engine.add_ai_player(self._ai_name(level), level)
            except MoveError:
                break        # the table filled up

    # -------------------------------------------------------- outbound plumbing
    def _enqueue(self, pid, obj):
        """Queue one message for a player (synchronous, never blocks).

        If the client is so far behind that its queue is full, drop the oldest
        frame to make room — state frames supersede one another, so the client
        still converges to the latest board.
        """
        queue = self.queues.get(pid)
        if queue is None:
            return
        data = P.dumps(obj)
        try:
            queue.put_nowait(data)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:
                pass

    def broadcast_state(self):
        """Enqueue the public state to everyone, plus each player's private rack.

        Synchronous: safe to call while holding ``self.lock`` since it performs
        no network I/O.  Per-queue FIFO ordering preserves move order per client.
        Spectators receive the public state only -- they hold no rack.
        """
        public = self.engine.public_state()
        public["spectators"] = list(self.spectators.values())
        public["turn_limit"] = self.turn_limit
        state = {"type": P.STATE, "state": public}
        for pid in list(self.conns):
            self._enqueue(pid, state)
            if pid not in self.spectators:
                self._enqueue(pid, {"type": P.RACK, "rack": self.engine.rack_of(pid)})
        # Every state change re-evaluates the per-turn clock and AI movers; both
        # are no-ops unless whose-turn-it-is actually changed.
        self._schedule_turn_driven()

    def broadcast_chat(self, name, text):
        msg = {"type": P.CHATMSG, "name": name, "text": text, "scope": "all"}
        for pid in list(self.conns):
            self._enqueue(pid, msg)

    def broadcast_team_chat(self, team, name, text):
        """A private message delivered only to connected members of ``team``
        (never to spectators or the other team)."""
        msg = {"type": P.CHATMSG, "name": name, "text": text,
               "scope": "team", "team": team}
        for pid in list(self.conns):
            if pid in self.spectators:
                continue
            player = self.engine.by_id.get(pid)
            if player is not None and player.team == team:
                self._enqueue(pid, msg)

    # ------------------------------------------------ per-turn clock & AI movers
    def _schedule_turn_driven(self):
        """(Re)arm the turn clock / AI mover when whose-turn-it-is changes.

        Synchronous and side-effect-free unless the turn signature changed, so it
        is safe to call after every broadcast (joins, chats, etc. do not reset a
        player's clock).  A monotonically increasing generation makes any
        previously-scheduled task that wakes late recognise it is stale."""
        cur = self.engine.current()
        playing = self.engine.phase == "playing"
        # Connectedness is part of the signature so a connect/disconnect of the
        # CURRENT seat re-evaluates the clock: we must not time out an offline
        # player, and we must give a fresh clock to one who reconnects.
        sig = (self.engine.phase, cur.id if cur else None, bool(cur and cur.connected))
        # An AI seat must always have a live mover -- even when the turn loops
        # back to the same AI (e.g. every other seat is offline), where the
        # signature is unchanged.  An AI task that is itself broadcasting counts
        # as "about to finish", so it still needs a replacement.
        ai_needs_driver = (
            playing and cur is not None and cur.is_ai
            and (self._ai_task is None or self._ai_task.done()
                 or self._ai_task is asyncio.current_task())
        )
        # The same applies to the per-turn clock for a HUMAN seat that retains
        # the turn under loop-back (their only opponents are offline): without
        # this, --turn-limit would stop force-passing after the first timeout.
        # Only fires when there is no live timer (or the live one is this very
        # task, finishing), so a healthy in-flight clock is never reset.
        timer_needs_driver = (
            playing and cur is not None and not cur.is_ai
            and self.turn_limit > 0 and cur.connected
            and (self._turn_task is None or self._turn_task.done()
                 or self._turn_task is asyncio.current_task())
        )
        if sig == self._last_turn_sig and not ai_needs_driver and not timer_needs_driver:
            return
        self._last_turn_sig = sig
        self._turn_gen += 1
        gen = self._turn_gen
        self._cancel_turn_tasks()
        if not playing or cur is None:
            return
        if cur.is_ai:
            self._ai_task = asyncio.create_task(self._run_ai_turn(gen, cur.id))
        elif self.turn_limit > 0 and cur.connected:
            self._turn_task = asyncio.create_task(self._turn_timeout(gen, cur.id))

    def _cancel_turn_tasks(self):
        current = asyncio.current_task()
        for attr in ("_turn_task", "_ai_task"):
            task = getattr(self, attr)
            if task is not None and task is not current and not task.done():
                task.cancel()
            setattr(self, attr, None)

    async def _turn_timeout(self, gen, pid):
        """Force the current player to pass if their turn clock runs out."""
        try:
            await asyncio.sleep(self.turn_limit)
        except asyncio.CancelledError:
            return
        async with self.lock:
            if gen != self._turn_gen:
                return            # the turn already moved on
            cur = self.engine.current()
            if self.engine.phase != "playing" or cur is None or cur.id != pid:
                return
            try:
                self.engine.passing(pid, timed_out=True)
            except MoveError:
                return
            self.broadcast_state()

    async def _run_ai_turn(self, gen, pid):
        """Let an AI player think (briefly) and then make its move."""
        try:
            lo, hi = self.ai_delay_range
            if hi > 0:
                await asyncio.sleep(self.ai_rng.uniform(lo, hi))
        except asyncio.CancelledError:
            return
        # Snapshot under the lock, compute off the loop, then apply under the lock.
        async with self.lock:
            if gen != self._turn_gen:
                return
            player = self.engine.by_id.get(pid)
            cur = self.engine.current()
            if (self.engine.phase != "playing" or cur is None or cur.id != pid
                    or player is None or not player.is_ai):
                return
            cells = self._board_cells()
            rack = list(player.rack)
            level = player.ai_level
            dictionary = self.engine.dictionary
        loop = asyncio.get_running_loop()
        try:
            move = await loop.run_in_executor(
                self._compute_pool, wordsmith.choose_ai_move, cells, rack,
                dictionary, level, self.ai_rng)
        except asyncio.CancelledError:
            return
        except Exception:
            move = None
        async with self.lock:
            if gen != self._turn_gen:
                return
            cur = self.engine.current()
            if self.engine.phase != "playing" or cur is None or cur.id != pid:
                return
            try:
                if move is not None:
                    self.engine.play(pid, move.row, move.col, move.direction, move.word)
                else:
                    self._ai_fallback(pid)
            except MoveError:
                try:
                    self.engine.passing(pid)
                except MoveError:
                    return
            self.broadcast_state()

    def _ai_fallback(self, pid):
        """No legal play: swap a few tiles to refresh the rack, else pass."""
        player = self.engine.by_id.get(pid)
        if player is None:
            return
        if len(self.engine.bag) >= RACK_SIZE and player.rack:
            swap_n = min(3, len(player.rack))
            try:
                self.engine.swap(pid, list(player.rack[:swap_n]))
                return
            except MoveError:
                pass
        self.engine.passing(pid)

    # ------------------------------------------------------------- coaching
    def _schedule_coach(self, pid, cells, rack, played_word, score, bingo):
        asyncio.create_task(
            self._coach_task(pid, cells, rack, played_word, score, bingo))

    async def _coach_task(self, pid, cells, rack, played_word, score, bingo):
        """Privately show the player the best plays they could have made and a
        light-hearted comment.  Computed off the loop; sent only to that player
        (it is derived from their rack, so it must never reach an opponent)."""
        loop = asyncio.get_running_loop()
        try:
            report = await loop.run_in_executor(
                self._compute_pool, wordsmith.coach_report, cells, rack,
                self.engine.dictionary, played_word, score, bingo)
        except asyncio.CancelledError:
            return
        except Exception:
            return
        if not report:
            return
        async with self.lock:
            if pid in self.conns and pid not in self.spectators:
                self._enqueue(pid, {"type": P.COACH, **report})

    async def _writer(self, pid, ws, queue, cipher):
        """Drain one connection's outbound queue, sealing each frame with this
        connection's cipher.  Closing the ws on failure is what prunes a dead
        client: it ends the reader's ``async for`` below."""
        try:
            while True:
                data = await queue.get()
                if data is None:        # shutdown sentinel
                    return
                try:
                    await asyncio.wait_for(ws.send(cipher.encrypt(data)), timeout=SEND_TIMEOUT)
                except Exception:
                    return
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    # --------------------------------------------------------------- handler
    async def handler(self, ws, path=None):
        # Secure handshake: hand the client a fresh random salt (public, like a
        # TLS handshake) and derive this connection's AES-256-GCM session key
        # from the shared room key.  Key derivation runs off the event loop so a
        # joining client never stalls the game.
        salt = crypto.new_salt()
        try:
            await asyncio.wait_for(ws.send(crypto.make_hello(salt)), timeout=SEND_TIMEOUT)
            loop = asyncio.get_running_loop()
            cipher = await loop.run_in_executor(None, crypto.cipher_for, self.passphrase, salt)
        except Exception:
            return

        pid = None
        spectator = False
        my_ws = ws               # this handler's own socket (generation guard)
        my_queue = None          # this handler's own outbound queue
        my_writer = None         # this handler's own writer task
        try:
            async for raw in ws:
                try:
                    data = cipher.decrypt(raw)
                except crypto.DecryptError:
                    # Wrong room key, tampering, or a plaintext probe: there is no
                    # secure channel, so drop the connection rather than reply.
                    break
                try:
                    msg = P.loads(data)
                except Exception:
                    if pid is not None:
                        self._enqueue(pid, {"type": P.ERROR, "message": "Malformed message."})
                    continue
                if not isinstance(msg, dict):
                    if pid is not None:
                        self._enqueue(pid, {"type": P.ERROR, "message": "Malformed message."})
                    continue
                mtype = msg.get("type")

                # Reject anything before a join without touching the game lock.
                if pid is None and mtype != P.JOIN:
                    await self._direct(ws, {"type": P.ERROR,
                                            "message": "Send a 'join' message first."}, cipher)
                    continue

                # Spectators may watch, chat and check words, but never act on
                # the game (play, team up, or manage AI players).
                if spectator and mtype in (P.START, P.PLAY, P.PASS, P.SWAP,
                                           P.SHUFFLE, P.SETTEAM, P.ADDAI, P.REMOVEAI):
                    self._enqueue(pid, {"type": P.ERROR,
                                        "message": "You are spectating; you can chat but not play."})
                    continue

                # join_error is sent AFTER the lock: a failed join has no queue
                # yet, so it must go directly to the socket (off the game lock).
                join_error = None
                async with self.lock:
                    do_broadcast = False
                    try:
                        if mtype == P.JOIN:
                            if pid is not None:
                                continue
                            name = str(msg.get("name", "")).strip()[:20]
                            if msg.get("spectator"):
                                spectator = True
                                name = name or "Spectator"
                                self._next_spec -= 1
                                pid = self._next_spec
                                self.conns[pid] = ws
                                my_queue = asyncio.Queue(maxsize=QUEUE_LIMIT)
                                my_writer = asyncio.create_task(self._writer(pid, ws, my_queue, cipher))
                                self.queues[pid] = my_queue
                                self.writers[pid] = my_writer
                                self.spectators[pid] = name
                                self._enqueue(pid, {"type": P.WELCOME, "id": pid,
                                                    "name": name, "spectator": True})
                                do_broadcast = True
                            else:
                                token = msg.get("token")
                                token = str(token) if token is not None else None
                                player = self.engine.add_player(name, token=token)
                                pid = player.id
                                # Reconnect over a still-live but dead old link:
                                # retire the stale socket so two connections do
                                # not both claim the seat.
                                old_ws = self.conns.get(pid)
                                if old_ws is not None and old_ws is not ws:
                                    old_q = self.queues.get(pid)
                                    old_w = self.writers.get(pid)
                                    if old_q is not None:
                                        try:
                                            old_q.put_nowait(None)
                                        except asyncio.QueueFull:
                                            pass
                                    if old_w is not None:
                                        old_w.cancel()
                                self.conns[pid] = ws
                                my_queue = asyncio.Queue(maxsize=QUEUE_LIMIT)
                                my_writer = asyncio.create_task(self._writer(pid, ws, my_queue, cipher))
                                self.queues[pid] = my_queue
                                self.writers[pid] = my_writer
                                self._enqueue(pid, {"type": P.WELCOME, "id": pid,
                                                    "name": player.name, "token": player.token})
                                do_broadcast = True
                                # Once the first human (the host) has a seat, seat
                                # any AI players requested on the command line.
                                if not self._seeded_ai and self._is_host(pid):
                                    self._seed_ai()
                        elif mtype == P.START:
                            self.engine.start(pid)
                            do_broadcast = True
                        elif mtype == P.PLAY:
                            row = int(msg["row"])
                            col = int(msg["col"])
                            # Snapshot the board + rack BEFORE the move so the
                            # coach can show what was possible from that rack.
                            want_coach = self.coach and self.engine.dictionary.enabled
                            pre_cells = self._board_cells() if want_coach else None
                            pre_rack = list(self.engine.rack_of(pid)) if want_coach else None
                            played_word = str(msg["word"]).upper()
                            info = self.engine.play(pid, row, col, msg["direction"],
                                                    str(msg["word"]))
                            do_broadcast = True
                            if want_coach:
                                self._schedule_coach(pid, pre_cells, pre_rack,
                                                     played_word, info["score"],
                                                     info["bingo"])
                        elif mtype == P.PASS:
                            self.engine.passing(pid)
                            do_broadcast = True
                        elif mtype == P.SWAP:
                            self.engine.swap(pid, list(str(msg.get("tiles", ""))))
                            do_broadcast = True
                        elif mtype == P.SHUFFLE:
                            # Private rearrange: re-send only this player's rack,
                            # no public state change and no turn consumed.
                            self.engine.shuffle_rack(pid)
                            self._enqueue(pid, {"type": P.RACK, "rack": self.engine.rack_of(pid)})
                        elif mtype == P.CHAT:
                            text = str(msg.get("text", "")).strip()[:200]
                            if text:
                                scope = msg.get("scope")
                                if spectator:
                                    sender = self.spectators.get(pid, "spectator")
                                else:
                                    sender = self.engine.by_id[pid].name
                                if scope == "team":
                                    player = None if spectator else self.engine.by_id.get(pid)
                                    if player is not None and player.team:
                                        self.broadcast_team_chat(player.team, sender, text)
                                    else:
                                        self._enqueue(pid, {"type": P.ERROR,
                                            "message": "You are not in a team. Use 'team <name>' first."})
                                else:
                                    self.broadcast_chat(sender, text)
                        elif mtype == P.SETTEAM:
                            self.engine.set_team(pid, str(msg.get("team", "")))
                            do_broadcast = True
                        elif mtype == P.CHECK:
                            raw = msg.get("words")
                            if isinstance(raw, str):
                                raw = raw.split()
                            if not isinstance(raw, (list, tuple)):
                                raw = []
                            results = []
                            seen = set()
                            for w in list(raw)[:20]:
                                word = str(w).strip().upper()
                                # A real Scrabble word is at most 15 tiles; cap
                                # length so a giant string can't burn CPU under
                                # the game lock.
                                if (not word or len(word) > 15
                                        or not word.isalpha() or word in seen):
                                    continue
                                seen.add(word)
                                value = sum(TILE_VALUES.get(ch, 0) for ch in word)
                                results.append([word,
                                                bool(self.engine.dictionary.is_valid(word)),
                                                value])
                            self._enqueue(pid, {"type": P.CHECKED, "results": results,
                                                "enabled": self.engine.dictionary.enabled})
                        elif mtype == P.ADDAI:
                            if not self._is_host(pid):
                                self._enqueue(pid, {"type": P.ERROR,
                                    "message": "Only the host (first player) can add AI players."})
                            else:
                                level = str(msg.get("level", "")).strip().lower()
                                if level not in wordsmith.LEVELS:
                                    self._enqueue(pid, {"type": P.ERROR,
                                        "message": "Pick an AI difficulty: easy, medium, hard, or expert."})
                                else:
                                    name = str(msg.get("name", "")).strip()[:20] or self._ai_name(level)
                                    self.engine.add_ai_player(name, level)
                                    do_broadcast = True
                        elif mtype == P.REMOVEAI:
                            if not self._is_host(pid):
                                self._enqueue(pid, {"type": P.ERROR,
                                    "message": "Only the host can remove AI players."})
                            else:
                                target = self.engine.by_name.get(
                                    str(msg.get("name", "")).strip().lower())
                                if target is None or not target.is_ai:
                                    self._enqueue(pid, {"type": P.ERROR,
                                        "message": "No AI player by that name."})
                                elif self.engine.phase != "lobby":
                                    self._enqueue(pid, {"type": P.ERROR,
                                        "message": "AI players can only be removed in the lobby."})
                                else:
                                    self.engine.remove_player(target.id)
                                    do_broadcast = True
                        else:
                            self._enqueue(pid, {"type": P.ERROR, "message": "Unknown command."})
                    except MoveError as exc:
                        if pid is None:
                            join_error = str(exc)
                        else:
                            self._enqueue(pid, {"type": P.ERROR, "message": str(exc)})
                    except (KeyError, ValueError, TypeError, OverflowError) as exc:
                        text = f"Bad request: {exc}"
                        if pid is None:
                            join_error = text
                        else:
                            self._enqueue(pid, {"type": P.ERROR, "message": text})

                    if do_broadcast:
                        # Still holding the lock: enqueue in move order (no I/O).
                        self.broadcast_state()

                if join_error is not None:
                    await self._direct(ws, {"type": P.ERROR, "message": join_error}, cipher)
        finally:
            # Always stop our own writer; it owns the now-closing socket.
            if my_queue is not None:
                try:
                    my_queue.put_nowait(None)       # ask the writer to stop
                except asyncio.QueueFull:
                    pass
            if my_writer is not None:
                my_writer.cancel()
            # Relinquish the seat ONLY if a newer connection has not already
            # taken it over (reconnect race): otherwise the stale handler would
            # evict the fresh one and wrongly mark the player offline.
            if pid is not None and self.conns.get(pid) is my_ws:
                self.conns.pop(pid, None)
                self.queues.pop(pid, None)
                self.writers.pop(pid, None)
                if spectator:
                    self.spectators.pop(pid, None)
                    async with self.lock:
                        self.broadcast_state()
                else:
                    async with self.lock:
                        if self.engine.phase == "lobby":
                            self.engine.remove_player(pid)
                        else:
                            self.engine.set_connected(pid, False)
                        self.broadcast_state()

    async def _direct(self, ws, obj, cipher):
        try:
            await asyncio.wait_for(ws.send(cipher.encrypt(P.dumps(obj))), timeout=SEND_TIMEOUT)
        except Exception:
            pass

    async def run(self):
        # compression=None: every payload is already AES-GCM ciphertext (high
        # entropy, so deflate would not shrink it) -- disabling it saves CPU and
        # removes any compression-side-channel surface.
        try:
            async with websockets.serve(
                self.handler, self.host, self.port,
                ping_interval=20, ping_timeout=20, compression=None,
            ):
                await asyncio.Future()    # run until cancelled
        finally:
            self._compute_pool.shutdown(wait=False)
