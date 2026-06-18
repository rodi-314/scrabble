"""Word validation.

The bundled word list (``data/dictionary.txt``) contains every valid two-letter
Scrabble word plus a few hundred common longer words so the game is playable out
of the box.  For serious play, point ``--dict`` at a fuller word list (one or
more words per line; lines starting with ``#`` are ignored).
"""

import os


class Dictionary:
    def __init__(self, words=None, enabled=True):
        self.enabled = enabled
        self.words = set()
        for w in (words or []):
            w = w.strip().upper()
            if w:
                self.words.add(w)

    def is_valid(self, word):
        """Return True if *word* is acceptable (always True when disabled)."""
        if not self.enabled:
            return True
        return word.upper() in self.words

    def __len__(self):
        return len(self.words)


def default_path():
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data", "dictionary.txt"
    )


def load(path=None, enabled=True):
    """Load a :class:`Dictionary`.  Missing files yield an empty word set."""
    if not enabled:
        return Dictionary(enabled=False)
    path = path or default_path()
    words = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                words.extend(line.split())
    except OSError:
        words = []
    return Dictionary(words=words, enabled=True)
