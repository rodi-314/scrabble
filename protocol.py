"""Wire protocol: every websocket message is a JSON object with a ``type`` key.

Client -> server: join, start, play, pass, swap, chat
Server -> client: welcome, state, rack, error, info, chat
"""

import json

# client -> server
JOIN = "join"
START = "start"
PLAY = "play"
PASS = "pass"
SWAP = "swap"
CHAT = "chat"

# server -> client
WELCOME = "welcome"
STATE = "state"
RACK = "rack"
ERROR = "error"
INFO = "info"
CHATMSG = "chat"


def _reject_constant(value):
    # Python's json.loads accepts Infinity/-Infinity/NaN by default; reject them
    # so a hostile client cannot smuggle non-finite numbers into the engine.
    raise ValueError(f"non-finite number not allowed: {value}")


def dumps(obj):
    return json.dumps(obj)


def loads(text):
    return json.loads(text, parse_constant=_reject_constant)
