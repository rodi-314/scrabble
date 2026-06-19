"""Authoritative Scrabble game logic: bag, racks, turns, validation, scoring.

The engine is pure and synchronous: it knows nothing about networking.  The
server calls into it under a lock and broadcasts the resulting state.  All rule
enforcement lives here so a malicious client cannot cheat.
"""

import random
from collections import deque

from constants import (
    BOARD_SIZE,
    BINGO_BONUS,
    CENTER,
    MAX_PLAYERS,
    PREMIUM,
    RACK_SIZE,
    TILE_DISTRIBUTION,
    TILE_VALUES,
)
from board import Board, Tile


class MoveError(Exception):
    """Raised for any illegal move; the message is shown to the player."""


def coord_name(r, c):
    """(row, col) -> human coordinate, e.g. (7, 7) -> 'H8'."""
    return f"{chr(ord('A') + c)}{r + 1}"


def parse_coord(text):
    """'H8' -> (row, col).  Raises ValueError on bad input."""
    text = text.strip().upper()
    if len(text) < 2 or not text[0].isalpha() or not text[1:].isdigit():
        raise ValueError("Coordinate must look like H8 (column letter + row number).")
    col = ord(text[0]) - ord("A")
    row = int(text[1:]) - 1
    if not (0 <= col < BOARD_SIZE and 0 <= row < BOARD_SIZE):
        raise ValueError("That coordinate is off the board (use A-O and 1-15).")
    return row, col


class Player:
    def __init__(self, pid, name, token):
        self.id = pid
        self.name = name
        self.token = token          # secret required to reconnect to this seat
        self.rack = []
        self.score = 0
        self.connected = True

    def rack_value(self):
        return sum(0 if t == "?" else TILE_VALUES[t] for t in self.rack)


class Engine:
    def __init__(self, dictionary, rng=None):
        self.dictionary = dictionary
        self.rng = rng or random.Random()
        self.board = Board()
        self.bag = self._new_bag()
        self.players = []
        self.by_id = {}
        self.by_name = {}        # lower-cased name -> Player
        self.turn = 0
        self.phase = "lobby"     # lobby -> playing -> over
        self.scoreless = 0       # consecutive passes/swaps; ends the game if high
        self.log = deque(maxlen=500)   # bounded human-readable event history
        self.winners = []
        self.end_summary = None        # final scoreboard once the game is over
        self._next_id = 1

    def _make_token(self):
        return f"{self.rng.getrandbits(64):016x}"

    # ------------------------------------------------------------------ setup
    def _new_bag(self):
        bag = []
        for letter, count in TILE_DISTRIBUTION.items():
            bag.extend([letter] * count)
        self.rng.shuffle(bag)
        return bag

    def add_player(self, name, token=None):
        name = name.strip()
        if not name:
            raise MoveError("Your name cannot be empty.")
        key = name.lower()
        existing = self.by_name.get(key)
        if existing is not None:
            if existing.connected:
                raise MoveError("That name is already taken.")
            # Reconnecting to a disconnected seat requires the secret token that
            # was issued when the seat was created, so nobody else can grab it.
            if not token or token != existing.token:
                raise MoveError("That name is taken by a disconnected player; the correct reconnect token is required.")
            existing.connected = True
            self.log.append(f"{existing.name} reconnected.")
            return existing
        if self.phase != "lobby":
            raise MoveError("The game has already started; you can only rejoin with the name you used.")
        if len(self.players) >= MAX_PLAYERS:
            raise MoveError(f"The game is full ({MAX_PLAYERS} players maximum).")
        player = Player(self._next_id, name, self._make_token())
        self._next_id += 1
        self.players.append(player)
        self.by_id[player.id] = player
        self.by_name[key] = player
        self.log.append(f"{player.name} joined.")
        return player

    def remove_player(self, pid):
        """Used only while still in the lobby; drops the seat entirely."""
        player = self.by_id.pop(pid, None)
        if not player:
            return
        self.by_name.pop(player.name.lower(), None)
        if player in self.players:
            self.players.remove(player)
        if self.players:
            self.turn %= len(self.players)
        self.log.append(f"{player.name} left.")

    def set_connected(self, pid, connected):
        """Mark a seat connected/disconnected during play (allows reconnects)."""
        player = self.by_id.get(pid)
        if not player:
            return
        player.connected = connected
        if not connected:
            self.log.append(f"{player.name} disconnected.")
            cur = self.current()
            if self.phase == "playing" and cur and cur.id == pid:
                self._advance()

    def start(self, pid):
        if self.phase != "lobby":
            raise MoveError("The game has already started.")
        if not self.players or self.players[0].id != pid:
            raise MoveError("Only the first player to join can start the game.")
        if len(self.players) < 2:
            raise MoveError("You need at least 2 players to start.")
        for player in self.players:
            self._refill(player)
        self.phase = "playing"
        self.turn = 0
        self.log.append("The game has started!")

    # ----------------------------------------------------------------- turns
    def current(self):
        if not self.players:
            return None
        return self.players[self.turn]

    def _require_turn(self, pid):
        if self.phase != "playing":
            raise MoveError("The game is not in progress.")
        cur = self.current()
        if cur is None or cur.id != pid:
            raise MoveError("It is not your turn.")

    def _advance(self):
        n = len(self.players)
        for _ in range(n):
            self.turn = (self.turn + 1) % n
            if self.players[self.turn].connected:
                return
        # Nobody is connected; leave the turn where it is.

    def _refill(self, player):
        while len(player.rack) < RACK_SIZE and self.bag:
            player.rack.append(self.bag.pop())

    # ----------------------------------------------------------------- moves
    def play(self, pid, row, col, direction, word):
        self._require_turn(pid)
        player = self.by_id[pid]
        info = self._validate_and_score(player, row, col, direction, word)
        # Commit: place tiles, spend them from the rack.
        for (r, c, letter, is_blank) in info["new"]:
            self.board.set(r, c, Tile(letter, is_blank))
            player.rack.remove("?" if is_blank else letter)
        player.score += info["score"]
        self._refill(player)
        self.scoreless = 0
        played = ", ".join(f"{w}({s})" for w, s in info["words"])
        bonus = " +50 (bingo!)" if info["bingo"] else ""
        self.log.append(f"{player.name} played {played} = {info['score']}{bonus}")
        if not player.rack and not self.bag:
            self._end_game(player)        # player used every tile -> game ends
        else:
            self._advance()
        return info

    def passing(self, pid):
        self._require_turn(pid)
        player = self.by_id[pid]
        self.scoreless += 1
        self.log.append(f"{player.name} passed.")
        self._after_scoreless()

    def swap(self, pid, letters):
        self._require_turn(pid)
        player = self.by_id[pid]
        if len(self.bag) < RACK_SIZE:
            raise MoveError(
                f"You can only swap when at least {RACK_SIZE} tiles remain in the bag."
            )
        norm = []
        for ch in letters:
            if ch in ("?", "_", "*"):
                norm.append("?")
            elif ch.isalpha():
                norm.append(ch.upper())
            else:
                raise MoveError(f"'{ch}' is not a tile you can swap.")
        if not norm:
            raise MoveError("Say which tiles to swap, e.g. 'swap aei'.")
        spare = list(player.rack)
        for ch in norm:
            if ch not in spare:
                shown = "blank" if ch == "?" else ch
                raise MoveError(f"You don't have a '{shown}' tile to swap.")
            spare.remove(ch)
        # Perform the exchange: remove, draw replacements, return the old tiles.
        for ch in norm:
            player.rack.remove(ch)
        drawn = [self.bag.pop() for _ in range(len(norm))]
        player.rack.extend(drawn)
        self.bag.extend(norm)
        self.rng.shuffle(self.bag)
        self.scoreless += 1
        self.log.append(f"{player.name} swapped {len(norm)} tile(s).")
        self._after_scoreless()

    def _after_scoreless(self):
        # Two full rounds of nobody scoring ends the game.
        if self.scoreless >= 2 * len(self.players):
            self._end_game(None)
        else:
            self._advance()

    def _end_game(self, went_out):
        """Close the game following the official end-game scoring rules.

        Each player's unplayed tiles are deducted from their score.  If a player
        went out (emptied their rack while the bag was empty), they instead gain
        the sum of everyone else's unplayed tiles.  A structured ``end_summary``
        records the before/after of every seat so the result can be shown
        transparently, with each player's leftover tiles revealed.
        """
        self.phase = "over"
        # Snapshot scores and leftover racks *before* applying any adjustment.
        base = {p.id: p.score for p in self.players}
        leftover = {p.id: "".join(sorted(p.rack)) for p in self.players}
        leftover_value = {p.id: p.rack_value() for p in self.players}

        if went_out is not None:
            # The player who emptied their rack collects everyone else's leftovers.
            gained = 0
            for p in self.players:
                if p is went_out:
                    continue
                p.score -= leftover_value[p.id]
                gained += leftover_value[p.id]
            went_out.score += gained
            reason = (f"{went_out.name} used all of their tiles and the bag is "
                      f"empty, so the game ends.")
        else:
            for p in self.players:
                p.score -= leftover_value[p.id]
            reason = "Two scoreless rounds in a row - the game is passed out."

        best = max((p.score for p in self.players), default=0)
        self.winners = [p.name for p in self.players if p.score == best]
        self.end_summary = {
            "reason": reason,
            "winners": list(self.winners),
            "rows": [
                {
                    "id": p.id,
                    "name": p.name,
                    "base": base[p.id],
                    "leftover": leftover[p.id],
                    "leftover_value": leftover_value[p.id],
                    "adjustment": p.score - base[p.id],
                    "final": p.score,
                }
                for p in self.players
            ],
        }
        self.log.append("Game over. " + reason)
        self.log.append("Final score: " + ", ".join(
            f"{p.name} {p.score}"
            for p in sorted(self.players, key=lambda x: -x.score)
        ))

    # ------------------------------------------------------- validation core
    def _validate_and_score(self, player, row, col, direction, word):
        if direction not in ("across", "down"):
            raise MoveError("Direction must be 'across' or 'down'.")
        if not word or not all(ch.isalpha() for ch in word):
            raise MoveError("A word may contain only letters (use lowercase for a blank).")
        dr, dc = (0, 1) if direction == "across" else (1, 0)
        n = len(word)

        # Every cell of the word must be on the board.
        for i in range(n):
            r, c = row + dr * i, col + dc * i
            if not (0 <= r < BOARD_SIZE and 0 <= c < BOARD_SIZE):
                raise MoveError("The word runs off the edge of the board.")

        # The word must be complete: no stray tile butting against either end.
        br, bc = row - dr, col - dc
        if 0 <= br < BOARD_SIZE and 0 <= bc < BOARD_SIZE and self.board.get(br, bc):
            raise MoveError("Include the tile that sits just before your word.")
        er, ec = row + dr * n, col + dc * n
        if 0 <= er < BOARD_SIZE and 0 <= ec < BOARD_SIZE and self.board.get(er, ec):
            raise MoveError("Include the tile that sits just after your word.")

        rack = list(player.rack)
        new = []          # (r, c, letter, is_blank) for freshly placed tiles
        main = []         # full main word: (r, c, letter, is_blank, is_new)
        has_through = False
        for i, ch in enumerate(word):
            r, c = row + dr * i, col + dc * i
            letter = ch.upper()
            is_blank = ch.islower()
            cur = self.board.get(r, c)
            if cur is not None:
                if cur.letter != letter:
                    raise MoveError(
                        f"{coord_name(r, c)} already holds '{cur.letter}', not '{letter}'."
                    )
                has_through = True
                main.append((r, c, cur.letter, cur.is_blank, False))
            else:
                need = "?" if is_blank else letter
                if need not in rack:
                    if is_blank:
                        raise MoveError("You have no blank tile to play that lowercase letter.")
                    raise MoveError(f"You don't have the tile '{letter}'.")
                rack.remove(need)
                new.append((r, c, letter, is_blank))
                main.append((r, c, letter, is_blank, True))

        if not new:
            raise MoveError("You must place at least one new tile.")

        # Connectivity.
        if self.board.is_empty():
            if not any((r, c) == CENTER for (r, c, _, _) in new):
                raise MoveError("The first word must cross the center square (H8).")
        else:
            connected = has_through
            if not connected:
                for (r, c, _, _) in new:
                    for ddr, ddc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        nr, nc = r + ddr, c + ddc
                        if 0 <= nr < BOARD_SIZE and 0 <= nc < BOARD_SIZE and self.board.get(nr, nc):
                            connected = True
                            break
                    if connected:
                        break
            if not connected:
                raise MoveError("Your word must connect to tiles already on the board.")

        # Collect every word formed: the main word plus a cross word per new tile.
        words = []
        if len(main) >= 2:
            words.append(main)
        pdr, pdc = dc, dr            # perpendicular direction
        for (r, c, letter, is_blank) in new:
            cross = self._perp_word(r, c, pdr, pdc, letter, is_blank)
            if len(cross) >= 2:
                words.append(cross)
        if not words:
            raise MoveError("Your move must form a word of at least two letters.")

        # Validate every formed word against the dictionary.
        invalid = []
        for wc in words:
            spelled = "".join(x[2] for x in wc)
            if not self.dictionary.is_valid(spelled):
                invalid.append(spelled)
        if invalid:
            raise MoveError("Not a valid word: " + ", ".join(sorted(set(invalid))))

        # Score.
        total = 0
        breakdown = []
        for wc in words:
            s = self._score_word(wc)
            total += s
            breakdown.append(("".join(x[2] for x in wc), s))
        bingo = len(new) == RACK_SIZE
        if bingo:
            total += BINGO_BONUS
        return {"new": new, "score": total, "words": breakdown, "bingo": bingo}

    def _perp_word(self, r, c, pdr, pdc, letter, is_blank):
        """Build the perpendicular word through a freshly placed tile at (r, c).

        Only this one tile is new; any others in the line are pre-existing.
        Returns the ordered cells, or a length-1 list if there is no cross word.
        """
        sr, sc = r, c
        while True:
            nr, nc = sr - pdr, sc - pdc
            if 0 <= nr < BOARD_SIZE and 0 <= nc < BOARD_SIZE and self.board.get(nr, nc):
                sr, sc = nr, nc
            else:
                break
        cells = []
        cr, cc = sr, sc
        while 0 <= cr < BOARD_SIZE and 0 <= cc < BOARD_SIZE:
            if cr == r and cc == c:
                cells.append((cr, cc, letter, is_blank, True))
            else:
                t = self.board.get(cr, cc)
                if t is None:
                    break
                cells.append((cr, cc, t.letter, t.is_blank, False))
            cr, cc = cr + pdr, cc + pdc
        return cells

    def _score_word(self, wc):
        """Score one word.  Premium squares apply only under freshly placed tiles."""
        word_mult = 1
        score = 0
        for (r, c, letter, is_blank, is_new) in wc:
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

    # --------------------------------------------------------- serialization
    def public_state(self):
        cur = self.current()
        return {
            "phase": self.phase,
            "board": self.board.to_json(),
            "players": [
                {
                    "id": p.id,
                    "name": p.name,
                    "score": p.score,
                    "tiles": len(p.rack),
                    "connected": p.connected,
                }
                for p in self.players
            ],
            "turn": cur.id if (self.phase == "playing" and cur) else None,
            "bag": len(self.bag),
            "log": list(self.log)[-12:],
            "winners": self.winners,
            "end_summary": self.end_summary,
            "first_player": self.players[0].id if self.players else None,
        }

    def rack_of(self, pid):
        player = self.by_id.get(pid)
        return list(player.rack) if player else []
