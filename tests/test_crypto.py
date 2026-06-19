"""Unit tests for the AES-256-GCM link encryption (crypto.py).

Run directly with:  python tests/test_crypto.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import crypto


def test_roundtrip_same_key_and_salt():
    salt = crypto.new_salt()
    c1 = crypto.cipher_for("room-key", salt)
    c2 = crypto.cipher_for("room-key", salt)        # same key derived independently
    blob = c1.encrypt('{"hi":1}')
    assert isinstance(blob, (bytes, bytearray))
    assert blob[:0] == b""                           # it's bytes, not text
    assert c2.decrypt(blob) == '{"hi":1}'


def test_wrong_key_rejected():
    salt = crypto.new_salt()
    good = crypto.cipher_for("right-key", salt)
    bad = crypto.cipher_for("wrong-key", salt)
    blob = good.encrypt("secret")
    try:
        bad.decrypt(blob)
    except crypto.DecryptError:
        return
    raise AssertionError("a wrong key must not decrypt the frame")


def test_tampered_frame_rejected():
    salt = crypto.new_salt()
    c = crypto.cipher_for("k", salt)
    blob = bytearray(c.encrypt("hello"))
    blob[-1] ^= 0x01                                 # flip a bit in the GCM tag
    try:
        c.decrypt(bytes(blob))
    except crypto.DecryptError:
        return
    raise AssertionError("a tampered frame must fail authentication")


def test_text_frame_rejected():
    c = crypto.cipher_for("k", crypto.new_salt())
    try:
        c.decrypt("plaintext, not ciphertext")       # a text frame where binary is due
    except crypto.DecryptError:
        return
    raise AssertionError("a non-binary frame must be rejected")


def test_short_frame_rejected():
    c = crypto.cipher_for("k", crypto.new_salt())
    try:
        c.decrypt(b"too short")
    except crypto.DecryptError:
        return
    raise AssertionError("a too-short frame must be rejected")


def test_nonce_is_random_per_frame():
    c = crypto.cipher_for("k", crypto.new_salt())
    a = c.encrypt("x")
    b = c.encrypt("x")
    assert a != b, "the same plaintext must encrypt to different ciphertext"
    assert a[:crypto.NONCE_LEN] != b[:crypto.NONCE_LEN], "nonces must differ"


def test_hello_roundtrip():
    salt = crypto.new_salt()
    text = crypto.make_hello(salt)
    s2, n, r, p = crypto.parse_hello(text)
    assert s2 == salt
    assert (n, r, p) == (crypto.SCRYPT_N, crypto.SCRYPT_R, crypto.SCRYPT_P)


def test_hello_rejects_downgrade():
    salt = crypto.new_salt()
    weak = json.dumps({"v": 1, "salt": salt.hex(), "n": 2, "r": 8, "p": 1})
    try:
        crypto.parse_hello(weak)
    except ValueError:
        return
    raise AssertionError("a downgraded (weak) KDF handshake must be rejected")


def test_hello_rejects_inflation():
    # A hostile/MITM server must not be able to inflate the scrypt work factor
    # (a pre-authentication memory/CPU exhaustion attack on the client).
    salt = crypto.new_salt()
    for params in ({"n": 2 ** 24, "r": 8, "p": 1},      # huge N  (~16 GB)
                   {"n": 2 ** 14, "r": 2 ** 16, "p": 1},  # huge r
                   {"n": 2 ** 14, "r": 8, "p": 9999}):     # huge p
        blob = json.dumps({"v": 1, "salt": salt.hex(), **params})
        try:
            crypto.parse_hello(blob)
        except ValueError:
            continue
        raise AssertionError(f"inflated KDF params must be rejected: {params}")


def test_gen_room_key_is_strong_and_unique():
    k1 = crypto.gen_room_key()
    k2 = crypto.gen_room_key()
    assert k1 != k2
    assert len(k1.replace("-", "")) >= 16           # >= 64 bits of hex entropy


def run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in tests:
        fn()
        passed += 1
        print(f"  ok  {fn.__name__}")
    print(f"\n{passed}/{len(tests)} crypto tests passed.")


if __name__ == "__main__":
    run_all()
