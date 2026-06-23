"""Wire protocol: every websocket message is a JSON object with a ``type`` key.

Client -> server: join, start, play, pass, swap, shuffle, chat, setteam, check,
                  addai, removeai
Server -> client: welcome, state, rack, error, info, chat, checked, coach

A ``join`` may carry ``"spectator": true`` to watch without taking a seat, and a
reconnecting player carries the secret ``"token"`` it was issued on first join.
A ``chat`` may carry ``"scope": "team"`` for a private team-only message.
"""

import json

# client -> server
JOIN = "join"
START = "start"
PLAY = "play"
PASS = "pass"
SWAP = "swap"
SHUFFLE = "shuffle"
CHAT = "chat"
SETTEAM = "setteam"      # set/clear the sender's team (lobby only)
CHECK = "check"          # ask whether one or more words are valid
ADDAI = "addai"          # host: add a computer player (lobby only)
REMOVEAI = "removeai"    # host: remove a computer player (lobby only)

# server -> client
WELCOME = "welcome"
STATE = "state"
RACK = "rack"
ERROR = "error"
INFO = "info"
CHATMSG = "chat"
CHECKED = "checked"      # result of a word-validity check
COACH = "coach"          # private post-move coaching (top plays + a comment)


def _reject_constant(value):
    # Python's json.loads accepts Infinity/-Infinity/NaN by default; reject them
    # so a hostile client cannot smuggle non-finite numbers into the engine.
    raise ValueError(f"non-finite number not allowed: {value}")


def dumps(obj):
    return json.dumps(obj)


def loads(text):
    return json.loads(text, parse_constant=_reject_constant)
