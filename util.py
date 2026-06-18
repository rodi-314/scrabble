"""Small cross-platform helpers: terminal colour, screen clearing, LAN IP lookup.

These deliberately avoid third-party dependencies so the client and server run on
a bare Python install on Linux, macOS or Windows (PowerShell / cmd).
"""

import os
import socket
import sys


def enable_ansi():
    """Enable ANSI escape-sequence processing on the current terminal.

    On Linux/macOS this is a no-op.  On Windows 10+ it switches the console into
    virtual-terminal mode so colours and cursor moves work in PowerShell and
    cmd.exe.  Returns True ONLY when virtual-terminal processing is actually
    confirmed enabled, so callers can honestly disable colour when it is not
    (avoiding literal escape codes printing as garbage on legacy consoles).
    """
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # -11 == STD_OUTPUT_HANDLE; 0x0004 == ENABLE_VIRTUAL_TERMINAL_PROCESSING
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            if kernel32.SetConsoleMode(handle, mode.value | 0x0004):
                return True
    except Exception:
        pass
    # Best-effort nudge for some consoles, but we cannot confirm VT support here,
    # so report failure and let the caller fall back to a non-ANSI screen clear.
    try:
        os.system("")
    except Exception:
        pass
    return False


def get_local_ip():
    """Best-effort discovery of this machine's LAN IP address.

    Opens a UDP socket and 'connects' it to an off-machine address; no packets
    are actually sent, but the OS picks the outgoing interface, whose address we
    read back.  Falls back to the hostname lookup, then loopback.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"
    finally:
        s.close()


def clear_screen(ansi=True):
    """Clear the terminal.

    With *ansi* True, use the ANSI clear/home sequence (fast, flicker-free).
    When ANSI is unavailable (legacy console or ``--no-color``), shell out to
    ``cls``/``clear`` so the screen still clears instead of printing raw escapes.
    """
    if ansi:
        sys.stdout.write("\x1b[2J\x1b[H")
    else:
        os.system("cls" if os.name == "nt" else "clear")
