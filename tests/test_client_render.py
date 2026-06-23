"""Renderer and line-editor tests for the client UI.  No terminal or network.

They cover the UI behaviour:
  * the rack shows the point value of every tile, and a values table exists;
  * a redraw (e.g. another player's chat) does NOT erase in-progress input;
  * the game-over scoreboard reflects the official end-game scoring;
  * the line editor: cursor movement, mid-line edits, and Up/Down history;
  * spectator mode hides the rack and shows the spectator banner;
  * the "your turn" animation produces distinct banner frames.

Run directly with:  python tests/test_client_render.py
"""

import asyncio
import io
import os
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from client import Client
from constants import ANSI


def _discard(_):
    """A submit() sink that throws keystrokes away (for editor tests)."""


def make_client(spectator=False):
    # color=True keeps clear_screen on the ANSI path so all output is captured
    # in-process (the non-ANSI path shells out to clear/cls).
    c = Client("ws://x", "Alice", color=True, spectator=spectator)
    c.my_id = -1 if spectator else 1
    return c


def capture_render(c):
    buf = io.StringIO()
    with redirect_stdout(buf):
        c.render()
    return buf.getvalue()


def feed(c, text, submitted=None):
    """Drive the line editor with *text*, swallowing its echo."""
    sink = submitted if submitted is not None else _discard
    with redirect_stdout(io.StringIO()):
        return c._consume_chars(text, sink if callable(sink) else sink.append)


def base_state(phase="playing"):
    board = [[None] * 15 for _ in range(15)]
    return {
        "phase": phase,
        "board": board,
        "players": [
            {"id": 1, "name": "Alice", "score": 0, "tiles": 7, "connected": True},
            {"id": 2, "name": "Bob", "score": 0, "tiles": 7, "connected": True},
        ],
        "turn": 1,
        "bag": 80,
        "log": [],
        "winners": [],
        "end_summary": None,
        "first_player": 1,
        "spectators": [],
        "last_move": [],
    }


# ----------------------------------------------------------------- rendering
def test_rack_shows_point_values():
    c = make_client()
    c.state = base_state()
    c.rack = ["Q", "A", "?"]
    out = capture_render(c)
    pts_lines = [ln for ln in out.splitlines() if "points:" in ln]
    assert pts_lines, "expected a 'points:' line under the rack"
    line = pts_lines[0]
    assert "10" in line and "1" in line and "0" in line, line


def test_values_table_lists_every_value():
    table = Client._values_table()
    assert "Letter values:" in table
    for token in ("10 pts", "Q", "Z", "blank"):
        assert token in table, token


def test_last_move_highlight_color():
    c = make_client()                         # color=True
    board = [[None] * 15 for _ in range(15)]
    board[7][7] = ["H", False]                # the just-played tile
    board[7][8] = ["A", False]                # an older tile
    out = c._render_board(board, [[7, 7]])
    assert ANSI["last_tile"] in out           # the highlight colour is applied
    assert ANSI["tile"] in out                # the older tile keeps the normal colour


def test_last_move_highlight_no_color_uses_brackets():
    c = Client("ws://x", "Alice", color=False)
    board = [[None] * 15 for _ in range(15)]
    board[7][7] = ["H", False]
    out = c._render_board(board, [[7, 7]])
    assert "[H]" in out                       # brackets mark the last move without colour
    # An unhighlighted tile is rendered plainly (no brackets).
    out2 = c._render_board(board, [])
    assert "[H]" not in out2 and " H " in out2


def test_render_preserves_typed_input():
    c = make_client()
    c.state = base_state()
    c.rack = ["A"]
    # The user is half-way through typing a chat message (cursor at the end).
    c.input_buffer = list("say hello wor")
    c.cursor = len(c.input_buffer)
    out = capture_render(c)
    # The redraw must end with the prompt plus the in-progress text intact.
    assert out.rstrip().endswith("> say hello wor"), repr(out[-40:])


def test_render_repositions_cursor_when_mid_line():
    c = make_client()
    c.state = base_state()
    c.rack = ["A"]
    c.input_buffer = list("hello")
    c.cursor = 2                      # cursor sits after "he"
    out = capture_render(c)
    # The full text is reprinted, then the cursor is walked back 3 columns.
    assert out.endswith("> hello" + "\b" * 3), repr(out[-20:])


def test_empty_input_buffer_has_clean_prompt():
    c = make_client()
    c.state = base_state()
    c.rack = ["A"]
    out = capture_render(c)
    assert out.endswith("> "), repr(out[-20:])


# ------------------------------------------------------------- line editor
def test_input_parser_handles_keys_and_escapes():
    c = make_client()
    submitted = []
    sink = io.StringIO()
    with redirect_stdout(sink):                # swallow the editor's echo
        # Type "hi", then an arrow-up; with empty history the arrow is a no-op,
        # not inserted as '[A'.
        tail = c._consume_chars("hi\x1b[A", submitted.append)
        assert "".join(c.input_buffer) == "hi", c.input_buffer
        assert tail == "", repr(tail)
        c._consume_chars("\x7f", submitted.append)     # backspace removes 'i'
        assert "".join(c.input_buffer) == "h"
        c._consume_chars("ello\r", submitted.append)   # Enter submits the line
        assert submitted == ["hello\n"], submitted
        assert c.input_buffer == [] and c.cursor == 0
        # A trailing, incomplete escape is held over for the next read.
        held = c._consume_chars("ab\x1b", submitted.append)
        assert held == "\x1b" and "".join(c.input_buffer) == "ab"


def test_cursor_move_and_midline_insert():
    c = make_client()
    feed(c, "hello")                  # buffer "hello", cursor 5
    assert c.cursor == 5
    feed(c, "\x1b[D\x1b[D")           # Left, Left -> cursor 3
    assert c.cursor == 3
    feed(c, "X")                      # insert at 3 -> "helXlo", cursor 4
    assert "".join(c.input_buffer) == "helXlo" and c.cursor == 4
    feed(c, "\x1b[H")                 # Home
    assert c.cursor == 0
    feed(c, "\x1b[F")                 # End
    assert c.cursor == len(c.input_buffer) == 6


def test_backspace_and_delete_midline():
    c = make_client()
    feed(c, "abcdef")                 # cursor 6
    feed(c, "\x1b[D\x1b[D")           # cursor 4
    feed(c, "\x7f")                   # backspace deletes 'd' -> "abcef", cursor 3
    assert "".join(c.input_buffer) == "abcef" and c.cursor == 3
    feed(c, "\x1b[3~")                # Delete removes 'e' -> "abcf", cursor 3
    assert "".join(c.input_buffer) == "abcf" and c.cursor == 3
    feed(c, "\x17")                   # Ctrl-W deletes the word before the cursor
    assert "".join(c.input_buffer) == "f" and c.cursor == 0   # "abc" removed, "f" kept


def test_command_history_recall():
    c = make_client()
    submitted = []
    feed(c, "first\r", submitted)
    feed(c, "second\r", submitted)
    assert submitted == ["first\n", "second\n"]
    assert c.history == ["first", "second"]
    feed(c, "\x1b[A")                 # Up -> most recent
    assert "".join(c.input_buffer) == "second"
    feed(c, "\x1b[A")                 # Up -> older
    assert "".join(c.input_buffer) == "first"
    feed(c, "\x1b[B")                 # Down -> newer
    assert "".join(c.input_buffer) == "second"
    feed(c, "\x1b[B")                 # Down -> back to the (empty) live draft
    assert "".join(c.input_buffer) == ""


def test_history_preserves_live_draft():
    c = make_client()
    feed(c, "old\r")                  # history ["old"]
    feed(c, "typing")                 # an unsubmitted draft
    feed(c, "\x1b[A")                 # Up recalls "old" and saves the draft
    assert "".join(c.input_buffer) == "old"
    feed(c, "\x1b[B")                 # Down restores the draft verbatim
    assert "".join(c.input_buffer) == "typing"


def test_history_skips_consecutive_duplicates():
    c = make_client()
    feed(c, "same\r")
    feed(c, "same\r")
    assert c.history == ["same"]       # only one entry recorded


def test_crlf_submits_once():
    # A CRLF pair (pasted / Windows line ending) must submit exactly one line,
    # not the line plus a spurious empty one.
    c = make_client()
    submitted = []
    feed(c, "hi\r\n", submitted)
    assert submitted == ["hi\n"], submitted
    assert c.input_buffer == []


def test_crlf_split_across_reads():
    # CR ends one read, LF starts the next: still one submission.
    c = make_client()
    submitted = []
    feed(c, "yo\r", submitted)         # read 1 ends on CR
    feed(c, "\nmore", submitted)       # read 2 starts on LF
    assert submitted == ["yo\n"], submitted
    assert "".join(c.input_buffer) == "more"


# ----------------------------------------------------------- spectate + anim
def test_spectator_hides_rack_and_shows_banner():
    c = make_client(spectator=True)
    st = base_state()
    st["spectators"] = ["Alice"]
    c.state = st
    c.rack = ["Q", "A"]               # even if a rack were set, it is not shown
    out = capture_render(c)
    assert "spectating" in out.lower()
    assert "Spectators: 1" in out
    assert "Your tiles" not in out
    assert "YOUR TURN" not in out     # a spectator never gets a turn


def test_spectator_cannot_play():
    c = make_client(spectator=True)
    c.state = base_state()
    with redirect_stdout(io.StringIO()):
        asyncio.run(c._handle("play H8 across HELLO"))
    assert any("spectating" in m.lower() for m in c.messages), c.messages


def test_turn_banner_animation_frames():
    c = make_client()
    c.state = base_state()
    c._anim_frame = None
    steady = c._your_turn_banner()
    c._anim_frame = 0
    f0 = c._your_turn_banner()
    c._anim_frame = 1
    f1 = c._your_turn_banner()
    assert "YOUR TURN" in steady and "YOUR TURN" in f0 and "YOUR TURN" in f1
    assert f0 != steady and f0 != f1   # the banner visibly changes per frame


def test_clear_command_wipes_messages():
    c = make_client()
    c.state = base_state()
    c.messages = ["chatter", "more chatter"]
    with redirect_stdout(io.StringIO()):
        asyncio.run(c._handle("clear"))
    assert c.messages == []


def test_game_over_scoreboard():
    c = make_client()
    st = base_state(phase="over")
    st["players"][0]["score"] = 30
    st["players"][1]["score"] = -20
    st["winners"] = ["Alice"]
    st["end_summary"] = {
        "reason": "Alice used all of their tiles and the bag is empty, so the game ends.",
        "winners": ["Alice"],
        "rows": [
            {"id": 1, "name": "Alice", "base": 10, "leftover": "",
             "leftover_value": 0, "adjustment": 20, "final": 30},
            {"id": 2, "name": "Bob", "base": 0, "leftover": "QZ",
             "leftover_value": 20, "adjustment": -20, "final": -20},
        ],
    }
    c.state = st
    out = capture_render(c)
    assert "GAME OVER" in out
    assert "Winner: Alice" in out
    assert "QZ" in out                       # Bob's revealed leftover tiles
    assert "+20" in out and "-20" in out     # the scoring adjustments


class _FakeLoop:
    """A stand-in event loop exposing just .time() for the turn-clock tests."""
    def __init__(self, now):
        self._now = now

    def time(self):
        return self._now


def test_render_shows_team_and_ai_tags():
    c = make_client()
    st = base_state()
    st["players"][0]["team"] = "RED"
    st["players"][1] = {"id": 3, "name": "Robo", "score": 12, "tiles": 7,
                        "connected": True, "team": "BLUE", "is_ai": True,
                        "ai_level": "expert"}
    st["teamed"] = True
    c.state = st
    c.rack = ["A"]
    out = capture_render(c)
    assert "{RED}" in out and "{BLUE}" in out      # team tags by each name
    assert "[AI:expert]" in out                    # the AI difficulty tag
    assert "Teams:" in out                         # the team totals block


def test_render_shows_turn_countdown():
    c = make_client()
    st = base_state()
    st["turn_limit"] = 30
    c.state = st
    c.rack = ["A"]
    c._loop = _FakeLoop(100.0)
    c._turn_deadline = 125.0                        # 25 seconds remaining
    out = capture_render(c)
    assert "[25s left]" in out, repr(out[-200:])


def test_show_coach_renders_best_plays_and_comment():
    c = make_client()
    c.state = base_state()
    c._show_coach({
        "played": {"word": "HELLO", "score": 18},
        "best": [["THOLES", "H3", "down", 26], ["HELLOS", "H8", "across", 20]],
        "comment": "Good play!",
    })
    out = capture_render(c)
    assert "-- Coach --" in out
    assert "THOLES" in out and "26" in out
    assert "Good play!" in out


def test_show_checked_renders_validity():
    c = make_client()
    c.state = base_state()
    c._show_checked({"results": [["HELLO", True, 8], ["ZZZZZ", False, 50]],
                     "enabled": True})
    out = capture_render(c)
    assert "HELLO" in out and "valid" in out
    assert "ZZZZZ" in out and "INVALID" in out


def test_game_over_team_standings():
    c = make_client()
    st = base_state(phase="over")
    st["players"][0]["score"], st["players"][0]["team"] = 40, "RED"
    st["players"][1]["score"], st["players"][1]["team"] = 30, "BLUE"
    st["winners"] = ["Alice"]
    st["end_summary"] = {
        "reason": "Two scoreless rounds in a row - the game is passed out.",
        "winners": ["Alice"], "teamed": True,
        "teams": [{"team": "RED", "members": ["Alice"], "total": 40, "winner": True},
                  {"team": "BLUE", "members": ["Bob"], "total": 30, "winner": False}],
        "rows": [{"id": 1, "name": "Alice", "team": "RED", "base": 40,
                  "leftover": "", "leftover_value": 0, "adjustment": 0, "final": 40},
                 {"id": 2, "name": "Bob", "team": "BLUE", "base": 30,
                  "leftover": "", "leftover_value": 0, "adjustment": 0, "final": 30}],
    }
    c.state = st
    out = capture_render(c)
    assert "GAME OVER" in out
    assert "Team standings:" in out
    assert "Winning team: RED" in out


def run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in tests:
        fn()
        passed += 1
        print(f"  ok  {fn.__name__}")
    print(f"\n{passed}/{len(tests)} client render tests passed.")


if __name__ == "__main__":
    run_all()
