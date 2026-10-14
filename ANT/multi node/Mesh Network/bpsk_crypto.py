"""Cryptographic primitives for the BPSK link: ChaCha20-Poly1305, X25519, HKDF.

Standard algorithms only, so the design can be defended by citation rather
than argument:

    ChaCha20-Poly1305 AEAD   RFC 8439
    X25519 key agreement     RFC 7748
    HMAC-SHA256 / HKDF       RFC 2104 / RFC 5869 (in the Noise form)

Two interchangeable backends produce identical bytes:

  * 'cryptography'  - the pyca/cryptography package (OpenSSL), used when it is
                      importable. Microseconds per frame.
  * 'pure-python'   - the code below, standard library only, so a lab machine
                      needs nothing installed. ~0.3 ms to seal a 256-byte
                      frame, a few ms per X25519 operation.

Force one with BPSK_CRYPTO=pure or BPSK_CRYPTO=native in the environment.
Both are checked against the RFC test vectors by test_crypto.py, and against
each other when both are present. self_test() runs a short known-answer test
at start-up so a broken backend is refused rather than used.

The pure-Python code is not constant-time: Python integers and table lookups
leak timing. Over a half-duplex radio link with stop-and-wait ARQ an attacker
cannot collect timing at anything like the resolution these attacks need, but
this is a teaching/lab implementation, not a hardened one.
"""

import hashlib
import hmac
import os
import struct

__all__ = ['BACKEND', 'aead_encrypt', 'aead_decrypt', 'x25519',
           'x25519_base', 'generate_private', 'hkdf2', 'sha256',
           'AuthError', 'self_test', 'KEY_LEN', 'TAG_LEN']

KEY_LEN = 32
TAG_LEN = 16
MASK32 = 0xFFFFFFFF


class AuthError(Exception):
    """AEAD tag did not verify: the frame is forged, damaged or not ours."""


# --------------------------------------------------------------------------
# ChaCha20 (RFC 8439 section 2.3 / 2.4)
# --------------------------------------------------------------------------
_SIGMA = struct.unpack('<4I', b'expand 32-byte k')


def _chacha20_block(key_words, counter, nonce_words):
    """One 64-byte keystream block. key_words: 8 ints, nonce_words: 3 ints."""
    x0, x1, x2, x3 = _SIGMA
    x4, x5, x6, x7, x8, x9, x10, x11 = key_words
    x12 = counter & MASK32
    x13, x14, x15 = nonce_words
    s = (x0, x1, x2, x3, x4, x5, x6, x7, x8, x9, x10, x11, x12, x13, x14, x15)
    for _ in range(10):
        # column round
        x0 = (x0 + x4) & MASK32; x12 ^= x0; x12 = ((x12 << 16) | (x12 >> 16)) & MASK32
        x8 = (x8 + x12) & MASK32; x4 ^= x8; x4 = ((x4 << 12) | (x4 >> 20)) & MASK32
        x0 = (x0 + x4) & MASK32; x12 ^= x0; x12 = ((x12 << 8) | (x12 >> 24)) & MASK32
        x8 = (x8 + x12) & MASK32; x4 ^= x8; x4 = ((x4 << 7) | (x4 >> 25)) & MASK32
        x1 = (x1 + x5) & MASK32; x13 ^= x1; x13 = ((x13 << 16) | (x13 >> 16)) & MASK32
        x9 = (x9 + x13) & MASK32; x5 ^= x9; x5 = ((x5 << 12) | (x5 >> 20)) & MASK32
        x1 = (x1 + x5) & MASK32; x13 ^= x1; x13 = ((x13 << 8) | (x13 >> 24)) & MASK32
        x9 = (x9 + x13) & MASK32; x5 ^= x9; x5 = ((x5 << 7) | (x5 >> 25)) & MASK32
        x2 = (x2 + x6) & MASK32; x14 ^= x2; x14 = ((x14 << 16) | (x14 >> 16)) & MASK32
        x10 = (x10 + x14) & MASK32; x6 ^= x10; x6 = ((x6 << 12) | (x6 >> 20)) & MASK32
        x2 = (x2 + x6) & MASK32; x14 ^= x2; x14 = ((x14 << 8) | (x14 >> 24)) & MASK32
        x10 = (x10 + x14) & MASK32; x6 ^= x10; x6 = ((x6 << 7) | (x6 >> 25)) & MASK32
        x3 = (x3 + x7) & MASK32; x15 ^= x3; x15 = ((x15 << 16) | (x15 >> 16)) & MASK32
        x11 = (x11 + x15) & MASK32; x7 ^= x11; x7 = ((x7 << 12) | (x7 >> 20)) & MASK32
        x3 = (x3 + x7) & MASK32; x15 ^= x3; x15 = ((x15 << 8) | (x15 >> 24)) & MASK32
        x11 = (x11 + x15) & MASK32; x7 ^= x11; x7 = ((x7 << 7) | (x7 >> 25)) & MASK32
        # diagonal round
        x0 = (x0 + x5) & MASK32; x15 ^= x0; x15 = ((x15 << 16) | (x15 >> 16)) & MASK32
        x10 = (x10 + x15) & MASK32; x5 ^= x10; x5 = ((x5 << 12) | (x5 >> 20)) & MASK32
        x0 = (x0 + x5) & MASK32; x15 ^= x0; x15 = ((x15 << 8) | (x15 >> 24)) & MASK32
        x10 = (x10 + x15) & MASK32; x5 ^= x10; x5 = ((x5 << 7) | (x5 >> 25)) & MASK32
        x1 = (x1 + x6) & MASK32; x12 ^= x1; x12 = ((x12 << 16) | (x12 >> 16)) & MASK32
        x11 = (x11 + x12) & MASK32; x6 ^= x11; x6 = ((x6 << 12) | (x6 >> 20)) & MASK32
        x1 = (x1 + x6) & MASK32; x12 ^= x1; x12 = ((x12 << 8) | (x12 >> 24)) & MASK32
        x11 = (x11 + x12) & MASK32; x6 ^= x11; x6 = ((x6 << 7) | (x6 >> 25)) & MASK32
        x2 = (x2 + x7) & MASK32; x13 ^= x2; x13 = ((x13 << 16) | (x13 >> 16)) & MASK32
        x8 = (x8 + x13) & MASK32; x7 ^= x8; x7 = ((x7 << 12) | (x7 >> 20)) & MASK32
        x2 = (x2 + x7) & MASK32; x13 ^= x2; x13 = ((x13 << 8) | (x13 >> 24)) & MASK32
        x8 = (x8 + x13) & MASK32; x7 ^= x8; x7 = ((x7 << 7) | (x7 >> 25)) & MASK32
        x3 = (x3 + x4) & MASK32; x14 ^= x3; x14 = ((x14 << 16) | (x14 >> 16)) & MASK32
        x9 = (x9 + x14) & MASK32; x4 ^= x9; x4 = ((x4 << 12) | (x4 >> 20)) & MASK32
        x3 = (x3 + x4) & MASK32; x14 ^= x3; x14 = ((x14 << 8) | (x14 >> 24)) & MASK32
        x9 = (x9 + x14) & MASK32; x4 ^= x9; x4 = ((x4 << 7) | (x4 >> 25)) & MASK32
    out = (x0, x1, x2, x3, x4, x5, x6, x7, x8, x9, x10, x11, x12, x13, x14, x15)
    return struct.pack('<16I', *[(a + b) & MASK32 for a, b in zip(out, s)])


def _chacha20_xor(key, counter, nonce, data):
    kw = struct.unpack('<8I', key)
    nw = struct.unpack('<3I', nonce)
    n = len(data)
    if not n:
        return b''
    stream = b''.join(_chacha20_block(kw, counter + i, nw)
                      for i in range((n + 63) // 64))
    return (int.from_bytes(data, 'little')
            ^ int.from_bytes(stream[:n], 'little')).to_bytes(n, 'little')


# --------------------------------------------------------------------------
# Poly1305 (RFC 8439 section 2.5)
# --------------------------------------------------------------------------
_P1305 = (1 << 130) - 5


def _poly1305(key, msg):
    r = int.from_bytes(key[:16], 'little') & 0x0ffffffc0ffffffc0ffffffc0fffffff
    s = int.from_bytes(key[16:32], 'little')
    acc = 0
    for i in range(0, len(msg), 16):
        block = msg[i:i + 16] + b'\x01'
        acc = ((acc + int.from_bytes(block, 'little')) * r) % _P1305
    return ((acc + s) & ((1 << 128) - 1)).to_bytes(16, 'little')


def _pad16(n):
    return b'\x00' * ((16 - n % 16) % 16)


def _mac_data(aad, ct):
    return (aad + _pad16(len(aad)) + ct + _pad16(len(ct))
            + struct.pack('<QQ', len(aad), len(ct)))


def _py_encrypt(key, nonce, plaintext, aad):
    otk = _chacha20_block(struct.unpack('<8I', key), 0,
                          struct.unpack('<3I', nonce))[:32]
    ct = _chacha20_xor(key, 1, nonce, plaintext)
    return ct + _poly1305(otk, _mac_data(aad, ct))


def _py_decrypt(key, nonce, data, aad):
    if len(data) < TAG_LEN:
        raise AuthError('too short')
    ct, tag = data[:-TAG_LEN], data[-TAG_LEN:]
    otk = _chacha20_block(struct.unpack('<8I', key), 0,
                          struct.unpack('<3I', nonce))[:32]
    if not hmac.compare_digest(_poly1305(otk, _mac_data(aad, ct)), tag):
        raise AuthError('tag mismatch')
    return _chacha20_xor(key, 1, nonce, ct)


# --------------------------------------------------------------------------
# X25519 (RFC 7748 section 5)
# --------------------------------------------------------------------------
_P = (1 << 255) - 19
_A24 = 121665


def _clamp(k):
    k = bytearray(k)
    k[0] &= 248
    k[31] &= 127
    k[31] |= 64
    return int.from_bytes(k, 'little')


def _py_x25519(k, u):
    k = _clamp(k)
    x1 = int.from_bytes(u, 'little') & ((1 << 255) - 1)
    x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
    p = _P
    for t in range(254, -1, -1):
        kt = (k >> t) & 1
        swap ^= kt
        if swap:
            x2, x3 = x3, x2
            z2, z3 = z3, z2
        swap = kt
        a = x2 + z2
        aa = a * a % p
        b = x2 - z2
        bb = b * b % p
        e = aa - bb
        c = x3 + z3
        d = x3 - z3
        da = d * a % p
        cb = c * b % p
        x3 = (da + cb) % p
        x3 = x3 * x3 % p
        z3 = (da - cb) % p
        z3 = x1 * (z3 * z3 % p) % p
        x2 = aa * bb % p
        z2 = e * (aa + _A24 * e) % p
    if swap:
        x2, x3 = x3, x2
        z2, z3 = z3, z2
    return (x2 * pow(z2, p - 2, p) % p).to_bytes(32, 'little')


_BASE = (9).to_bytes(32, 'little')


# --------------------------------------------------------------------------
# Backend selection
# --------------------------------------------------------------------------
def _load_native():
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey, X25519PublicKey)
    from cryptography.hazmat.primitives import serialization
    from cryptography.exceptions import InvalidTag

    raw = serialization.Encoding.Raw
    rawpub = serialization.PublicFormat.Raw

    def enc(key, nonce, pt, aad):
        return ChaCha20Poly1305(key).encrypt(nonce, pt, aad)

    def dec(key, nonce, data, aad):
        try:
            return ChaCha20Poly1305(key).decrypt(nonce, data, aad)
        except InvalidTag:
            raise AuthError('tag mismatch')

    def dh(k, u):
        priv = X25519PrivateKey.from_private_bytes(k)
        if u == _BASE:
            return priv.public_key().public_bytes(raw, rawpub)
        try:
            return priv.exchange(X25519PublicKey.from_public_bytes(u))
        except ValueError:
            # OpenSSL refuses an all-zero shared secret (low-order point);
            # the RFC function returns it. Match the RFC so both backends
            # agree, and let the caller reject it.
            return b'\x00' * 32
    return enc, dec, dh


_choice = os.environ.get('BPSK_CRYPTO', '').strip().lower()
BACKEND = 'pure-python'
_enc, _dec, _dh = _py_encrypt, _py_decrypt, _py_x25519
if _choice != 'pure':
    try:
        _enc, _dec, _dh = _load_native()
        BACKEND = 'cryptography'
    except Exception:                                    # noqa: BLE001
        if _choice == 'native':
            raise
        _enc, _dec, _dh = _py_encrypt, _py_decrypt, _py_x25519


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def aead_encrypt(key, nonce, plaintext, aad=b''):
    """ChaCha20-Poly1305: returns ciphertext || 16-byte tag."""
    return _enc(bytes(key), bytes(nonce), bytes(plaintext), bytes(aad))


def aead_decrypt(key, nonce, data, aad=b''):
    """Inverse of aead_encrypt. Raises AuthError if the tag does not verify."""
    return _dec(bytes(key), bytes(nonce), bytes(data), bytes(aad))


def x25519(private, public):
    """Shared secret. Raises ValueError on a low-order (all-zero) result."""
    out = _dh(bytes(private), bytes(public))
    if out == b'\x00' * 32:
        raise ValueError('X25519 produced the all-zero secret '
                         '(low-order public key)')
    return out


def x25519_base(private):
    return _dh(bytes(private), _BASE)


def generate_private():
    return os.urandom(32)


def sha256(data):
    return hashlib.sha256(data).digest()


def hkdf2(chaining_key, ikm):
    """Noise HKDF with two outputs = RFC 5869 with salt=ck, info=''."""
    temp = hmac.new(chaining_key, ikm, hashlib.sha256).digest()
    out1 = hmac.new(temp, b'\x01', hashlib.sha256).digest()
    out2 = hmac.new(temp, out1 + b'\x02', hashlib.sha256).digest()
    return out1, out2


def self_test():
    """Known-answer test of the active backend (RFC 8439 2.8.2, RFC 7748 6.1).

    Returns None, or raises RuntimeError naming what failed.
    """
    key = bytes(range(0x80, 0xA0))
    nonce = bytes.fromhex('070000004041424344454647')
    aad = bytes.fromhex('50515253c0c1c2c3c4c5c6c7')
    pt = (b"Ladies and Gentlemen of the class of '99: If I could offer you "
          b"only one tip for the future, sunscreen would be it.")
    want_tag = bytes.fromhex('1ae10b594f09e26a7e902ecbd0600691')
    got = aead_encrypt(key, nonce, pt, aad)
    if got[-16:] != want_tag or aead_decrypt(key, nonce, got, aad) != pt:
        raise RuntimeError('ChaCha20-Poly1305 known-answer test failed (%s)'
                           % BACKEND)
    a = bytes.fromhex('77076d0a7318a57d3c16c17251b26645'
                      'df4c2f87ebc0992ab177fba51db92c2a')
    b_pub = bytes.fromhex('de9edb7d7b7dc1b4d35b61c2ece43537'
                          '3f8343c85b78674dadfc7e146f882b4f')
    want = bytes.fromhex('4a5d9d5ba4ce2de1728e3bf480350f25'
                         'e07e21c947d19e3376f09b3c1e161742')
    if x25519(a, b_pub) != want:
        raise RuntimeError('X25519 known-answer test failed (%s)' % BACKEND)
