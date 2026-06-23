"""Move generation, scoring, AI move selection, and post-move coaching.

This module powers two features that both need to know *what plays are possible*
from a given rack on a given board:

  * **AI players** -- pick a legal move at a chosen difficulty (easy/medium/hard/
    expert); and
  * the **coach** -- after a human plays, show them the top-scoring plays they
    *could* have made from the same rack, plus a light-hearted comment.

The generator is a trie-backed implementation of the classic Appel & Jacobson
"anchor / extend-right" algorithm.  It enumerates every legal play and scores
each one *exactly* as :class:`engine.Engine` would (the tests cross-check this
against the engine, both for soundness -- every move we return is accepted -- and
completeness -- we find every move the engine would accept).

Nothing here touches the network or game state; it operates on a plain board
snapshot (``cells``: a 15x15 grid of ``None`` or ``(LETTER, is_blank)``) and a
``rack`` (a list of single-character tiles, ``'?'`` for a blank), so it is pure
and easy to test.
"""

import random

from constants import (
    BINGO_BONUS,
    BOARD_SIZE,
    CENTER,
    PREMIUM,
    RACK_SIZE,
    TILE_VALUES,
)

BLANK = "?"
LEVELS = ("easy", "medium", "hard", "expert")
_LETTERS = [chr(c) for c in range(ord("A"), ord("Z") + 1)]

# A generous ceiling on recursive node visits per generate() call so a
# pathological board can never hang the host.  Generation runs off the event
# loop anyway; this is a belt-and-braces backstop.  Reaching it returns whatever
# was found so far (the AI still gets a move; the coach's top-5 is approximate).
_NODE_BUDGET = 400_000


class Move:
    """One legal play: where to place it, the word string accepted by the engine
    (lowercase letters mark blanks), its exact score, and how many new tiles it
    uses (7 == a bingo)."""

    __slots__ = ("row", "col", "direction", "word", "score", "new_count")

    def __init__(self, row, col, direction, word, score, new_count):
        self.row = row
        self.col = col
        self.direction = direction
        self.word = word
        self.score = score
        self.new_count = new_count

    @property
    def coord(self):
        return f"{chr(ord('A') + self.col)}{self.row + 1}"

    @property
    def is_bingo(self):
        return self.new_count == RACK_SIZE

    def __repr__(self):
        return (f"Move({self.coord} {self.direction} {self.word!r} "
                f"= {self.score})")


# --------------------------------------------------------------------- trie
class _Node:
    __slots__ = ("kids", "word")

    def __init__(self):
        self.kids = {}
        self.word = False


def _build_trie(words):
    root = _Node()
    for w in words:
        node = root
        for ch in w:
            nxt = node.kids.get(ch)
            if nxt is None:
                nxt = _Node()
                node.kids[ch] = nxt
            node = nxt
        node.word = True
    return root


# Building the trie over a full word list takes ~1-2s and a few hundred MB, so we
# cache it per dictionary object and only rebuild if the word count changes.
_trie_cache = {}


def _trie_for(dictionary):
    key = id(dictionary)
    token = len(dictionary)
    cached = _trie_cache.get(key)
    if cached is None or cached[1] != token:
        root = _build_trie(dictionary.words)
        _trie_cache[key] = (root, token)
        return root
    return cached[0]


# --------------------------------------------------------------- scoring
def _score_line(line_cells):
    """Score one word given its cells as ``(r, c, letter, is_blank, is_new)``.

    Premium squares apply only under freshly placed tiles -- identical to
    :meth:`engine.Engine._score_word`."""
    word_mult = 1
    score = 0
    for (r, c, letter, is_blank, is_new) in line_cells:
        base = 0 if is_blank else TILE_VALUES[letter]
        if is_new:
            prem = PREMIUM[r][c]
            letter_mult = 3 if prem == "t" else 2 if prem == "d" else 1
            if prem == "T":
                word_mult *= 3
            elif prem == "D":
                word_mult *= 2
            score += base * letter_mult
        else:
            score += base
    return score * word_mult


def _cross_score(cells, r, c, letter, is_blank, perp):
    """Score the perpendicular word formed by placing ``letter`` at ``(r, c)``.

    ``cells`` is the board *before* the move (original coordinates); ``perp`` is
    the unit step of the perpendicular axis.  Returns 0 if no cross word forms
    (the placed tile has no neighbour on that axis), matching the engine, which
    only counts cross words of length >= 2."""
    pdr, pdc = perp
    sr, sc = r, c
    while True:
        nr, nc = sr - pdr, sc - pdc
        if 0 <= nr < BOARD_SIZE and 0 <= nc < BOARD_SIZE and cells[nr][nc] is not None:
            sr, sc = nr, nc
        else:
            break
    line = []
    cr, cc = sr, sc
    while 0 <= cr < BOARD_SIZE and 0 <= cc < BOARD_SIZE:
        if cr == r and cc == c:
            line.append((cr, cc, letter, is_blank, True))
        else:
            t = cells[cr][cc]
            if t is None:
                break
            line.append((cr, cc, t[0], t[1], False))
        cr, cc = cr + pdr, cc + pdc
    if len(line) < 2:
        return 0
    return _score_line(line)


# ------------------------------------------------------------- generation
class _Gen:
    """One generation pass over a single orientation.

    ``grid`` is the board in the *current* orientation (the transpose for the
    'down' pass), while ``orig`` is always the real board, used for scoring so we
    never assume the premium layout is transpose-symmetric.  ``to_orig`` maps an
    oriented ``(r, c)`` back to real coordinates; ``perp`` is the perpendicular
    step in real coordinates for cross-word scoring."""

    def __init__(self, grid, orig, rack, dictionary, root, direction,
                 to_orig, perp, empty, results, counter):
        self.grid = grid
        self.orig = orig
        self.dictionary = dictionary
        self.root = root
        self.direction = direction
        self.to_orig = to_orig
        self.perp = perp
        self.empty = empty
        self.results = results
        self.size = BOARD_SIZE
        self.counter = counter            # shared [int] node-visit budget
        # Mutable rack as letter -> count, mutated/restored across recursion.
        self.rack = {}
        for t in rack:
            self.rack[t] = self.rack.get(t, 0) + 1
        self.rack_size = len(rack)
        self.cross = self._cross_checks()

    # -- precompute, for every empty square, the set of letters that form a
    #    valid cross word (None == no constraint: the square has no neighbour on
    #    the cross axis, so any letter is allowed and no cross word is formed).
    def _cross_checks(self):
        size = self.size
        cross = [[None] * size for _ in range(size)]
        for r in range(size):
            for c in range(size):
                if self.grid[r][c] is not None:
                    continue
                up = []
                rr = r - 1
                while rr >= 0 and self.grid[rr][c] is not None:
                    up.append(self.grid[rr][c][0])
                    rr -= 1
                up.reverse()
                down = []
                rr = r + 1
                while rr < size and self.grid[rr][c] is not None:
                    down.append(self.grid[rr][c][0])
                    rr += 1
                if not up and not down:
                    cross[r][c] = None
                    continue
                prefix, suffix = "".join(up), "".join(down)
                allowed = set()
                for L in _LETTERS:
                    if self.dictionary.is_valid(prefix + L + suffix):
                        allowed.add(L)
                cross[r][c] = allowed
        return cross

    def _is_anchor(self, r, c):
        if self.grid[r][c] is not None:
            return False
        if self.empty:
            return self.to_orig(r, c) == CENTER
        for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nr, nc = r + dr, c + dc
            if 0 <= nr < self.size and 0 <= nc < self.size and self.grid[nr][nc] is not None:
                return True
        return False

    def run(self):
        for r in range(self.size):
            for c in range(self.size):
                if self._is_anchor(r, c):
                    self._from_anchor(r, c)

    def _from_anchor(self, r, c):
        """Enumerate every valid start column for a word covering anchor ``c``
        and extend rightwards from each.  Starting at ``start`` is valid only
        when the square just left of it is empty/off-board (a clean word
        boundary); we walk left through forced tiles and empty squares but stop
        before another anchor (which owns words starting further left -- this is
        what prevents the same word being generated twice)."""
        starts = []
        empties = 0
        cc = c
        while True:
            if cc == 0 or self.grid[r][cc - 1] is None:
                starts.append(cc)
            if cc == 0:
                break
            left = cc - 1
            if self.grid[r][left] is None:
                if self._is_anchor(r, left):
                    break
                empties += 1
                if empties > self.rack_size:
                    break          # not enough tiles to fill that many gaps
            cc = left
        for start in starts:
            self._extend(self.root, r, start, c, [], [])

    def _extend(self, node, r, col, anchor, main, placed):
        if self.counter[0] <= 0:
            return
        self.counter[0] -= 1
        size = self.size
        if col >= size or self.grid[r][col] is None:
            # The square at ``col`` is empty or off the board: the word built so
            # far (covering [start, col-1]) is complete here.
            if (node.word and placed and len(main) >= 2 and (col - 1) >= anchor):
                self._record(main, placed)
            if col >= size:
                return
            allowed = self.cross[r][col]
            for L, child in node.kids.items():
                if allowed is not None and L not in allowed:
                    continue
                if self.rack.get(L, 0) > 0:                 # use a real tile
                    self.rack[L] -= 1
                    main.append((r, col, L, False, True))
                    placed.append((r, col, L, False))
                    self._extend(child, r, col + 1, anchor, main, placed)
                    placed.pop()
                    main.pop()
                    self.rack[L] += 1
                if self.rack.get(BLANK, 0) > 0:             # use a blank as L
                    self.rack[BLANK] -= 1
                    main.append((r, col, L, True, True))
                    placed.append((r, col, L, True))
                    self._extend(child, r, col + 1, anchor, main, placed)
                    placed.pop()
                    main.pop()
                    self.rack[BLANK] += 1
        else:
            L = self.grid[r][col][0]
            child = node.kids.get(L)
            if child is not None:
                main.append((r, col, L, self.grid[r][col][1], False))
                self._extend(child, r, col + 1, anchor, main, placed)
                main.pop()

    def _record(self, main, placed):
        to_orig = self.to_orig
        orig_main = [to_orig(r, c) + (L, b, isnew) for (r, c, L, b, isnew) in main]
        # main-word score
        word_mult = 1
        sc = 0
        for (r, c, L, b, isnew) in orig_main:
            base = 0 if b else TILE_VALUES[L]
            if isnew:
                prem = PREMIUM[r][c]
                lm = 3 if prem == "t" else 2 if prem == "d" else 1
                if prem == "T":
                    word_mult *= 3
                elif prem == "D":
                    word_mult *= 2
                sc += base * lm
            else:
                sc += base
        total = sc * word_mult
        # cross words
        orig_placed = []
        for (r, c, L, b) in placed:
            orr, occ = to_orig(r, c)
            orig_placed.append((orr, occ, L, b))
            total += _cross_score(self.orig, orr, occ, L, b, self.perp)
        if len(placed) == RACK_SIZE:
            total += BINGO_BONUS

        sr, sc0 = orig_main[0][0], orig_main[0][1]
        word = "".join((L.lower() if (isnew and b) else L)
                       for (_, _, L, b, isnew) in orig_main)
        move = Move(sr, sc0, self.direction, word, total, len(placed))
        key = tuple(sorted(orig_placed))
        prev = self.results.get(key)
        if prev is None or move.score > prev.score:
            self.results[key] = move


def generate_moves(cells, rack, dictionary):
    """Return every legal :class:`Move` for ``rack`` on the board ``cells``.

    ``cells`` is a 15x15 grid of ``None`` or ``(LETTER, is_blank)``; ``rack`` is
    a list of tiles (``'?'`` for a blank).  Returns ``[]`` when validation is
    disabled or the word list is empty (no dictionary to play against)."""
    if not getattr(dictionary, "enabled", True) or len(dictionary) == 0:
        return []
    root = _trie_for(dictionary)
    results = {}
    counter = [_NODE_BUDGET]
    empty = all(cells[r][c] is None
                for r in range(BOARD_SIZE) for c in range(BOARD_SIZE))

    # across: identity; down: operate on the transpose and map back.
    _Gen(cells, cells, rack, dictionary, root, "across",
         lambda r, c: (r, c), (1, 0), empty, results, counter).run()
    transposed = [[cells[r][c] for r in range(BOARD_SIZE)] for c in range(BOARD_SIZE)]
    _Gen(transposed, cells, rack, dictionary, root, "down",
         lambda r, c: (c, r), (0, 1), empty, results, counter).run()
    return list(results.values())


# ------------------------------------------------------------ AI selection
def best_moves(cells, rack, dictionary, k=5):
    """The ``k`` highest-scoring legal plays, best first (ties broken so the
    result is deterministic for a given board/rack)."""
    moves = generate_moves(cells, rack, dictionary)
    moves.sort(key=lambda m: (-m.score, m.row, m.col, m.direction, m.word))
    return moves[:k]


def choose_ai_move(cells, rack, dictionary, level, rng=None):
    """Pick a move for an AI player at the given difficulty, or ``None`` if there
    is no legal play (the caller then passes/swaps).

    expert plays the maximum; hard plays near the top; medium plays a mid-table
    move; easy deliberately plays a weak (but legal) move."""
    rng = rng or random.Random()
    moves = generate_moves(cells, rack, dictionary)
    if not moves:
        return None
    moves.sort(key=lambda m: (m.score, m.row, m.col, m.direction, m.word))
    n = len(moves)                       # ascending: moves[-1] is the best
    if level == "expert":
        top = moves[-1].score
        pool = [m for m in moves if m.score == top]
    elif level == "hard":
        k = max(1, round(n * 0.10))
        pool = moves[-k:]                # top ~10%
    elif level == "medium":
        lo = int(n * 0.35)
        hi = max(lo + 1, int(n * 0.65))
        pool = moves[lo:hi] or moves
    else:                                # easy: a weak but real move
        k = max(1, round(n * 0.25))
        pool = moves[:k]                 # bottom ~25%
    return rng.choice(pool)


# --------------------------------------------------------------- coaching
# A comment for every quality tier; the exact line is chosen deterministically
# (no RNG) so behaviour is reproducible.
_COMMENTS = {
    "optimal": [
        "Optimal play -- you left nothing on the table!",
        "Chef's kiss. That was the best play available.",
        "Flawless. The board had nothing better.",
    ],
    "great": [
        "Great play!",
        "Slick. Very close to the best move.",
        "Strong move -- well spotted.",
    ],
    "good": [
        "Good play!",
        "Solid. There was a little more on offer, though.",
        "Nice one.",
    ],
    "ok": [
        "Not bad.",
        "Decent, but you left points out there.",
        "Playable -- but you can do better.",
    ],
    "meh": [
        "Eh, you can do better.",
        "Hmm. There were much juicier plays.",
        "That'll do... barely.",
    ],
    "bad": [
        "Try harder bro.",
        "Oof. You left a LOT of points on the rack.",
        "C'mon, the board was wide open!",
    ],
}


def coach_comment(actual_score, best_score, is_bingo):
    """A funny one-liner grading ``actual_score`` against the best available."""
    if is_bingo:
        return "BINGO! All seven tiles -- absolute showoff."
    if best_score <= 0:
        return "Nice -- there wasn't much else to play there."
    if actual_score >= best_score:
        tier = "optimal"
    else:
        ratio = actual_score / best_score
        if ratio >= 0.85:
            tier = "great"
        elif ratio >= 0.65:
            tier = "good"
        elif ratio >= 0.45:
            tier = "ok"
        elif ratio >= 0.25:
            tier = "meh"
        else:
            tier = "bad"
    options = _COMMENTS[tier]
    return options[actual_score % len(options)]


def coach_report(cells, rack, dictionary, played_word, played_score, is_bingo, k=5):
    """Build the private coaching payload shown to a player after they move:
    the top ``k`` plays that were available from their rack, plus a comment.

    Returns ``None`` when there is nothing useful to say (validation disabled or
    no alternative plays found)."""
    if not getattr(dictionary, "enabled", True) or len(dictionary) == 0:
        return None
    tops = best_moves(cells, rack, dictionary, k)
    if not tops:
        return None
    best_score = tops[0].score
    return {
        "played": {"word": played_word, "score": played_score},
        "best": [[m.word.upper(), m.coord, m.direction, m.score] for m in tops],
        "comment": coach_comment(played_score, best_score, is_bingo),
    }
