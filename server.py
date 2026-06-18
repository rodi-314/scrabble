"""Async websocket server that hosts one Scrabble game for a LAN.

All game mutations run through :class:`engine.Engine` while holding a single lock,
so moves are processed one at a time.  Outbound messages are never sent while
the lock is held: each connection has its own outbound queue drained by a
dedicated writer task.  Broadcasting is therefore a fast, synchronous enqueue,
and a slow or dead client can never stall the rest of the game.
"""

import asyncio

import websockets

import protocol as P
from engine import Engine, MoveError

QUEUE_LIMIT = 128        # per-connection outbound backlog before we shed frames
SEND_TIMEOUT = 5         # seconds before a stalled send drops its connection


class GameServer:
    def __init__(self, dictionary, host="0.0.0.0", port=8765):
        self.engine = Engine(dictionary)
        self.host = host
        self.port = port
        self.conns = {}          # player id -> websocket
        self.queues = {}         # player id -> asyncio.Queue of serialized frames
        self.writers = {}        # player id -> writer task
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
        """
        state = {"type": P.STATE, "state": self.engine.public_state()}
        for pid in list(self.conns):
            self._enqueue(pid, state)
            self._enqueue(pid, {"type": P.RACK, "rack": self.engine.rack_of(pid)})

    def broadcast_chat(self, name, text):
        msg = {"type": P.CHATMSG, "name": name, "text": text}
        for pid in list(self.conns):
            self._enqueue(pid, msg)

    async def _writer(self, pid, ws, queue):
        """Drain one connection's outbound queue.  Closing the ws on failure is
        what prunes a dead client: it ends the reader's ``async for`` below."""
        try:
            while True:
                data = await queue.get()
                if data is None:        # shutdown sentinel
                    return
                try:
                    await asyncio.wait_for(ws.send(data), timeout=SEND_TIMEOUT)
                except Exception:
                    return
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    # --------------------------------------------------------------- handler
    async def handler(self, ws, path=None):
        pid = None
        try:
            async for raw in ws:
                try:
                    msg = P.loads(raw)
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
                                            "message": "Send a 'join' message first."})
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
                            token = msg.get("token")
                            token = str(token) if token is not None else None
                            player = self.engine.add_player(name, token=token)
                            pid = player.id
                            self.conns[pid] = ws
                            self.queues[pid] = asyncio.Queue(maxsize=QUEUE_LIMIT)
                            self.writers[pid] = asyncio.create_task(
                                self._writer(pid, ws, self.queues[pid])
                            )
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
                        elif mtype == P.CHAT:
                            text = str(msg.get("text", "")).strip()[:200]
                            if text:
                                self.broadcast_chat(self.engine.by_id[pid].name, text)
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
                    await self._direct(ws, {"type": P.ERROR, "message": join_error})
        finally:
            if pid is not None:
                self.conns.pop(pid, None)
                queue = self.queues.pop(pid, None)
                writer = self.writers.pop(pid, None)
                if queue is not None:
                    try:
                        queue.put_nowait(None)      # ask the writer to stop
                    except asyncio.QueueFull:
                        pass
                if writer is not None:
                    writer.cancel()
                async with self.lock:
                    if self.engine.phase == "lobby":
                        self.engine.remove_player(pid)
                    else:
                        self.engine.set_connected(pid, False)
                    self.broadcast_state()

    async def _direct(self, ws, obj):
        try:
            await asyncio.wait_for(ws.send(P.dumps(obj)), timeout=SEND_TIMEOUT)
        except Exception:
            pass

    async def run(self):
        async with websockets.serve(
            self.handler, self.host, self.port, ping_interval=20, ping_timeout=20
        ):
            await asyncio.Future()        # run until cancelled
