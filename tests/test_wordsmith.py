"""Tests for the move generator / scorer / AI selection (wordsmith.py).

The engine is used as the oracle:

  * **soundness** -- every move the generator returns is accepted by Engine.play
    with the *exact* same score; and
  * **completeness** -- every play the engine would accept (found by an
    independent brute force over the word list) is also found by the generator.

Together these pin the generator to the engine's rules and scoring.

Run directly with:  python tests/test_wordsmith.py
"""

import os
import random
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wordsmith
from board import Tile
from dictionary import Dictionary
from engine import Engine, MoveError

WORDS = ["HELLO", "HELLOS", "HA", "AH", "AS", "AX", "OX", "HE", "EH", "HEN", "AN", "NA",
         "AT", "TA", "CAT", "CATS", "QI", "ZA", "OH", "HO", "ON", "NO", "OS",
         "SO", "ES", "ET", "TE", "EL", "LA", "AL", "HALE", "HALES", "HEAL",
         "HEALS", "LEACH", "ELAN", "LANE", "SANE", "OLE", "LO", "SH", "ASH",
         "CASH", "SHE", "HES", "AE", "EA"]


def make_dict():
    return Dictionary(words=WORDS)


def snapshot(board):
    return [[(board.grid[r][c].letter, board.grid[r][c].is_blank)
             if board.grid[r][c] else None
             for c in range(15)] for r in range(15)]


def cells_from(placements):
    """Build a board snapshot from ``(r, c, letter, is_blank)`` placements."""
    cells = [[None] * 15 for _ in range(15)]
    for (r, c, letter, is_blank) in placements:
        cells[r][c] = (letter, is_blank)
    return cells


def make_state(cells, rack, dictionary):
    """A playable engine whose board == ``cells`` and whose mover holds ``rack``."""
    e = Engine(dictionary, rng=random.Random(0))
    a = e.add_player("A")
    e.add_player("B")
    e.phase = "playing"
    e.turn = 0
    for r in range(15):
        for c in range(15):
            cell = cells[r][c]
            if cell is not None:
                e.board.set(r, c, Tile(cell[0], cell[1]))
    a.rack = list(rack)
    return e, a


def move_key(move):
    """Identify the *physical* play by the new tiles it places."""
    dr, dc = (0, 1) if move.direction == "across" else (1, 0)
    placed = []
    for i, ch in enumerate(move.word):
        r, c = move.row + dr * i, move.col + dc * i
        # a lowercase letter is a freshly placed blank; an uppercase letter on an
        # empty square is a freshly placed real tile; anything on a filled square
        # is a through tile (not newly placed).
        placed.append((r, c, ch.upper(), ch.islower()))
    return placed   # filtered to new tiles by the caller using the board


def assert_sound(cells, rack, dictionary, label):
    """Every generated move is accepted by the engine with a matching score."""
    moves = wordsmith.generate_moves(cells, rack, dictionary)
    for mv in moves:
        e, a = make_state(cells, rack, dictionary)
        try:
            info = e.play(a.id, mv.row, mv.col, mv.direction, mv.word)
        except MoveError as exc:
            raise AssertionError(
                f"[{label}] generator produced an ILLEGAL move {mv}: {exc}")
        assert info["score"] == mv.score, (
            f"[{label}] score mismatch for {mv}: engine={info['score']} "
            f"gen={mv.score}")
    return moves


def brute_force_keys(cells, rack, dictionary):
    """All physically-distinct legal plays, found independently of the generator.

    Restricted to racks WITHOUT blanks so each (word, position) maps to exactly
    one physical play (no blank/real ambiguity); blanks are exercised by the
    soundness checks instead."""
    assert "?" not in rack, "brute force oracle assumes a blank-free rack"
    counts = Counter(rack)
    keys = set()
    for direction in ("across", "down"):
        dr, dc = (0, 1) if direction == "across" else (1, 0)
        for word in dictionary.words:
            if len(word) < 2:
                continue
            for r in range(15):
                for c in range(15):
                    need = Counter()
                    new = []
                    ok = True
                    for i, ch in enumerate(word):
                        rr, cc = r + dr * i, c + dc * i
                        if not (0 <= rr < 15 and 0 <= cc < 15):
                            ok = False
                            break
                        b = cells[rr][cc]
                        if b is not None:
                            if b[0] != ch:
                                ok = False
                                break
                        else:
                            need[ch] += 1
                            new.append((rr, cc, ch, False))
                    if not ok or not new:
                        continue
                    br, bc = r - dr, c - dc
                    if 0 <= br < 15 and 0 <= bc < 15 and cells[br][bc] is not None:
                        continue
                    er, ec = r + dr * len(word), c + dc * len(word)
                    if 0 <= er < 15 and 0 <= ec < 15 and cells[er][ec] is not None:
                        continue
                    if any(need[l] > counts.get(l, 0) for l in need):
                        continue
                    e, a = make_state(cells, rack, dictionary)
                    try:
                        e.play(a.id, r, c, direction, word)
                    except MoveError:
                        continue
                    keys.add(tuple(sorted(new)))
    return keys


def gen_keys(cells, rack, dictionary):
    keys = set()
    for mv in wordsmith.generate_moves(cells, rack, dictionary):
        dr, dc = (0, 1) if mv.direction == "across" else (1, 0)
        new = []
        for i, ch in enumerate(mv.word):
            r, c = mv.row + dr * i, mv.col + dc * i
            if cells[r][c] is None:
                new.append((r, c, ch.upper(), ch.islower()))
        keys.add(tuple(sorted(new)))
    return keys


def assert_complete(cells, rack, dictionary, label):
    expected = brute_force_keys(cells, rack, dictionary)
    got = gen_keys(cells, rack, dictionary)
    missing = expected - got
    assert not missing, f"[{label}] generator MISSED {len(missing)} legal plays: {list(missing)[:5]}"


# ----------------------------------------------------------------- tests
def test_empty_board_first_move_sound_and_complete():
    d = make_dict()
    cells = [[None] * 15 for _ in range(15)]
    assert_sound(cells, list("HELOACX"), d, "empty/sound")
    assert_complete(cells, list("HELOACX"), d, "empty/complete")
    # Every first move must cover the centre and score >= the centre double.
    moves = wordsmith.generate_moves(cells, list("HELOACX"), d)
    assert moves, "expected first-move plays on the empty board"
    for mv in moves:
        dr, dc = (0, 1) if mv.direction == "across" else (1, 0)
        covers = any((mv.row + dr * i, mv.col + dc * i) == (7, 7)
                     for i in range(len(mv.word)))
        assert covers, f"first move {mv} does not cross the centre"


def test_extending_existing_word_sound_and_complete():
    d = make_dict()
    cells = cells_from([(7, 7, "H", False), (7, 8, "E", False),
                        (7, 9, "L", False), (7, 10, "L", False),
                        (7, 11, "O", False)])
    rack = list("SACHEAT")
    moves = assert_sound(cells, rack, d, "hello/sound")
    assert_complete(cells, rack, d, "hello/complete")
    # HELLOS (add S after O) must be found and scored correctly.
    words = {mv.word.upper() for mv in moves}
    assert "HELLOS" in words, words


def test_blank_play_is_sound():
    d = make_dict()
    cells = cells_from([(7, 7, "H", False), (7, 8, "E", False),
                        (7, 9, "N", False)])
    # A rack with a blank: the generator must still only return legal, correctly
    # scored plays (a blank scores 0, even on a premium square).
    moves = assert_sound(cells, list("?ASCXOT"), d, "blank/sound")
    assert any("?" not in mv.word and mv.word != mv.word.upper() for mv in moves), \
        "expected at least one play that uses the blank (a lowercase letter)"


def test_random_boards_sound_and_complete():
    d = make_dict()
    rng = random.Random(2024)
    pool = "AEIOULNRSTHCXOZQ"
    for seed in range(12):
        # Build a plausible board by letting the expert AI play a few moves.
        e = Engine(d, rng=random.Random(seed))
        a = e.add_player("A")
        b = e.add_player("B")
        e.phase = "playing"
        e.turn = 0
        movers = [a, b]
        for t in range(5):
            mover = movers[t % 2]
            e.turn = t % 2
            mover.rack = [rng.choice(pool) for _ in range(7)]
            cells = snapshot(e.board)
            mv = wordsmith.choose_ai_move(cells, mover.rack, d, "expert", rng)
            if mv is None:
                continue
            try:
                e.play(mover.id, mv.row, mv.col, mv.direction, mv.word)
            except MoveError:
                pass
        cells = snapshot(e.board)
        test_rack = [rng.choice(pool) for _ in range(7) if rng.random() > 0]
        test_rack = [c for c in test_rack if c != "?"]   # blank-free for completeness
        assert_sound(cells, test_rack, d, f"rand{seed}/sound")
        assert_complete(cells, test_rack, d, f"rand{seed}/complete")


def test_choose_ai_move_levels_are_legal_and_ordered():
    d = make_dict()
    cells = cells_from([(7, 7, "H", False), (7, 8, "E", False),
                        (7, 9, "L", False), (7, 10, "L", False),
                        (7, 11, "O", False)])
    rack = list("SACHEAT")
    rng = random.Random(5)
    all_moves = wordsmith.generate_moves(cells, rack, d)
    assert len(all_moves) >= 4, "need a few moves to compare difficulties"
    best = max(m.score for m in all_moves)
    # expert always plays the maximum-scoring move.
    for _ in range(10):
        mv = wordsmith.choose_ai_move(cells, rack, d, "expert", rng)
        assert mv.score == best, (mv.score, best)
        # ...and it is a legal move.
        e, a = make_state(cells, rack, d)
        e.play(a.id, mv.row, mv.col, mv.direction, mv.word)
    # every difficulty returns a legal move.
    for level in wordsmith.LEVELS:
        mv = wordsmith.choose_ai_move(cells, rack, d, level, rng)
        e, a = make_state(cells, rack, d)
        e.play(a.id, mv.row, mv.col, mv.direction, mv.word)


def test_choose_ai_move_none_when_no_play():
    d = make_dict()
    cells = [[None] * 15 for _ in range(15)]
    # A rack that cannot make any 2-letter word in our dictionary through centre.
    mv = wordsmith.choose_ai_move(cells, list("BBBBBBB"), d, "expert")
    assert mv is None


def test_coach_comment_tiers():
    # Bingo trumps everything.
    assert "BINGO" in wordsmith.coach_comment(30, 80, True)
    # Optimal: actual >= best.
    assert wordsmith.coach_comment(50, 50, False) in wordsmith._COMMENTS["optimal"]
    # A poor play (< 25% of the best) gets the harshest tier.
    harsh = wordsmith.coach_comment(5, 100, False)
    assert harsh in wordsmith._COMMENTS["bad"]
    # No alternatives -> a gentle note, never a division error.
    assert "wasn't much" in wordsmith.coach_comment(10, 0, False)


def test_coach_report_structure():
    d = make_dict()
    cells = cells_from([(7, 7, "H", False), (7, 8, "E", False),
                        (7, 9, "L", False), (7, 10, "L", False),
                        (7, 11, "O", False)])
    rack = list("SACHEAT")
    report = wordsmith.coach_report(cells, rack, d, "HE", 5, False, k=5)
    assert report is not None
    assert report["played"] == {"word": "HE", "score": 5}
    assert 1 <= len(report["best"]) <= 5
    # best is sorted high-to-low and each row is [word, coord, dir, score].
    scores = [row[3] for row in report["best"]]
    assert scores == sorted(scores, reverse=True), scores
    for row in report["best"]:
        assert len(row) == 4 and isinstance(row[3], int)
    assert isinstance(report["comment"], str) and report["comment"]


def test_disabled_dictionary_yields_no_moves():
    d = Dictionary(enabled=False)
    cells = [[None] * 15 for _ in range(15)]
    assert wordsmith.generate_moves(cells, list("HELLOAX"), d) == []
    assert wordsmith.coach_report(cells, list("HELLOAX"), d, "HELLO", 18, False) is None


def run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in tests:
        fn()
        passed += 1
        print(f"  ok  {fn.__name__}")
    print(f"\n{passed}/{len(tests)} wordsmith tests passed.")


if __name__ == "__main__":
    run_all()
