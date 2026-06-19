"""Async websocket server that hosts one Scrabble game for a LAN.

All game mutations run through :class:`engine.Engine` while holding a single lock,
so moves are processed one at a time.  Outbound messages are never sent while
the lock is held: each connection has its own outbound queue drained by a
dedicated writer task.  Broadcasting is therefore a fast, synchronous enqueue,
and a slow or dead client can never stall the rest of the game.
"""

import asyncio

import websockets

import crypto
import protocol as P
from engine import Engine, MoveError

QUEUE_LIMIT = 128        # per-connection outbound backlog before we shed frames
SEND_TIMEOUT = 5         # seconds before a stalled send drops its connection


class GameServer:
    def __init__(self, dictionary, host="0.0.0.0", port=8765, passphrase=""):
        self.engine = Engine(dictionary)
        self.host = host
        self.port = port
        self.passphrase = passphrase    # shared room key; every frame is encrypted
        self.conns = {}          # conn id -> websocket (players and spectators)
        self.queues = {}         # conn id -> asyncio.Queue of serialized frames
        self.writers = {}        # conn id -> writer task
        self.spectators = {}     # conn id (negative) -> spectator display name
        self._next_spec = 0      # spectator id generator (counts down: -1, -2, ...)
        self.lock = asyncio.Lock()

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
        state = {"type": P.STATE, "state": public}
        for pid in list(self.conns):
            self._enqueue(pid, state)
            if pid not in self.spectators:
                self._enqueue(pid, {"type": P.RACK, "rack": self.engine.rack_of(pid)})

    def broadcast_chat(self, name, text):
        msg = {"type": P.CHATMSG, "name": name, "text": text}
        for pid in list(self.conns):
            self._enqueue(pid, msg)

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

                # Spectators may watch and chat, but never act on the game.
                if spectator and mtype in (P.START, P.PLAY, P.PASS, P.SWAP, P.SHUFFLE):
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
                        elif mtype == P.START:
                            self.engine.start(pid)
                            do_broadcast = True
                        elif mtype == P.PLAY:
                            row = int(msg["row"])
                            col = int(msg["col"])
                            self.engine.play(pid, row, col, msg["direction"], str(msg["word"]))
                            do_broadcast = True
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
                                if spectator:
                                    sender = self.spectators.get(pid, "spectator")
                                else:
                                    sender = self.engine.by_id[pid].name
                                self.broadcast_chat(sender, text)
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
        async with websockets.serve(
            self.handler, self.host, self.port,
            ping_interval=20, ping_timeout=20, compression=None,
        ):
            await asyncio.Future()        # run until cancelled
