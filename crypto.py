"""Authenticated encryption for the LAN link.

Every websocket frame is sealed with **AES-256-GCM** under a key derived from a
shared *room key* (passphrase) via **scrypt**.  GCM gives confidentiality *and*
integrity: a frame that was sniffed cannot be read, and a frame that was forged
or tampered with (or sent under the wrong room key) fails authentication and is
rejected.  Authentication therefore doubles as access control -- only holders of
the room key can talk to the game.

Key agreement is a one-shot, no-secrets handshake: when a client connects, the
server sends a fresh random *salt* plus the scrypt parameters in the clear (a
salt is public by design, exactly like TLS).  Both sides run scrypt over the
shared room key with that salt to arrive at the same 256-bit session key, so
each connection gets its own key and nonces never repeat across sessions.

This module needs the third-party ``cryptography`` package; importing it without
that package raises a clear, actionable error.
"""

import json
import os
import secrets

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
except ImportError as exc:  # pragma: no cover - exercised only without the dep
    raise ImportError(
        "Network encryption needs the 'cryptography' package. "
        "Install it with:  pip install -r requirements.txt"
    ) from exc

# scrypt cost parameters.  N=2**14 keeps key derivation well under ~50 ms per
# connection while making an offline guess of a weak room key expensive.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
# Accepted range for a peer-supplied handshake.  The lower bound blocks a
# downgrade; the upper bounds stop a hostile/MITM server from inflating the work
# factor to exhaust the client's memory/CPU *before* authentication (scrypt
# allocates ~128*N*r bytes).  These caps hold that to well under ~150 MB.
MIN_SCRYPT_N = 2 ** 13
MAX_SCRYPT_N = 2 ** 16
MAX_SCRYPT_R = 16
MAX_SCRYPT_P = 4
KEY_LEN = 32               # AES-256
NONCE_LEN = 12             # 96-bit GCM nonce (random per frame)
SALT_LEN = 16
HELLO_VERSION = 1


class DecryptError(Exception):
    """Raised when a frame cannot be authenticated/decrypted (wrong key etc.)."""


def derive_key(room_key, salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P):
    """Derive the 256-bit session key from the room key and salt via scrypt."""
    kdf = Scrypt(salt=salt, length=KEY_LEN, n=n, r=r, p=p)
    return kdf.derive(room_key.encode("utf-8"))


class Cipher:
    """AES-256-GCM sealer for one connection (one derived session key)."""

    def __init__(self, key):
        self._aead = AESGCM(key)

    def encrypt(self, plaintext):
        """Seal a str/bytes plaintext, returning ``nonce || ciphertext+tag``."""
        if isinstance(plaintext, str):
            plaintext = plaintext.encode("utf-8")
        nonce = os.urandom(NONCE_LEN)
        return nonce + self._aead.encrypt(nonce, plaintext, None)

    def decrypt(self, blob):
        """Open a sealed frame, returning the plaintext str.

        Raises :class:`DecryptError` on anything that is not a valid frame under
        this key: a wrong room key, tampering, or a plaintext (text) frame where
        ciphertext was expected.
        """
        if not isinstance(blob, (bytes, bytearray)):
            raise DecryptError("expected a binary ciphertext frame")
        if len(blob) < NONCE_LEN + 16:        # nonce + minimum GCM tag
            raise DecryptError("ciphertext frame too short")
        nonce, ct = bytes(blob[:NONCE_LEN]), bytes(blob[NONCE_LEN:])
        try:
            return self._aead.decrypt(nonce, ct, None).decode("utf-8")
        except Exception as exc:              # InvalidTag, UnicodeDecodeError, ...
            raise DecryptError("could not authenticate frame") from exc


def new_salt():
    return os.urandom(SALT_LEN)


def cipher_for(room_key, salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P):
    return Cipher(derive_key(room_key, salt, n, r, p))


def make_hello(salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P):
    """Build the cleartext handshake frame (salt + KDF params; no secrets)."""
    return json.dumps({"v": HELLO_VERSION, "salt": salt.hex(),
                       "n": n, "r": r, "p": p})


def parse_hello(text):
    """Parse a handshake frame -> (salt, n, r, p).  Validates the KDF floor so a
    tampered handshake cannot silently downgrade the work factor."""
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8", "replace")
    data = json.loads(text)
    if data.get("v") != HELLO_VERSION:
        raise ValueError("unsupported handshake version")
    salt = bytes.fromhex(str(data["salt"]))
    n, r, p = int(data["n"]), int(data["r"]), int(data["p"])
    # Reject params outside the accepted window: too low is a downgrade, too high
    # is a pre-authentication memory/CPU exhaustion attack via scrypt.
    if (len(salt) < 8
            or not (MIN_SCRYPT_N <= n <= MAX_SCRYPT_N)
            or not (1 <= r <= MAX_SCRYPT_R)
            or not (1 <= p <= MAX_SCRYPT_P)):
        raise ValueError("handshake parameters out of the accepted range")
    return salt, n, r, p


def gen_room_key():
    """A strong, human-typeable default room key: 5 groups of 4 hex (80 bits)."""
    return "-".join(secrets.token_hex(2) for _ in range(5))
