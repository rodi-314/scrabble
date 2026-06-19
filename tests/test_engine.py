"""Unit tests for the Scrabble engine (rules and scoring).

Run directly with:  python tests/test_engine.py
No third-party test runner is required.
"""

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from constants import TILE_DISTRIBUTION, TILE_VALUES, RACK_SIZE
from dictionary import Dictionary
from engine import Engine, MoveError, parse_coord, coord_name

WORDS = ["HELLO", "HA", "AH", "FRIENDS", "AS", "AX", "OX", "HE", "EH", "HEN",
         "AN", "NA", "AT", "TA", "CAT", "CATS", "QI", "ZA"]


def make_engine():
    return Engine(Dictionary(words=WORDS), rng=random.Random(1234))


def two_player_game():
    e = make_engine()
    a = e.add_player("Alice")
    b = e.add_player("Bob")
    e.start(a.id)
    return e, a, b


def expect_error(fn, contains=None):
    try:
        fn()
    except MoveError as exc:
        if contains:
            assert contains.lower() in str(exc).lower(), f"wrong error: {exc}"
        return
    raise AssertionError("expected MoveError, none raised")


def test_constants():
    assert sum(TILE_DISTRIBUTION.values()) == 100
    e = make_engine()
    assert len(e.bag) == 100
    assert coord_name(7, 7) == "H8"
    assert parse_coord("H8") == (7, 7)
    assert parse_coord("A1") == (0, 0)
    assert parse_coord("O15") == (14, 14)


def test_first_move_must_cover_center():
    e, a, b = two_player_game()
    a.rack = list("HELLOAX")
    # Off-center first move is rejected.
    expect_error(lambda: e.play(a.id, 0, 0, "across", "HA"), contains="center")
    # Across through the center scores 18: (H4 E1 L1 L1 O1->O on DL=2) = 9, x2 word.
    info = e.play(a.id, 7, 7, "across", "HELLO")
    assert info["score"] == 18, info
    assert info["bingo"] is False
    assert a.score == 18


def test_blank_scores_zero():
    e, a, b = two_player_game()
    a.rack = ["?", "E", "L", "L", "O", "A", "X"]
    # Play HELLO with a blank H (lowercase 'h').  H contributes 0.
    # (0 + E1 + L1 + L1 + O2) = 5, doubled by the center = 10.
    info = e.play(a.id, 7, 7, "across", "hELLO")
    assert info["score"] == 10, info
    assert e.board.get(7, 7).is_blank is True


def test_bingo_bonus():
    e, a, b = two_player_game()
    a.rack = list("FRIENDS")
    # FRIENDS across, starting at E8 (row 7, col 4) so it crosses the center.
    # Letters sum to 11, doubled by the center = 22, +50 bingo = 72.
    info = e.play(a.id, 7, 4, "across", "FRIENDS")
    assert info["bingo"] is True
    assert info["score"] == 72, info


def test_cross_word_and_through_tile():
    e, a, b = two_player_game()
    a.rack = list("HELLOAX")
    e.play(a.id, 7, 7, "across", "HELLO")     # H at H8 (7,7)
    # Bob plays "HA" downward, reusing the existing H as the top letter.
    b.rack = list("AEIOUST")
    info = e.play(b.id, 7, 7, "down", "HA")
    # H is pre-existing (4, no premium); A is new at (8,7) plain = 1.  Total 5.
    assert info["score"] == 5, info
    assert e.board.get(8, 7).letter == "A"


def test_must_connect():
    e, a, b = two_player_game()
    a.rack = list("HELLOAX")
    e.play(a.id, 7, 7, "across", "HELLO")
    b.rack = list("CATSXYZ")
    # A floating word that touches nothing is rejected.
    expect_error(lambda: e.play(b.id, 0, 0, "across", "CAT"), contains="connect")


def test_invalid_word_rejected():
    e, a, b = two_player_game()
    a.rack = list("HELLOAX")
    e.play(a.id, 7, 7, "across", "HELLO")
    b.rack = list("AXOXEIU")
    # "XO" is not in our test dictionary.
    expect_error(lambda: e.play(b.id, 6, 11, "down", "XO"), contains="valid word")


def test_missing_tile_rejected():
    e, a, b = two_player_game()
    a.rack = list("HELLOAX")
    expect_error(lambda: e.play(a.id, 7, 7, "across", "FRIENDS"),
                 contains="don't have")


def test_not_your_turn():
    e, a, b = two_player_game()
    b.rack = list("CATSXYZ")
    expect_error(lambda: e.play(b.id, 7, 7, "across", "CAT"), contains="your turn")


def test_pass_advances_and_ends_game():
    e, a, b = two_player_game()
    assert e.current().id == a.id
    e.passing(a.id)
    assert e.current().id == b.id
    e.passing(b.id)
    e.passing(a.id)
    # 2 players -> game ends after 2*2 = 4 scoreless turns.
    e.passing(b.id)
    assert e.phase == "over"


def test_swap_keeps_rack_size():
    e, a, b = two_player_game()
    before = sorted(a.rack)
    e.swap(a.id, ["?"] if "?" in a.rack else list(a.rack[:2]))
    assert len(a.rack) == RACK_SIZE
    assert len(e.bag) == 100 - 2 * RACK_SIZE   # both players were dealt 7
    assert e.current().id == b.id


def test_start_requires_two_players():
    e = make_engine()
    a = e.add_player("Solo")
    expect_error(lambda: e.start(a.id), contains="2 players")


def test_only_first_player_starts():
    e = make_engine()
    a = e.add_player("Alice")
    b = e.add_player("Bob")
    expect_error(lambda: e.start(b.id), contains="first player")


def test_reconnect_requires_token():
    e, a, b = two_player_game()
    token = a.token
    assert token, "a token should be issued on join"
    e.set_connected(a.id, False)
    assert a.connected is False
    # Wrong or missing token cannot steal the disconnected seat.
    expect_error(lambda: e.add_player("Alice", token="deadbeefdeadbeef"), contains="token")
    expect_error(lambda: e.add_player("Alice"), contains="token")
    # A connected name is still simply "taken".
    expect_error(lambda: e.add_player("Bob"), contains="taken")
    # The correct token reconnects to the same seat (same id, rack, score).
    p = e.add_player("Alice", token=token)
    assert p is a and a.connected is True


def test_full_word_required():
    e, a, b = two_player_game()
    a.rack = list("HELLOAX")
    e.play(a.id, 7, 7, "across", "HELLO")
    # Trying to play "A" right after the O without including O is rejected.
    b.rack = list("AEIOUST")
    expect_error(lambda: e.play(b.id, 7, 12, "across", "A"), contains="before")


def test_end_summary_passed_out():
    e, a, b = two_player_game()
    a.rack = list("AE")       # leftover value 1 + 1 = 2
    b.rack = list("QZ")       # leftover value 10 + 10 = 20
    # Two full scoreless rounds (4 passes) ends a 2-player game.
    e.passing(a.id); e.passing(b.id); e.passing(a.id); e.passing(b.id)
    assert e.phase == "over"
    s = e.end_summary
    assert s is not None and "passed out" in s["reason"].lower()
    rows = {r["name"]: r for r in s["rows"]}
    assert rows["Alice"]["adjustment"] == -2 and rows["Alice"]["final"] == -2
    assert rows["Bob"]["adjustment"] == -20 and rows["Bob"]["final"] == -20
    assert rows["Bob"]["leftover"] == "QZ"
    # Nobody went out, so each player only loses their own leftovers; Alice wins.
    assert s["winners"] == ["Alice"]


def test_end_summary_played_out():
    e, a, b = two_player_game()
    e.bag = []                # empty bag so emptying a rack ends the game
    a.rack = list("HA")       # plays out completely
    b.rack = list("QZ")       # leftover value 20
    a_base = a.score
    info = e.play(a.id, 7, 7, "across", "HA")   # HA across the center scores 10
    assert e.phase == "over"
    s = e.end_summary
    rows = {r["name"]: r for r in s["rows"]}
    # Alice emptied her rack: she gains Bob's leftover (20), Bob loses it.
    assert rows["Alice"]["leftover"] == "" and rows["Alice"]["adjustment"] == 20
    assert rows["Alice"]["final"] == a_base + info["score"] + 20
    assert rows["Bob"]["adjustment"] == -20 and rows["Bob"]["final"] == -20
    assert "used all of their tiles" in s["reason"]
    assert s["winners"] == ["Alice"]


def run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in tests:
        fn()
        passed += 1
        print(f"  ok  {fn.__name__}")
    print(f"\n{passed}/{len(tests)} engine tests passed.")


if __name__ == "__main__":
    run_all()
