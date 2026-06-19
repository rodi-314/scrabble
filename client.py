"""Async websocket client with a terminal UI.

Two coroutines run concurrently: one receives state updates and redraws the
board, the other reads commands from stdin (via a thread, so it works the same
on Windows and Unix) and sends them to the server.

The input line is a small but real terminal editor: it supports left/right
cursor movement, Home/End, word/line erase, in-place editing, and an Up/Down
command history -- and it redraws without ever erasing text the user is still
typing when another player's message arrives.
"""

import asyncio
import atexit
import os
import sys
import threading

import websockets

import crypto
import protocol as P
from constants import ANSI, BOARD_SIZE, CENTER, PREMIUM, PREMIUM_NAMES, TILE_VALUES
from engine import parse_coord
from util import clear_screen

HELP = """\
Commands:
  play <coord> <across|down> <word>   place a word, e.g.  play H8 across HELLO
                                      (lowercase a letter to play a blank: 'hELLO')
  pass                                forfeit your turn
  swap <letters>                      exchange tiles, e.g.  swap aei   (use '?' for a blank)
  shuffle                             randomly reorder the tiles on your rack
  start                               (host only) begin the game once everyone has joined
  say <message>  /  <message>         chat with the other players
  values                              show the point value of every letter
  clear                               wipe the screen and message history
  help                                show this help
  board                               redraw the screen
  quit                                leave the game
Coordinates are a column letter (A-O) + a row number (1-15); 'across' goes right,
'down' goes down, and you type the full word including any tiles already on the board.
Line editing: arrow keys move the cursor and scroll command history; Home/End jump
to the ends; Ctrl-U clears the line; Ctrl-W erases a word."""

HISTORY_MAX = 200          # commands remembered for Up/Down recall
TURN_ANIM_FRAMES = 6       # frames in the "your turn" attention animation
TURN_ANIM_DELAY = 0.09     # seconds between animation frames


class Client:
    def __init__(self, uri, name, color=True, spectator=False, passphrase=""):
        self.uri = uri
        self.name = name
        self.color = color
        self.spectator = spectator
        self.passphrase = passphrase   # shared room key for encrypting the link
        self._cipher = None            # per-connection AES-256-GCM cipher
        self._secure_ok = False        # a frame decrypted this attempt (key OK)
        self.ws = None
        self.state = None
        self.rack = []
        self.my_id = None
        self.token = None         # reconnect secret issued by the server
        self.messages = []        # chat + info + error lines
        self.quitting = False     # user asked to leave; do not reconnect
        self._connected_once = False  # have we ever opened the socket?
        self.input_queue = None   # asyncio.Queue of stdin lines (shared, persistent)
        self.input_buffer = []    # chars typed but not yet submitted (raw mode)
        self.cursor = 0           # cursor index within input_buffer (0..len)
        self.history = []         # previously submitted command lines
        self.hist_pos = 0         # index into history while browsing (== len: live draft)
        self.hist_draft = ""      # the live line saved when browsing started
        self._screen_lock = threading.Lock()  # serialize stdout: render vs. echo
        self._raw = False         # True when char-at-a-time line editing is active
        self._termios_fd = None   # saved tty + settings for restoration (POSIX)
        self._termios_old = None
        self._last_was_cr = False  # for coalescing a CRLF pair into one newline
        self._anim_task = None    # running "your turn" animation task
        self._anim_frame = None   # current animation frame, or None when idle

    # ------------------------------------------------------------- lifecycle
    async def run(self):
        # One persistent input pump for the whole session, so transient
        # reconnects do not spawn competing readers of the same terminal.
        self.input_queue = asyncio.Queue()
        self._raw = self._enable_raw()
        self._start_input()
        try:
            attempt = 0
            while not self.quitting:
                self._secure_ok = False
                try:
                    async with websockets.connect(
                        self.uri, ping_interval=20, ping_timeout=20, open_timeout=10,
                        compression=None,    # payloads are already encrypted
                    ) as ws:
                        self._connected_once = True
                        self.ws = ws
                        # Secure handshake: receive the salt + KDF params and
                        # derive this session's key from the shared room key.  If
                        # it fails, fall through to the reconnect/exit logic below.
                        if await self._handshake(ws):
                            attempt = 0
                            join = {"type": P.JOIN, "name": self.name}
                            if self.spectator:
                                join["spectator"] = True
                            if self.token:
                                join["token"] = self.token   # reclaim our seat
                            await self._send(join)
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
                    if not self._connected_once:
                        self._print_safe(f"\nCould not connect to {self.uri}: {exc}\n"
                                         "Check the host IP/port and that the server is running on your LAN.")
                        return

                if self.quitting:
                    break
                if not self._secure_ok and not self.token:
                    # We opened a socket but never decrypted a single server
                    # frame: almost always a wrong room key.  Don't spin forever.
                    self._print_safe("\nCould not establish a secure session - "
                                     "is the room key correct?  (pass it with --key)")
                    return
                if not self.token:
                    # No seat to reclaim: a spectator, or a drop before joining.
                    if self.spectator and self._connected_once:
                        self._print_safe("\nDisconnected from the host. Re-run to watch again.")
                    break
                # We had a seat (have a token) but the link dropped: reconnect.
                attempt += 1
                if attempt > 5:
                    self._print_safe("\nLost connection and could not reconnect. Type Enter to exit.")
                    break
                delay = min(attempt, 4)
                self.messages.append(f"[reconnecting in {delay}s... attempt {attempt}/5]")
                self.render()
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    raise
        finally:
            self._cancel_animation()
            self._restore_terminal()

    # --------------------------------------------------------------- input pump
    def _enable_raw(self):
        """Put the terminal into char-at-a-time mode so we own the input line.

        Returns True on success.  Falls back to plain whole-line reads when
        stdin is not an interactive terminal (e.g. piped, as in the tests) or
        the platform primitives are unavailable, so non-interactive use is
        unaffected.  Only in raw mode can we redraw the screen without erasing
        text the user is still typing.
        """
        try:
            if not sys.stdin.isatty():
                return False
        except Exception:
            return False
        if os.name == "nt":
            try:
                import msvcrt  # noqa: F401  (probe availability)
                return True
            except Exception:
                return False
        try:
            import termios
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            new = termios.tcgetattr(fd)
            # Drop canonical mode and echo; keep ISIG so Ctrl-C still interrupts.
            new[3] = new[3] & ~(termios.ICANON | termios.ECHO)
            new[6][termios.VMIN] = 1
            new[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSADRAIN, new)
            self._termios_fd = fd
            self._termios_old = old
            atexit.register(self._restore_terminal)
            return True
        except Exception:
            return False

    def _print_safe(self, text):
        """Write a status line to stdout under the screen lock, so it cannot
        interleave with the daemon stdin thread's keystroke echo in raw mode."""
        with self._screen_lock:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()

    def _restore_terminal(self):
        """Restore the saved terminal mode (POSIX).  Safe to call repeatedly."""
        if self._termios_old is None or self._termios_fd is None:
            return
        try:
            import termios
            termios.tcsetattr(self._termios_fd, termios.TCSADRAIN, self._termios_old)
        except Exception:
            pass
        self._termios_old = None

    def _start_input(self):
        # Read stdin on a daemon thread and hand completed lines to the event
        # loop.  A daemon thread is abandoned cleanly at exit, so a blocked read
        # does not keep the process alive after the game ends.
        loop = asyncio.get_running_loop()
        queue = self.input_queue

        def submit(s):
            try:
                loop.call_soon_threadsafe(queue.put_nowait, s)
            except RuntimeError:
                pass                # event loop already closed; stop quietly

        if self._raw and os.name == "nt":
            target = lambda: self._win_reader(submit)
        elif self._raw:
            target = lambda: self._unix_reader(submit)
        else:
            target = lambda: self._line_reader(submit)
        threading.Thread(target=target, daemon=True).start()

    def _line_reader(self, submit):
        """Fallback for non-interactive stdin: whole-line reads.  An empty
        string means EOF, matching the raw readers and _consume_input."""
        while True:
            line = sys.stdin.readline()
            submit(line)
            if line == "":          # EOF (closed pipe)
                break

    def _unix_reader(self, submit):
        # Read raw bytes straight from the fd (not via sys.stdin's buffered
        # TextIOWrapper) so multi-byte escape sequences -- arrow keys etc. --
        # arrive together and can be decoded whole instead of leaking '[A'
        # into the line.  An incremental decoder handles split UTF-8 sequences.
        import codecs

        fd = sys.stdin.fileno()
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        while True:
            try:
                data = os.read(fd, 1024)
            except OSError:
                submit("")
                break
            if not data:                  # stream EOF (closed pipe)
                submit("")
                break
            pending = self._consume_chars(pending + decoder.decode(data), submit)

    def _consume_chars(self, text, submit):
        """Process complete keystrokes from *text*; return the unconsumed tail
        (a lone trailing ESC or an incomplete escape sequence) for next time."""
        i, n = 0, len(text)
        while i < n:
            ch = text[i]
            # Coalesce CRLF: an LF directly following a CR we already submitted on
            # is the second half of one newline (Windows endings, pasted text, or
            # a CR/LF split across two reads) -- swallow it, do not submit again.
            if ch == "\n" and self._last_was_cr:
                self._last_was_cr = False
                i += 1
                continue
            if ch == "\x1b":              # ESC: decode a whole control sequence
                self._last_was_cr = False
                j = i + 1
                if j >= n:
                    break                 # incomplete -> keep for next read
                if text[j] in ("[", "O"):
                    k = j + 1
                    while k < n and not ("@" <= text[k] <= "~"):
                        k += 1
                    if k >= n:
                        break             # final byte not here yet
                    self._handle_escape(text[k], text[j + 1:k])
                    i = k + 1
                    continue
                i += 1                    # lone ESC -> drop it
                continue
            self._last_was_cr = (ch == "\r")
            if ch in ("\r", "\n"):
                self._submit_line(submit)
            elif ch in ("\x7f", "\x08"):  # DEL / Backspace
                self._erase_char()
            elif ch == "\x01":            # Ctrl-A: jump to start of line
                self._move_home()
            elif ch == "\x05":            # Ctrl-E: jump to end of line
                self._move_end()
            elif ch == "\x04":            # Ctrl-D: EOF only on an empty line
                with self._screen_lock:
                    empty = not self.input_buffer
                if empty:
                    submit("")
            elif ch == "\x15":            # Ctrl-U: clear the line
                self._clear_line()
            elif ch == "\x17":            # Ctrl-W: delete the previous word
                self._delete_word()
            elif ch.isprintable():
                self._insert_char(ch)
            i += 1
        return text[i:]

    def _handle_escape(self, final, params):
        """Dispatch a decoded ``ESC [ ... <final>`` (or ``ESC O <final>``) key."""
        if final == "A":            # Up
            self._history_prev()
        elif final == "B":          # Down
            self._history_next()
        elif final == "C":          # Right
            self._move_right()
        elif final == "D":          # Left
            self._move_left()
        elif final == "H":          # Home
            self._move_home()
        elif final == "F":          # End
            self._move_end()
        elif final == "~":
            if params in ("1", "7"):
                self._move_home()
            elif params in ("4", "8"):
                self._move_end()
            elif params == "3":     # Delete (forward)
                self._delete_forward()
        # Anything else (PgUp/PgDn, F-keys, ...) is intentionally ignored.

    def _win_reader(self, submit):
        import msvcrt
        while True:
            try:
                ch = msvcrt.getwch()
            except KeyboardInterrupt:     # Ctrl-C at the console
                submit("")
                break
            if ch in ("\r", "\n"):
                self._submit_line(submit)
            elif ch == "\x08":            # Backspace
                self._erase_char()
            elif ch in ("\x00", "\xe0"):  # special-key prefix -> arrows/Home/End/Del
                try:
                    code = msvcrt.getwch()
                except Exception:
                    code = ""
                if code == "H":
                    self._history_prev()
                elif code == "P":
                    self._history_next()
                elif code == "K":
                    self._move_left()
                elif code == "M":
                    self._move_right()
                elif code == "G":
                    self._move_home()
                elif code == "O":
                    self._move_end()
                elif code == "S":
                    self._delete_forward()
            elif ch in ("\x03", "\x1a"):  # Ctrl-C / Ctrl-Z -> leave
                submit("")
                break
            elif ch == "\x01":
                self._move_home()
            elif ch == "\x05":
                self._move_end()
            elif ch == "\x15":
                self._clear_line()
            elif ch == "\x17":
                self._delete_word()
            elif ch.isprintable():
                self._insert_char(ch)

    # --------------------------------------------------------- line editing
    # Each primitive holds the screen lock while it touches both the buffer and
    # stdout, so it never interleaves with a redraw on the event loop.  Cursor
    # motion uses backspaces ("\b"), which work on every terminal including
    # legacy consoles without ANSI support.
    def _submit_line(self, submit):
        with self._screen_lock:
            line = "".join(self.input_buffer)
            self.input_buffer = []
            self.cursor = 0
            if line.strip() and (not self.history or self.history[-1] != line):
                self.history.append(line)
                if len(self.history) > HISTORY_MAX:
                    self.history.pop(0)
            self.hist_pos = len(self.history)
            self.hist_draft = ""
            sys.stdout.write("\n")
            sys.stdout.flush()
        # Trailing newline keeps an empty line ("\n") distinct from EOF ("").
        submit(line + "\n")

    def _insert_char(self, ch):
        with self._screen_lock:
            i = self.cursor
            self.input_buffer.insert(i, ch)
            self.cursor = i + 1
            tail = "".join(self.input_buffer[i:])      # the new char + old tail
            sys.stdout.write(tail)
            back = len(tail) - 1                       # park the cursor after ch
            if back:
                sys.stdout.write("\b" * back)
            sys.stdout.flush()

    def _erase_char(self):
        """Backspace: delete the character to the left of the cursor."""
        with self._screen_lock:
            if self.cursor == 0:
                return
            i = self.cursor - 1
            del self.input_buffer[i]
            self.cursor = i
            tail = "".join(self.input_buffer[i:])
            sys.stdout.write("\b" + tail + " ")        # back, rewrite, erase last
            sys.stdout.write("\b" * (len(tail) + 1))
            sys.stdout.flush()

    def _delete_forward(self):
        """Delete key: remove the character under the cursor."""
        with self._screen_lock:
            if self.cursor >= len(self.input_buffer):
                return
            del self.input_buffer[self.cursor]
            tail = "".join(self.input_buffer[self.cursor:])
            sys.stdout.write(tail + " ")
            sys.stdout.write("\b" * (len(tail) + 1))
            sys.stdout.flush()

    def _move_left(self):
        with self._screen_lock:
            if self.cursor > 0:
                self.cursor -= 1
                sys.stdout.write("\b")
                sys.stdout.flush()

    def _move_right(self):
        with self._screen_lock:
            if self.cursor < len(self.input_buffer):
                sys.stdout.write(self.input_buffer[self.cursor])
                self.cursor += 1
                sys.stdout.flush()

    def _move_home(self):
        with self._screen_lock:
            if self.cursor > 0:
                sys.stdout.write("\b" * self.cursor)
                self.cursor = 0
                sys.stdout.flush()

    def _move_end(self):
        with self._screen_lock:
            if self.cursor < len(self.input_buffer):
                sys.stdout.write("".join(self.input_buffer[self.cursor:]))
                self.cursor = len(self.input_buffer)
                sys.stdout.flush()

    def _clear_line(self):
        with self._screen_lock:
            if self.input_buffer:
                self._set_line_locked([])

    def _delete_word(self):
        with self._screen_lock:
            if self.cursor == 0:
                return
            i = self.cursor
            j = i
            while j > 0 and self.input_buffer[j - 1] == " ":
                j -= 1
            while j > 0 and self.input_buffer[j - 1] != " ":
                j -= 1
            removed = i - j
            if not removed:
                return
            del self.input_buffer[j:i]
            self.cursor = j
            tail = "".join(self.input_buffer[j:])
            sys.stdout.write("\b" * (i - j) + tail + " " * removed)
            sys.stdout.write("\b" * (len(tail) + removed))
            sys.stdout.flush()

    def _set_line_locked(self, new_chars):
        """Replace the whole visible input line with *new_chars* (lock held)."""
        # Move to the end of what is currently shown, then erase it all.
        if self.cursor < len(self.input_buffer):
            sys.stdout.write("".join(self.input_buffer[self.cursor:]))
        if self.input_buffer:
            sys.stdout.write("\b \b" * len(self.input_buffer))
        self.input_buffer = list(new_chars)
        self.cursor = len(self.input_buffer)
        if self.input_buffer:
            sys.stdout.write("".join(self.input_buffer))
        sys.stdout.flush()

    def _history_prev(self):
        """Up arrow: recall an older command."""
        with self._screen_lock:
            if not self.history or self.hist_pos == 0:
                return
            if self.hist_pos == len(self.history):
                self.hist_draft = "".join(self.input_buffer)   # save the live line
            self.hist_pos -= 1
            self._set_line_locked(self.history[self.hist_pos])

    def _history_next(self):
        """Down arrow: move toward newer commands, then back to the live draft."""
        with self._screen_lock:
            if self.hist_pos >= len(self.history):
                return
            self.hist_pos += 1
            if self.hist_pos == len(self.history):
                self._set_line_locked(self.hist_draft)
            else:
                self._set_line_locked(self.history[self.hist_pos])

    # ----------------------------------------------------------- networking
    async def _handshake(self, ws):
        """Receive the server's cleartext salt + KDF params and derive this
        session's AES-256-GCM key from the shared room key.  Returns True on
        success.  The key is derived off the event loop (scrypt is CPU-bound)."""
        try:
            hello = await asyncio.wait_for(ws.recv(), timeout=10)
            salt, n, r, p = crypto.parse_hello(hello)
            loop = asyncio.get_running_loop()
            self._cipher = await loop.run_in_executor(
                None, crypto.cipher_for, self.passphrase, salt, n, r, p)
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def _receive(self):
        try:
            async for raw in self.ws:
                try:
                    data = self._cipher.decrypt(raw)
                except crypto.DecryptError:
                    # Cannot authenticate the server's frame: wrong room key.
                    self._secure_ok = False
                    try:
                        await self.ws.close()
                    except Exception:
                        pass
                    return
                self._secure_ok = True
                try:
                    msg = P.loads(data)
                except Exception:
                    continue
                mtype = msg.get("type")
                if mtype == P.WELCOME:
                    self.my_id = msg["id"]
                    self.token = msg.get("token") or self.token
                    if msg.get("spectator"):
                        self.messages.append("You are spectating the game.")
                    else:
                        self.messages.append(f"Joined as {msg.get('name', self.name)}.")
                elif mtype == P.STATE:
                    self._apply_state(msg["state"])
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
                self._print_safe("\nLeft the game.")

    def _apply_state(self, new_state):
        """Store the latest state and fire the turn animation on the transition
        into our turn (so it is unmistakable that it is time to move)."""
        prev_turn = self.state.get("turn") if isinstance(self.state, dict) else None
        self.state = new_state
        now_turn = new_state.get("turn")
        if (new_state.get("phase") == "playing" and not self.spectator
                and now_turn == self.my_id and prev_turn != self.my_id):
            self._start_turn_animation()
        elif now_turn != self.my_id:
            self._cancel_animation()

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
        if self.spectator and cmd in ("play", "p", "pass", "swap", "exchange",
                                      "start", "shuffle", "sh"):
            self._note("You are spectating - you can chat, but you cannot play.")
            return
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
            elif cmd in ("shuffle", "sh"):
                await self._send({"type": P.SHUFFLE})
            elif cmd == "start":
                await self._send({"type": P.START})
            elif cmd in ("help", "?", "h"):
                self._note(HELP)
            elif cmd in ("clear", "cls"):
                self.messages.clear()
                clear_screen(self.color, scrollback=True)
                self.render()
            elif cmd == "board":
                self.render()
            elif cmd in ("quit", "exit"):
                self.quitting = True
                await self.ws.close()
            elif cmd in ("values", "points", "v"):
                self._note(self._values_table())
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
        await self.ws.send(self._cipher.encrypt(P.dumps(obj)))

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

    # --------------------------------------------------------- turn animation
    def _start_turn_animation(self):
        """Begin (or restart) the attention animation for our turn."""
        if not self._raw:
            return            # only meaningful on an interactive terminal
        self._cancel_animation()
        try:
            with self._screen_lock:
                sys.stdout.write("\a")     # audible bell: it is your move
                sys.stdout.flush()
        except Exception:
            pass
        self._anim_task = asyncio.create_task(self._turn_animation())

    def _cancel_animation(self):
        self._anim_frame = None
        task = self._anim_task
        if task is not None and not task.done():
            task.cancel()
        self._anim_task = None

    async def _turn_animation(self):
        try:
            for frame in range(TURN_ANIM_FRAMES):
                self._anim_frame = frame
                self.render()
                await asyncio.sleep(TURN_ANIM_DELAY)
        except asyncio.CancelledError:
            pass
        finally:
            self._anim_frame = None
            try:
                self.render()             # settle on the steady banner
            except Exception:
                pass

    # ---------------------------------------------------------------- render
    def _c(self, key, text):
        if not self.color:
            return text
        return ANSI[key] + text + ANSI["reset"]

    def render(self):
        # Clear the screen BEFORE taking the lock.  On the no-color path
        # clear_screen() shells out to clear/cls (a blocking subprocess); holding
        # the stdout lock -- which the input-echo thread also needs -- across that
        # would stall keystroke echo on every redraw.  Only the in-process redraw
        # writes are serialized by the lock; the worst a clear/echo overlap can do
        # is a momentary stray glyph that the very next write paints over, and the
        # typed buffer is always reprinted intact.
        clear_screen(self.color)
        with self._screen_lock:
            self._render_locked()

    def _render_locked(self):
        out = [self._c("bold", "=== LAN SCRABBLE ===")]
        st = self.state
        if st is None:
            out.append("Connecting to the game...")
            self._flush(out)
            return

        out.append(self._render_board(st["board"], st.get("last_move")))
        out.append("")

        turn = st.get("turn")
        out.append("Players:")
        for p in st["players"]:
            mark = self._c("turn", ">") if p["id"] == turn else " "
            you = " (you)" if p["id"] == self.my_id else ""
            offline = "" if p["connected"] else " [offline]"
            out.append(f" {mark} {p['name']}{you}: {p['score']} pts, {p['tiles']} tiles{offline}")
        out.append(f"Tiles left in bag: {st['bag']}")
        specs = st.get("spectators") or []
        if specs:
            shown = ", ".join(specs[:6]) + (" ..." if len(specs) > 6 else "")
            out.append(f"Spectators: {len(specs)} ({shown})")
        out.append("")

        if self.spectator:
            out.append(self._c("dim", "You are spectating - watching the game live."))
        else:
            out.extend(self._render_rack_lines())
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
            if not self.spectator and st.get("first_player") == self.my_id:
                out.append(self._c("turn", "You are the host. Type 'start' once everyone has joined."))
            else:
                out.append("Waiting for the host to start the game...")
        elif phase == "playing":
            if not self.spectator and turn == self.my_id:
                out.append(self._your_turn_banner())
            else:
                who = next((p["name"] for p in st["players"] if p["id"] == turn), "?")
                out.append(f"Waiting for {who} to move...")
        elif phase == "over":
            out.extend(self._over_lines(st))
        self._flush(out)

    def _render_board(self, board, last_move=None):
        last_set = {tuple(cell) for cell in (last_move or [])}
        header = "    " + "".join(f"{ch:^3}" for ch in "ABCDEFGHIJKLMNO")
        rows = [header]
        for r in range(BOARD_SIZE):
            cells = []
            for c in range(BOARD_SIZE):
                cell = board[r][c]
                if cell:
                    letter, is_blank = cell[0], cell[1]
                    glyph = letter.lower() if is_blank else letter
                    is_last = (r, c) in last_set
                    if is_last and not self.color:
                        # No colour: bracket the just-played tiles so the last
                        # move is still obvious (same 3-column width as ' X ').
                        cells.append(f"[{glyph}]")
                    elif is_last:
                        cells.append(self._c("last_blank_tile" if is_blank else "last_tile",
                                             f" {glyph} "))
                    else:
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

    def _render_rack_lines(self):
        """Two aligned lines: the tiles, and each tile's point value beneath it."""
        if not self.rack:
            return ["Your tiles:  (empty)"]
        letters, points = [], []
        for t in self.rack:
            glyph = "_" if t == "?" else t
            val = 0 if t == "?" else TILE_VALUES[t]
            letters.append(self._c("rack", f"{glyph:^3}"))
            points.append(f"{val:^3}")
        return [
            "Your tiles: " + " ".join(letters),
            "   points:  " + " ".join(points),
        ]

    def _your_turn_banner(self):
        """The 'your turn' prompt -- a flashing/growing banner while the turn
        animation is running, then a steady banner with the command hints."""
        hint = "play H8 across HELLO  |  pass  |  swap aei  |  shuffle  |  help"
        frame = self._anim_frame
        if frame is None:
            return self._c("turn", "  >>> YOUR TURN <<<  ") + "   " + hint
        reach = frame + 1
        label = ">" * reach + " YOUR TURN " + "<" * reach
        if self.color:
            key = "turn" if frame % 2 == 0 else "warn"
            return self._c(key, "  " + label + "  ")
        # No colour: shift the banner so the motion itself signals the turn.
        return " " * (TURN_ANIM_FRAMES - frame) + label

    @staticmethod
    def _values_table():
        """A reference table of every letter's point value, grouped by value."""
        buckets = {}
        for letter, val in TILE_VALUES.items():
            if letter == "?":
                continue
            buckets.setdefault(val, []).append(letter)
        lines = ["Letter values:"]
        for val in sorted(buckets):
            lines.append(f"  {val:>2} pts : " + " ".join(sorted(buckets[val])))
        lines.append("   0 pts : blank ( ? )")
        return "\n".join(lines)

    def _over_lines(self, st):
        """The end-of-game scoreboard, showing how the final scores were reached."""
        lines = [self._c("bold", "================  GAME OVER  ================")]
        summary = st.get("end_summary")
        if not summary:
            lines.append("Winner(s): " + ", ".join(st.get("winners", [])))
            lines.append("Type 'quit' to leave.")
            return lines
        lines.append(summary.get("reason", ""))
        lines.append("")
        lines.append("   Player            Final   Adj  Leftover tiles")
        winners = summary.get("winners", [])
        rows = sorted(summary.get("rows", []), key=lambda r: r["final"], reverse=True)
        for r in rows:
            mark = self._c("turn", " * ") if r["name"] in winners else "   "
            adj = r.get("adjustment", 0)
            adjs = f"{adj:+d}" if adj else "0"
            left = r.get("leftover") or "-"
            lines.append(f"{mark}{r['name'][:16]:<16}{r['final']:>6}  {adjs:>4}  {left}")
        lines.append("")
        if len(winners) == 1:
            lines.append(self._c("bold", f"Winner: {winners[0]} - congratulations!"))
        elif winners:
            lines.append(self._c("bold", "It's a tie between " + ", ".join(winners) + "!"))
        lines.append("Type 'quit' to leave (you can still chat).")
        return lines

    def _flush(self, lines):
        sys.stdout.write("\n".join(lines))
        # Reprint the prompt with whatever the user has typed so far, so a redraw
        # triggered by another player's message never erases their input, and put
        # the cursor back where they left it (it may be mid-line).
        sys.stdout.write("\n> " + "".join(self.input_buffer))
        back = len(self.input_buffer) - self.cursor
        if back > 0:
            sys.stdout.write("\b" * back)
        sys.stdout.flush()
