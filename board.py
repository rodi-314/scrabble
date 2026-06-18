"""The 15x15 board and the tiles that sit on it."""

from constants import BOARD_SIZE, TILE_VALUES


class Tile:
    """A single placed tile.  ``is_blank`` tiles always score zero but still
    spell out the letter they were played as."""

    __slots__ = ("letter", "is_blank")

    def __init__(self, letter, is_blank=False):
        self.letter = letter.upper()
        self.is_blank = is_blank

    def value(self):
        return 0 if self.is_blank else TILE_VALUES[self.letter]

    def to_json(self):
        return [self.letter, self.is_blank]


class Board:
    def __init__(self):
        self.grid = [[None] * BOARD_SIZE for _ in range(BOARD_SIZE)]

    def get(self, r, c):
        return self.grid[r][c]

    def set(self, r, c, tile):
        self.grid[r][c] = tile

    def is_empty(self):
        return all(
            self.grid[r][c] is None
            for r in range(BOARD_SIZE)
            for c in range(BOARD_SIZE)
        )

    def to_json(self):
        return [
            [cell.to_json() if cell else None for cell in row]
            for row in self.grid
        ]
