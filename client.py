"""Async websocket client with a terminal UI.

Two coroutines run concurrently: one receives state updates and redraws the
board, the other reads commands from stdin (via a thread, so it works the same
on Windows and Unix) and sends them to the server.
"""

import asyncio
import sys
import threading

import websockets

import protocol as P
from constants import ANSI, BOARD_SIZE, CENTER, PREMIUM, PREMIUM_NAMES
from engine import parse_coord
from util import clear_screen

HELP = """\
Commands:
  play <coord> <across|down> <word>   place a word, e.g.  play H8 across HELLO
                                      (lowercase a letter to play a blank: 'hELLO')
  pass                                forfeit your turn
  swap <letters>                      exchange tiles, e.g.  swap aei   (use '?' for a blank)
  start                               (host only) begin the game once everyone has joined
  say <message>  /  <message>         chat with the other players
  help                                show this help
  board                               redraw the screen
  quit                                leave the game
Coordinates are a column letter (A-O) + a row number (1-15); 'across' goes right,
'down' goes down, and you type the full word including any tiles already on the board."""


class Client:
    def __init__(self, uri, name, color=True):
        self.uri = uri
        self.name = name
        self.color = color
        self.ws = None
        self.state = None
        self.rack = []
        self.my_id = None
        self.token = None         # reconnect secret issued by the server
        self.messages = []        # chat + info + error lines
        self.quitting = False     # user asked to leave; do not reconnect
        self.input_queue = None   # asyncio.Queue of stdin lines (shared, persistent)

    # ------------------------------------------------------------- lifecycle
    async def run(self):
        # One persistent stdin pump for the whole session, so transient
        # reconnects do not spawn competing readers of the same terminal.
        self.input_queue = asyncio.Queue()
        self._start_stdin_pump()

        attempt = 0
        while not self.quitting:
            connected = False
            try:
                async with websockets.connect(
                    self.uri, ping_interval=20, ping_timeout=20, open_timeout=10
                ) as ws:
                    connected = True
                    attempt = 0
                    self.ws = ws
                    join = {"type": P.JOIN, "name": self.name}
                    if self.token:
                        join["token"] = self.token       # reclaim our seat
                    await ws.send(P.dumps(join))
                    receiver = asyncio.create_task(self._receive())
                    reader = asyncio.create_task(self._consume_input())
                    _, pending = await asyncio.wait(
                        {receiver, reader}, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in pending:
                        task.cancel()
            except asyncio.CancelledError:
                raise
            except (OSError, websockets.exceptions.WebSocketException) as exc:
                if not self.token:
                    print(f"\nCould not connect to {self.uri}: {exc}")
                    print("Check the host IP/port and that the server is running on your LAN.")
                    return

            if self.quitting:
                break
            # We had a seat (have a token) but the link dropped: try to reconnect.
            if not self.token:
                break
            attempt += 1
            if attempt > 5:
                print("\nLost connection and could not reconnect. Type Enter to exit.")
                break
            delay = min(attempt, 4)
            self.messages.append(f"[reconnecting in {delay}s... attempt {attempt}/5]")
            self.render()
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise

    def _start_stdin_pump(self):
        # Read stdin on a daemon thread and hand lines to the event loop.  A
        # daemon thread is abandoned cleanly at exit, so a blocked readline does
        # not keep the process alive after the game ends (Windows and Unix).
        loop = asyncio.get_running_loop()
        queue = self.input_queue

        def pump():
            while True:
                line = sys.stdin.readline()
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, line)
                except RuntimeError:
                    break               # event loop already closed; stop quietly
                if line == "":          # EOF (Ctrl-D / closed pipe)
                    break

        threading.Thread(target=pump, daemon=True).start()

    async def _receive(self):
        try:
            async for raw in self.ws:
                try:
                    msg = P.loads(raw)
                except Exception:
                    continue
                mtype = msg.get("type")
                if mtype == P.WELCOME:
                    self.my_id = msg["id"]
                    self.token = msg.get("token") or self.token
                    self.messages.append(f"Joined as {msg.get('name', self.name)}.")
                elif mtype == P.STATE:
                    self.state = msg["state"]
                elif mtype == P.RACK:
                    self.rack = msg["rack"]
                elif mtype == P.ERROR:
                    self.messages.append("[!] " + msg.get("message", ""))
                elif mtype == P.INFO:
                    self.messages.append(msg.get("message", ""))
                elif mtype == P.CHATMSG:
                    self.messages.append(f"[{msg.get('name', '?')}] {msg.get('text', '')}")
                self.render()
        except websockets.exceptions.ConnectionClosed:
            if self.quitting:
                print("\nLeft the game.")

    async def _consume_input(self):
        self.render()
        while True:
            line = await self.input_queue.get()
            if line == "":              # EOF
                self.quitting = True
                if self.ws is not None:
                    try:
                        await self.ws.close()
                    except Exception:
                        pass
                return
            await self._handle(line.strip())

    # --------------------------------------------------------------- commands
    async def _handle(self, line):
        if not line:
            self.render()
            return
        parts = line.split()
        cmd = parts[0].lower()
        try:
            if cmd in ("play", "p"):
                if len(parts) < 4:
                    self._note("Usage: play <coord> <across|down> <word>")
                    return
                row, col = parse_coord(parts[1])
                direction = self._direction(parts[2])
                await self._send({"type": P.PLAY, "row": row, "col": col,
                                  "direction": direction, "word": parts[3]})
            elif cmd == "pass":
                await self._send({"type": P.PASS})
            elif cmd in ("swap", "exchange"):
                if len(parts) < 2:
                    self._note("Usage: swap <letters>, e.g. swap aei")
                    return
                await self._send({"type": P.SWAP, "tiles": parts[1]})
            elif cmd == "start":
                await self._send({"type": P.START})
            elif cmd in ("help", "?", "h"):
                self._note(HELP)
            elif cmd == "board":
                self.render()
            elif cmd in ("quit", "exit"):
                self.quitting = True
                await self.ws.close()
            elif cmd in ("say", "chat", "msg"):
                if len(parts) >= 2:
                    await self._send({"type": P.CHAT, "text": line.split(None, 1)[1]})
            else:
                # Anything unrecognised is treated as chat.
                await self._send({"type": P.CHAT, "text": line})
        except ValueError as exc:
            self._note("[!] " + str(exc))
        except websockets.exceptions.ConnectionClosed:
            self._note("[!] Disconnected from server.")

    async def _send(self, obj):
        await self.ws.send(P.dumps(obj))

    def _note(self, text):
        self.messages.append(text)
        self.render()

    @staticmethod
    def _direction(token):
        token = token.lower()
        if token in ("across", "a", "right", "r", "h", "horizontal", "-"):
            return "across"
        if token in ("down", "d", "v", "vertical", "|"):
            return "down"
        raise ValueError("Direction must be 'across' or 'down'.")

    # ---------------------------------------------------------------- render
    def _c(self, key, text):
        if not self.color:
            return text
        return ANSI[key] + text + ANSI["reset"]

    def render(self):
        clear_screen(self.color)
        out = [self._c("bold", "=== LAN SCRABBLE ===")]
        st = self.state
        if st is None:
            out.append("Connecting to the game...")
            self._flush(out)
            return

        out.append(self._render_board(st["board"]))
        out.append("")

        turn = st.get("turn")
        out.append("Players:")
        for p in st["players"]:
            mark = self._c("turn", ">") if p["id"] == turn else " "
            you = " (you)" if p["id"] == self.my_id else ""
            offline = "" if p["connected"] else " [offline]"
            out.append(f" {mark} {p['name']}{you}: {p['score']} pts, {p['tiles']} tiles{offline}")
        out.append(f"Tiles left in bag: {st['bag']}")
        out.append("")
        out.append("Your rack: " + self._render_rack())
        out.append("")

        if st.get("log"):
            out.append("Recent moves:")
            for line in st["log"][-6:]:
                out.append("  " + line)
        if self.messages:
            out.append("")
            for line in self.messages[-5:]:
                out.append(line)
        out.append("")

        phase = st["phase"]
        if phase == "lobby":
            if st.get("first_player") == self.my_id:
                out.append(self._c("turn", "You are the host. Type 'start' once everyone has joined."))
            else:
                out.append("Waiting for the host to start the game...")
        elif phase == "playing":
            if turn == self.my_id:
                out.append(self._c("turn", "YOUR TURN") +
                           "  ->  play H8 across HELLO  |  pass  |  swap aei  |  help")
            else:
                who = next((p["name"] for p in st["players"] if p["id"] == turn), "?")
                out.append(f"Waiting for {who} to move...")
        elif phase == "over":
            out.append(self._c("bold", "GAME OVER. Winner(s): " + ", ".join(st.get("winners", []))))
            out.append("Type 'quit' to leave.")
        self._flush(out)

    def _render_board(self, board):
        header = "    " + "".join(f"{ch:^3}" for ch in "ABCDEFGHIJKLMNO")
        rows = [header]
        for r in range(BOARD_SIZE):
            cells = []
            for c in range(BOARD_SIZE):
                cell = board[r][c]
                if cell:
                    letter, is_blank = cell[0], cell[1]
                    glyph = letter.lower() if is_blank else letter
                    cells.append(self._c("blank_tile" if is_blank else "tile", f" {glyph} "))
                elif (r, c) == CENTER:
                    cells.append(self._c("star", " * "))
                else:
                    prem = PREMIUM[r][c]
                    if prem == ".":
                        cells.append(" . ")
                    else:
                        key = {"T": "TW", "D": "DW", "t": "TL", "d": "DL"}[prem]
                        cells.append(self._c(key, f"{PREMIUM_NAMES[prem]:^3}"))
            rows.append(f"{r + 1:>2} " + "".join(cells) + f" {r + 1}")
        rows.append(header)
        return "\n".join(rows)

    def _render_rack(self):
        if not self.rack:
            return "(empty)"
        return " ".join(self._c("rack", f" {'_' if t == '?' else t} ") for t in self.rack)

    def _flush(self, lines):
        sys.stdout.write("\n".join(lines))
        sys.stdout.write("\n> ")
        sys.stdout.flush()
