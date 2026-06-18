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
- The **`websockets`** package:

  ```bash
  pip install -r requirements.txt
  # or simply:  pip install websockets
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
  Listening on 0.0.0.0:8765
  Dictionary : 1000+ words loaded

  Players on your LAN can join with:
      python scrabble.py join <HOST_IP> --port 8765
========================================================
```

The host can also play — just open a **second terminal** and run the join
command below.

### 2. Everyone joins

On each player's machine (including the host's second terminal):

```bash
python scrabble.py join <HOST_IP>
```

On Windows (PowerShell or cmd) it is exactly the same command:

```powershell
python scrabble.py join <HOST_IP>
```

You'll be asked for a name (or pass `--name Alice`).

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
| `start` | (First player only) begin the game |
| `say <message>` or just type text | Chat with the other players |
| `help` | Show the command help |
| `board` | Redraw the screen |
| `quit` | Leave the game |

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
- End-game scoring: leftover rack values are subtracted, and a player who empties
  their rack collects everyone else's leftovers. The game ends when the bag is
  empty and a player goes out, or after two full rounds with no scoring.

### If your connection drops

If your client briefly loses the network mid-game, it automatically reconnects
and reclaims your seat (rack and score intact). Reconnection is protected by a
secret token the server hands you when you join, so no one else on the LAN can
take over your seat while you're away. New players can only join while the game
is still in the lobby; once it has started, only reconnects are accepted.

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
python scrabble.py host [--host 0.0.0.0] [--port 8765] [--dict FILE] [--no-dict]
python scrabble.py join <HOST_IP> [--port 8765] [--name NAME] [--no-color]
```

- `--host` — interface to bind. `0.0.0.0` (default) listens on every LAN
  interface. Use a specific address to restrict it.
- `--port` — TCP port (default `8765`). Host and players must agree.
- `--no-color` — disable ANSI colours for very basic terminals.

---

## Testing

No third-party test runner needed:

```bash
python tests/test_engine.py        # rules + scoring unit tests
python tests/test_integration.py   # real server + two real clients over websockets
```

---

## Troubleshooting

- **"Could not connect"** — confirm the host IP and port, that the server is
  running, and that both machines are on the same `192.168.XXX.0/24` LAN.
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
scrabble.py        # CLI entry point: host / join
constants.py       # tile distribution, values, board premium layout
board.py           # Board + Tile
engine.py          # rules, validation, scoring, turn & game management
dictionary.py      # word-list loading and validation
protocol.py        # websocket message types (JSON)
server.py          # async websocket server (the host)
client.py          # async websocket client + terminal UI
util.py            # cross-platform colour / IP helpers
data/dictionary.txt
tests/             # engine unit tests + an end-to-end network test
```
