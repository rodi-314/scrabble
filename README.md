# LAN Scrabble

A multiplayer, terminal-based Scrabble game for a local network. One machine
**hosts** a game (runs a small websocket server) and everyone else **joins**
from their own terminal. It runs on Linux, macOS, and Windows (PowerShell or
`cmd.exe`) with nothing but Python and the `websockets` library.

```
      A  B  C  D  E  F  G  H  I  J  K  L  M  N  O
 1   TW  .  . DL  .  .  . TW  .  .  . DL  .  . TW   1
 2    . DW  .  .  . TL  .  .  . TL  .  .  . DW  .   2
 ...
 8   TW  .  . DL  .  .  . H  E  L  L  O DL  . TW   8
 ...
```

---

## Requirements

- **Python 3.8 or newer**
- Two packages — **`websockets`** (transport) and **`cryptography`** (the
  AES-256-GCM encryption that protects every frame):

  ```bash
  pip install -r requirements.txt
  # or simply:  pip install websockets cryptography
  ```

Everything else is the Python standard library.

---

## Network setup

This game is designed to be played over a private LAN — for example a home or
office subnet such as `192.168.XXX.0/24` (the real subnet is redacted here on
purpose). All you need is:

1. Every player connected to the **same LAN / Wi-Fi**.
2. The host's **LAN IP address** (something like `192.168.XXX.YY`).
3. The host's chosen **port** open through any local firewall
   (default: `8765`).

The server binds to `0.0.0.0` by default, so it is reachable from every machine
on the subnet. When you start the server it prints the exact join command,
including the host's detected IP — share that with the other players.

> **Note:** Replace every `<HOST_IP>` / `192.168.XXX.YY` placeholder below with
> the actual address printed by your server. Do not commit real addresses to a
> shared repository.

### Encryption (room key)

**All traffic is encrypted** with **AES-256-GCM** — board state, racks, chat and
the reconnect token are unreadable to anyone sniffing the LAN. When the server
starts it shows a **room key**; players supply that key to join. The key is the
shared secret: it both derives the encryption key (via scrypt, with a fresh
random salt per connection) and gates access — without it you cannot read the
traffic *or* join. The host can set a memorable key with `--key`, otherwise a
strong random one is generated and printed. The room key is shown only on the
host's screen at runtime; never commit it to a shared repository.

---

## How to play

### 1. Host starts the server

On the host machine, from inside the project folder:

```bash
python scrabble.py host
```

It prints something like:

```
========================================================
  LAN SCRABBLE  -  server
========================================================
  Listening on 0.0.0.0:8765  (encrypted: AES-256-GCM)
  Dictionary : 1000+ words loaded
  Turn limit : none
  Coaching   : on (top plays + comments after each move)

  Room key   : 3f7a-1c92-...-redacted
  Share this key with your players -- nobody can join (or sniff the
  game) without it.

  Players on your LAN can join with:
      python scrabble.py join <HOST_IP> --port 8765 --key <ROOM_KEY>
========================================================
```

The host can also play — just open a **second terminal** and run the join
command below.

### 2. Everyone joins

On each player's machine (including the host's second terminal), with the room
key the host shared:

```bash
python scrabble.py join <HOST_IP> --key <ROOM_KEY>
```

On Windows (PowerShell or cmd) it is exactly the same command:

```powershell
python scrabble.py join <HOST_IP> --key <ROOM_KEY>
```

You'll be asked for a name (or pass `--name Alice`). If you omit `--key`, you'll
be prompted for the room key.

### 3. Start the game

The **first player to join** is the host of the match. Once at least two
players are in the lobby, that player types:

```
start
```

Tiles are dealt and play begins.

---

## In-game commands

| Command | What it does |
| --- | --- |
| `play <coord> <across\|down> <word>` | Place a word, e.g. `play H8 across HELLO` |
| `pass` | Forfeit your turn |
| `swap <letters>` | Exchange tiles, e.g. `swap aei` (use `?` for a blank) |
| `shuffle` | Randomly reorder the tiles on your rack |
| `check <word> [word ...]` | Check whether word(s) are valid (any time) |
| `team <name>` | (Lobby) join a team; `team none` to leave it |
| `tc <message>` | Private chat to your teammates only |
| `addai <easy\|medium\|hard\|expert>` | (Host, lobby) add a computer player |
| `removeai <name>` | (Host, lobby) remove a computer player |
| `start` | (First player only) begin the game |
| `say <message>` or just type text | Chat with the other players |
| `values` | Show the point value of every letter |
| `clear` | Wipe the screen and the message history |
| `help` | Show the command help |
| `board` | Redraw the screen |
| `quit` | Leave the game |

Your rack is shown with each tile's **point value printed directly beneath it**,
and `values` prints the full letter-value table at any time. `shuffle`
rearranges your rack to help you spot words. While you are typing, messages from
other players are drawn above your prompt **without erasing what you have typed
so far** — your in-progress command or chat stays on the line. When it becomes
your turn, the prompt plays a brief **flashing "YOUR TURN" animation** (and rings
the terminal bell) so you never miss your move. The tiles placed by the **most
recent move are highlighted** on the board — in a contrasting colour, or wrapped
in `[brackets]` when colour is disabled — so the latest play is easy to spot.

### Line editing

The input line behaves like a real shell prompt:

- **← / →** move the cursor; **Home/End** (or **Ctrl-A/Ctrl-E**) jump to the ends.
- **↑ / ↓** scroll through your **command history** to repeat or tweak a move.
- **Backspace/Delete** edit in place; **Ctrl-U** clears the line and **Ctrl-W**
  erases the previous word.

### Spectating

Anyone can **watch** a game without taking a seat (the room key is still
required — spectators must be able to decrypt the traffic too):

```bash
python scrabble.py spectate <HOST_IP> --key <ROOM_KEY>
```

Spectators see the live board, scores and chat (and can chat themselves), but
they hold no rack and cannot play, pass, swap or start. The player list shows
how many spectators are watching.

### Teams and private chat

In the **lobby**, pick a team and you'll play as a unit:

```
team Red
```

Players on the same team **share a combined score**, and the winner is decided
by the highest **team total** (so two solid players can beat one big scorer).
Teammates also get a **private chat channel** — nobody outside the team (not the
other team, not spectators) can read it:

```
tc let's open with the triple-word in the corner
```

Teaming is entirely opt-in: with no teams set, the game is the usual
free-for-all, and a player who never joins a team is simply a "team of one". The
scoreboard groups players by team and shows each team's total, in-game and on
the final results screen. (`team none` leaves your team while still in the lobby.)

### Word checker

Not sure a word is real? Check it any time — it doesn't use your turn, and even
spectators can:

```
check zydeco qi flummox
```

The server answers from its dictionary, marking each word **valid** or
**INVALID** and showing its face value (tile points). With `--no-dict` the
server accepts everything, and the checker says so.

### Turn time limit

The host can put a clock on every turn:

```bash
python scrabble.py host --turn-limit 60
```

Each player then has that many seconds to move; when the clock runs out the
server **automatically passes** for them and play moves on. Everyone sees a live
`[NNs left]` countdown next to the current turn. Without `--turn-limit` (or with
`0`) there is no clock — the default.

### AI players (easy / medium / hard / expert)

Short a player? Add computer opponents. The host can seat them on the command
line:

```bash
python scrabble.py host --ai easy --ai expert
```

or from the lobby at any time before the game starts:

```
addai hard
removeai Robo(hard)
```

The four difficulties differ in how good a play they choose from everything
that's legal: **expert** always plays the highest-scoring move, **hard** plays
near the top, **medium** plays a middle-of-the-road move, and **easy** picks a
deliberately weak (but real) play. AI seats take their turn automatically after
a short "thinking" pause.

### Coaching: top plays + a comment

After **you** play a word, a private coach (only you see it) shows the
**top-scoring plays you could have made** from that same rack, and ribs you
about it:

```
-- Coach -- you played HELLO for 18.
Best plays you could have made:
   26  THOLES H3 down
   24  HOTELS D7 across
   ...
Good play!
```

The comment scales with how close you got to the best available play — from
"Optimal play!" down to "Try harder bro." Turn it off with `--no-coach` on the
host. (The coach and the AI both need the dictionary, so they do little under
`--no-dict`.)

### Placing words

- **Coordinate** = a column letter (`A`–`O`) followed by a row number
  (`1`–`15`). The center star is `H8`.
- **Direction** is `across` (left → right) or `down` (top → bottom). You can
  abbreviate to `a` / `d`.
- Type the **whole word**, including any letters already on the board that your
  word runs through. For example, if `HELLO` is on the board and you want to add
  `S` to make `HELLOS`, you'd play `play H8 across HELLOS`.
- **Blanks:** type the letter in **lowercase** to mark it as a blank tile. For
  example `play H8 across hELLO` plays a blank as the `H`. Blanks score zero.

### Rules implemented

- Standard 15×15 board with the usual TW / DW / TL / DL premium squares.
- Standard 100-tile English distribution and letter values.
- The first word must cross the center square (`H8`).
- Every subsequent word must connect to tiles already on the board.
- All words formed (the main word **and** every cross-word) must be valid.
- A 50-point bonus for using all seven tiles in one move (a "bingo").
- The most recent move's tiles are highlighted on the board so the latest play
  is easy to see.
- End-game scoring follows the standard rules: each player's leftover rack value
  is subtracted from their score, and a player who empties their rack while the
  bag is empty instead **collects everyone else's leftovers**. The game ends when
  the bag is empty and a player goes out, or after two full rounds with no
  scoring. When it ends, everyone sees a final scoreboard showing each player's
  adjustment, their revealed leftover tiles, the final totals, and the winner.
- In teaming mode, scores are pooled per team and the winner is the team with
  the highest combined total.

### If your connection drops

If your client briefly loses the network mid-game, it automatically reconnects
and reclaims your seat (rack and score intact). Reconnection is protected by a
secret token the server hands you when you join, so no one else on the LAN can
take over your seat while you're away. The reconnect works even if it happens
so quickly that the server has not yet noticed the old link drop — presenting
your token retires the stale connection and hands the seat to your fresh one,
and the old connection can never knock you back offline. New players can only
join while the game is still in the lobby; once it has started, only reconnects
(and spectators) are accepted.

---

## Dictionary

A starter word list lives in `data/dictionary.txt` (every valid two-letter word
plus several hundred common words). For tournament-strength play, supply your
own list:

```bash
python scrabble.py host --dict /path/to/wordlist.txt
```

The file may have one or more words per line; lines starting with `#` are
ignored. To turn validation off entirely (any letters accepted):

```bash
python scrabble.py host --no-dict
```

---

## Options

```
python scrabble.py host     [--host 0.0.0.0] [--port 8765] [--dict FILE] [--no-dict]
                            [--key KEY] [--turn-limit SECONDS] [--ai LEVEL ...] [--no-coach]
python scrabble.py join     <HOST_IP> [--port 8765] [--name NAME] [--no-color] [--key KEY]
python scrabble.py spectate <HOST_IP> [--port 8765] [--name NAME] [--no-color] [--key KEY]
```

- `--host` — interface to bind. `0.0.0.0` (default) listens on every LAN
  interface. Use a specific address to restrict it.
- `--port` — TCP port (default `8765`). Host and players must agree.
- `--key` — the shared **room key** for encryption. On `host`, sets the key
  (default: a strong random one is generated and shown). On `join`/`spectate`,
  the key the host shared (you are prompted if it is omitted).
- `--turn-limit` — seconds per turn before a player is force-passed
  (`0`/omitted = no clock).
- `--ai` — seat an AI player at the given difficulty (`easy`/`medium`/`hard`/
  `expert`). Repeat the flag to add several bots.
- `--no-coach` — turn off the post-move coaching (top plays + comments).
- `--no-color` — disable ANSI colours for very basic terminals.

---

## Testing

No third-party test runner needed:

```bash
python tests/test_engine.py        # rules + scoring + teams + AI seats
python tests/test_crypto.py        # AES-256-GCM encryption unit tests
python tests/test_wordsmith.py     # move generator vs. the engine (sound + complete)
python tests/test_client_render.py # terminal UI + line-editor unit tests
python tests/test_integration.py   # real server + real clients over the encrypted wire
python tests/test_features.py      # teams, private chat, word check, turn limit, AI, coach
```

---

## Troubleshooting

- **"Could not connect"** — confirm the host IP and port, that the server is
  running, and that both machines are on the same `192.168.XXX.0/24` LAN.
- **"Could not establish a secure session" / can't join** — the **room key** is
  wrong. Use the exact key the host's screen shows (`--key`), keys are
  case-sensitive.
- **`cryptography` not installed** — run `pip install -r requirements.txt`;
  encryption (and therefore the game) needs it.
- **Firewall** — allow inbound TCP on the chosen port on the host machine.
  On Windows you may get a Windows Defender Firewall prompt the first time;
  allow access on **Private** networks.
- **No colours on Windows** — make sure you're on Windows 10+ (colours are
  enabled automatically; if virtual-terminal mode can't be turned on, the client
  disables colour by itself). Otherwise add `--no-color`.
- **Dropped connection** — the client retries a few times automatically. If it
  gives up, just run the `join` command again (while the game is in progress you
  keep your seat only if your client reconnects; a fresh process starts over).
- **Find your IP manually** — Linux/macOS: `ip addr` or `ifconfig`;
  Windows: `ipconfig` (look for the `192.168.XXX.YY` address on your LAN
  adapter).

---

## Project layout

```
scrabble.py        # CLI entry point: host / join / spectate
constants.py       # tile distribution, values, board premium layout
board.py           # Board + Tile
engine.py          # rules, validation, scoring, turn & game management
dictionary.py      # word-list loading and validation
wordsmith.py       # move generator + scorer, AI move selection, coaching
protocol.py        # websocket message types (JSON)
crypto.py          # AES-256-GCM link encryption (scrypt-derived room key)
server.py          # async websocket server (the host)
client.py          # async websocket client + terminal UI
util.py            # cross-platform colour / IP helpers
data/dictionary.txt
tests/             # engine, crypto, render, and end-to-end network tests
```
