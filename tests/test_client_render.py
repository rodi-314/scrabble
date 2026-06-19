"""Renderer tests for the client UI.  No terminal or network is required.

They verify the three changes to the UI:
  1. the rack shows the point value of every tile, and a values table exists;
  2. a redraw (e.g. another player's chat arriving) does NOT erase text the
     user is part-way through typing -- the input buffer is reprinted;
  3. the game-over scoreboard reflects the official end-game scoring.

Run directly with:  python tests/test_client_render.py
"""

import io
import os
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from client import Client


def make_client():
    # color=True keeps clear_screen on the ANSI path so all output is captured
    # in-process (the non-ANSI path shells out to clear/cls).
    c = Client("ws://x", "Alice", color=True)
    c.my_id = 1
    return c


def capture_render(c):
    buf = io.StringIO()
    with redirect_stdout(buf):
        c.render()
    return buf.getvalue()


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
    }


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


def test_render_preserves_typed_input():
    c = make_client()
    c.state = base_state()
    c.rack = ["A"]
    # The user is half-way through typing a chat message.
    c.input_buffer = list("say hello wor")
    out = capture_render(c)
    # The redraw must end with the prompt plus the in-progress text intact.
    assert out.rstrip().endswith("> say hello wor"), repr(out[-40:])


def test_empty_input_buffer_has_clean_prompt():
    c = make_client()
    c.state = base_state()
    c.rack = ["A"]
    out = capture_render(c)
    assert out.endswith("> "), repr(out[-20:])


def test_input_parser_handles_keys_and_escapes():
    c = make_client()
    submitted = []
    sink = io.StringIO()
    with redirect_stdout(sink):                # swallow the editor's echo
        # Type "hi", then an arrow-up; the arrow must be eaten, not inserted.
        tail = c._consume_chars("hi\x1b[A", submitted.append)
        assert "".join(c.input_buffer) == "hi", c.input_buffer
        assert tail == "", repr(tail)
        c._consume_chars("\x7f", submitted.append)     # backspace removes 'i'
        assert "".join(c.input_buffer) == "h"
        c._consume_chars("ello\r", submitted.append)   # Enter submits the line
        assert submitted == ["hello\n"], submitted
        assert c.input_buffer == []
        # A trailing, incomplete escape is held over for the next read.
        held = c._consume_chars("ab\x1b", submitted.append)
        assert held == "\x1b" and "".join(c.input_buffer) == "ab"


def test_crlf_submits_once():
    # A CRLF pair (pasted / Windows line ending) must submit exactly one line,
    # not the line plus a spurious empty one.
    c = make_client()
    submitted = []
    with redirect_stdout(io.StringIO()):
        c._consume_chars("hi\r\n", submitted.append)
    assert submitted == ["hi\n"], submitted
    assert c.input_buffer == []


def test_crlf_split_across_reads():
    # CR ends one read, LF starts the next: still one submission.
    c = make_client()
    submitted = []
    with redirect_stdout(io.StringIO()):
        c._consume_chars("yo\r", submitted.append)      # read 1 ends on CR
        c._consume_chars("\nmore", submitted.append)    # read 2 starts on LF
    assert submitted == ["yo\n"], submitted
    assert "".join(c.input_buffer) == "more"


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
