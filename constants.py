"""Static Scrabble data: tile distribution, tile values, and the board layout.

Nothing here depends on the network or game state, so it can be imported freely
by the engine, the server and the client.
"""

BOARD_SIZE = 15
CENTER = (7, 7)          # the star square, in (row, col) form -> "H8"
RACK_SIZE = 7
BINGO_BONUS = 50         # extra points for using all 7 tiles in one move
MAX_PLAYERS = 8

# Standard English tile distribution (letter -> count); '?' is the blank tile.
TILE_DISTRIBUTION = {
    'A': 9, 'B': 2, 'C': 2, 'D': 4, 'E': 12, 'F': 2, 'G': 3, 'H': 2, 'I': 9,
    'J': 1, 'K': 1, 'L': 4, 'M': 2, 'N': 6, 'O': 8, 'P': 2, 'Q': 1, 'R': 6,
    'S': 4, 'T': 6, 'U': 4, 'V': 2, 'W': 2, 'X': 1, 'Y': 2, 'Z': 1, '?': 2,
}

# Standard letter values; the blank '?' is worth nothing.
TILE_VALUES = {
    'A': 1, 'B': 3, 'C': 3, 'D': 2, 'E': 1, 'F': 4, 'G': 2, 'H': 4, 'I': 1,
    'J': 8, 'K': 5, 'L': 1, 'M': 3, 'N': 1, 'O': 1, 'P': 3, 'Q': 10, 'R': 1,
    'S': 1, 'T': 1, 'U': 1, 'V': 4, 'W': 4, 'X': 8, 'Y': 4, 'Z': 10, '?': 0,
}

# Premium-square template.  T = triple word, D = double word, t = triple letter,
# d = double letter, . = plain.  The center square (7, 7) is a double-word
# square (the star) and is handled exactly like any other 'D'.
_TEMPLATE = [
    "T..d...T...d..T",
    ".D...t...t...D.",
    "..D...d.d...D..",
    "d..D...d...D..d",
    "....D.....D....",
    ".t...t...t...t.",
    "..d...d.d...d..",
    "T..d...D...d..T",
    "..d...d.d...d..",
    ".t...t...t...t.",
    "....D.....D....",
    "d..D...d...D..d",
    "..D...d.d...D..",
    ".D...t...t...D.",
    "T..d...T...d..T",
]

PREMIUM = [list(row) for row in _TEMPLATE]

# Short labels shown on empty premium squares.
PREMIUM_NAMES = {'T': 'TW', 'D': 'DW', 't': 'TL', 'd': 'DL', '.': ''}

# ANSI colour codes used by the client renderer.  Plain ASCII glyphs are used on
# the board itself so the game still reads correctly on terminals without colour
# (e.g. legacy Windows cmd.exe with colour disabled).
ANSI = {
    'reset': "\x1b[0m",
    'bold': "\x1b[1m",
    'dim': "\x1b[2m",
    'TW': "\x1b[1;37;41m",        # white on red
    'DW': "\x1b[1;37;45m",        # white on magenta
    'TL': "\x1b[1;37;44m",        # white on blue
    'DL': "\x1b[1;30;46m",        # black on cyan
    'star': "\x1b[1;33;45m",      # yellow star on magenta
    'tile': "\x1b[1;30;43m",      # black on yellow (a placed tile)
    'blank_tile': "\x1b[1;31;47m",  # red on white (a played blank)
    'rack': "\x1b[1;30;42m",      # black on green (your rack)
    'turn': "\x1b[1;32m",         # bright green
    'warn': "\x1b[1;33m",         # yellow
}
