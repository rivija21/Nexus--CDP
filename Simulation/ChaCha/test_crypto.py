"""Known-answer tests for bpsk_crypto and the Noise KK handshake.

Every vector below is published data, not output of this code:

  RFC 8439  ChaCha20 block (2.3.2), encryption (2.4.2), Poly1305 (2.5.2),
            AEAD (2.8.2)
  RFC 7748  X25519 (5.2, both vectors; 1 and 1000 iterations), DH (6.1)
  RFC 5869  HKDF-SHA256 test case 3 (the Noise HKDF is RFC 5869, info = '')
  Noise KK  transcript produced by the independent `noiseprotocol` 0.3.1
            reference implementation with fixed keys (see NOISE_KAT)

Both backends are tested: the pure-Python one always, 'cryptography' when it
is installed, plus a cross-check of the two on random inputs.

    python3 test_crypto.py          (about 5 s; the 1000-iteration X25519
                                     vector dominates on the pure backend)
"""

import importlib.util
import os
import random
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FAILURES = []
H = bytes.fromhex


def check(label, cond, detail=''):
    print('  [%s] %s%s' % ('PASS' if cond else 'FAIL', label,
                           (' - ' + detail) if detail else ''))
    if not cond:
        FAILURES.append(label)


def load_backend(choice):
    """A private copy of bpsk_crypto with BPSK_CRYPTO forced."""
    old = os.environ.get('BPSK_CRYPTO')
    os.environ['BPSK_CRYPTO'] = choice
    try:
        spec = importlib.util.spec_from_file_location(
            'bpsk_crypto_' + choice, os.path.join(HERE, 'bpsk_crypto.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:                                      # noqa: BLE001
        return None
    finally:
        if old is None:
            os.environ.pop('BPSK_CRYPTO', None)
        else:
            os.environ['BPSK_CRYPTO'] = old


SUNSCREEN = (b"Ladies and Gentlemen of the class of '99: If I could offer you "
             b"only one tip for the future, sunscreen would be it.")


def rfc_vectors(C):
    print('\n--- %s backend: RFC vectors' % C.BACKEND)
    if C.BACKEND == 'pure-python':
        blk = C._chacha20_block(struct.unpack('<8I', bytes(range(32))), 1,
                                struct.unpack('<3I',
                                              H('000000090000004a00000000')))
        check('RFC 8439 2.3.2 ChaCha20 block', blk == H(
            '10f1e7e4d13b5915500fdd1fa32071c4c7d1f4c733c068030422aa9ac3d46c4e'
            'd2826446079faa0914c2d705d98b02a2b5129cd1de164eb9cbd083e8a2503c4e'))
        ct = C._chacha20_xor(bytes(range(32)), 1, H('000000000000004a00000000'),
                             SUNSCREEN)
        check('RFC 8439 2.4.2 ChaCha20 encryption', ct == H(
            '6e2e359a2568f98041ba0728dd0d6981e97e7aec1d4360c20a27afccfd9fae0b'
            'f91b65c5524733ab8f593dabcd62b3571639d624e65152ab8f530c359f0861d8'
            '07ca0dbf500d6a6156a38e088a22b65e52bc514d16ccf806818ce91ab7793736'
            '5af90bbf74a35be6b40b8eedf2785e42874d'))
        tag = C._poly1305(H('85d6be7857556d337f4452fe42d506a8'
                            '0103808afb0db2fd4abff6af4149f51b'),
                          b'Cryptographic Forum Research Group')
        check('RFC 8439 2.5.2 Poly1305', tag == H('a8061dc1305136c6c22b8baf0c0127a9'))

    key = bytes(range(0x80, 0xA0))
    nonce = H('070000004041424344454647')
    aad = H('50515253c0c1c2c3c4c5c6c7')
    want = H('d31a8d34648e60db7b86afbc53ef7ec2a4aded51296e08fea9e2b5a736ee62d6'
             '3dbea45e8ca9671282fafb69da92728b1a71de0a9e060b2905d6a5b67ecd3b36'
             '92ddbd7f2d778b8c9803aee328091b58fab324e4fad675945585808b4831d7bc'
             '3ff4def08e4b7a9de576d26586cec64b6116'
             '1ae10b594f09e26a7e902ecbd0600691')
    got = C.aead_encrypt(key, nonce, SUNSCREEN, aad)
    check('RFC 8439 2.8.2 AEAD seal', got == want)
    check('RFC 8439 2.8.2 AEAD open', C.aead_decrypt(key, nonce, want, aad)
          == SUNSCREEN)
    bad = bytearray(want)
    bad[5] ^= 1
    try:
        C.aead_decrypt(key, nonce, bytes(bad), aad)
        rejected = False
    except C.AuthError:
        rejected = True
    check('one flipped ciphertext bit is rejected', rejected)
    try:
        C.aead_decrypt(key, nonce, want, aad[:-1] + b'\x00')
        rejected = False
    except C.AuthError:
        rejected = True
    check('altered associated data is rejected', rejected)

    x = C._dh if C.BACKEND == 'cryptography' else C._py_x25519
    check('RFC 7748 5.2 X25519 vector 1', x(
        H('a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4'),
        H('e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c'))
        == H('c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552'))
    check('RFC 7748 5.2 X25519 vector 2', x(
        H('4b66e9d4d1b4673c5ad22691957d6af5c11b6421e0ea01d42ca4169e7918ba0d'),
        H('e5210f12786811d3f4b7959d0538ae2c31dbe7106fc03c3efc4cd549c715a493'))
        == H('95cbde9476e8907d7aade45cb4b873f88b595a68799fa152e6f8f7647aac7957'))
    k = u = (9).to_bytes(32, 'little')
    for i in range(1000):
        k, u = x(k, u), k
        if i == 0:
            check('RFC 7748 5.2 X25519 iterated x1', k == H(
                '422c8e7a6227d7bca1350b3e2bb7279f7897b87bb6854b783c60e80311ae3079'))
    check('RFC 7748 5.2 X25519 iterated x1000', k == H(
        '684cf59ba83309552800ef566f2f4d3c1c3887c49360e3875f2eb94d99532c51'))
    a = H('77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a')
    b = H('5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb')
    ap, bp = C.x25519_base(a), C.x25519_base(b)
    check('RFC 7748 6.1 public keys', ap == H(
        '8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a')
        and bp == H('de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f'))
    shared = H('4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742')
    check('RFC 7748 6.1 shared secret, both directions',
          C.x25519(a, bp) == shared and C.x25519(b, ap) == shared)
    try:
        C.x25519(a, bytes(32))
        refused = False
    except ValueError:
        refused = True
    check('a low-order public key (all-zero secret) is refused', refused)

    o1, o2 = C.hkdf2(b'', b'\x0b' * 22)
    check('RFC 5869 test case 3 (Noise HKDF)', (o1 + o2)[:42] == H(
        '8da4e775a563c18f715f802a063c5a31b8a11f5c5ee1879ec3454e5f3c738d2d'
        '9d201395faa4b61a96c8'))
    try:
        C.self_test()
        ok = True
    except RuntimeError:
        ok = False
    check('start-up self test passes', ok)


# Noise_KK_25519_ChaChaPoly_SHA256 with s_i = 00..1f, s_r = 20..3f,
# e_i = 40..5f, e_r = 60..7f, prologue = prologue('bpsk-lab', 1, 2),
# payloads b'init-payload' / b'resp'; T1 = initiator's first transport message
# encrypting b'hello'. Produced by noiseprotocol 0.3.1 (independent code).
NOISE_KAT = {
    'msg1': '79a631eede1bf9c98f12032cdeadd0e7a079398fc786b88cc846ec89af85a51a'
            '2f40f04a1b1213f34b206cb807f472097b703178a550143e9ab36b22',
    'msg2': '675dd574ed7789310b3d2e7681f3790b466c773b1521fecf36577958371ea52f'
            'fd9a7a7b31cd85e3e160d4cab3059088afe17164',
    't1': '57a691ed2e10bac677d4467452b9b8d6ab8910bba0',
}


def noise_kat():
    import bpsk_crypto as C
    import bpsk_secure as S
    print('\n--- Noise KK against the reference transcript (%s)' % C.BACKEND)
    s_i, s_r = bytes(range(32)), bytes(range(32, 64))
    e_i, e_r = bytes(range(64, 96)), bytes(range(96, 128))
    pro = S.prologue('bpsk-lab', 1, 2)
    m1, st = S.kk_initiator_start(pro, s_i, C.x25519_base(s_i),
                                  C.x25519_base(s_r), b'init-payload',
                                  e_priv=e_i)
    check('handshake message 1 matches the reference', m1 == H(NOISE_KAT['msg1']))
    p1, m2, r_send, r_recv, rh = S.kk_responder(
        pro, s_r, C.x25519_base(s_r), C.x25519_base(s_i), m1,
        lambda p: b'resp', e_priv=e_r)
    check('responder reads the initiator payload', p1 == b'init-payload')
    check('handshake message 2 matches the reference', m2 == H(NOISE_KAT['msg2']))
    p2, i_send, i_recv, ih = S.kk_initiator_finish(st, m2)
    check('both sides agree on keys and transcript hash',
          p2 == b'resp' and i_send == r_recv and i_recv == r_send and ih == rh)
    check('first transport message matches the reference',
          C.aead_encrypt(i_send, S._nonce(0), b'hello', b'')
          == H(NOISE_KAT['t1']))
    # A different prologue (other network name or addresses) must not verify.
    m1x, _ = S.kk_initiator_start(S.prologue('other-lab', 1, 2), s_i,
                                  C.x25519_base(s_i), C.x25519_base(s_r),
                                  b'init-payload', e_priv=e_i)
    try:
        S.kk_responder(pro, s_r, C.x25519_base(s_r), C.x25519_base(s_i), m1x,
                       lambda p: b'')
        refused = False
    except C.AuthError:
        refused = True
    check('a handshake bound to another network is refused', refused)
    # Someone with a roster key who is NOT the initiator cannot pose as it.
    s_x = bytes(range(128, 160))
    m1y, _ = S.kk_initiator_start(pro, s_x, C.x25519_base(s_x),
                                  C.x25519_base(s_r), b'init-payload')
    try:
        S.kk_responder(pro, s_r, C.x25519_base(s_r), C.x25519_base(s_i), m1y,
                       lambda p: b'')
        refused = False
    except C.AuthError:
        refused = True
    check('a third node cannot complete a handshake as address 1', refused)


def cross_check(P, N):
    print('\n--- pure-python vs cryptography on random inputs')
    rng = random.Random(5)
    same = True
    for _ in range(300):
        k, n = os.urandom(32), os.urandom(12)
        pt = os.urandom(rng.randint(0, 700))
        aad = os.urandom(rng.randint(0, 40))
        a = P.aead_encrypt(k, n, pt, aad)
        same &= a == N.aead_encrypt(k, n, pt, aad)
        same &= N.aead_decrypt(k, n, a, aad) == pt
    check('300 random AEAD seal/open agree', same)
    same = True
    for _ in range(50):
        s, u = os.urandom(32), os.urandom(32)
        same &= P.x25519_base(s) == N.x25519_base(s)
        same &= P._py_x25519(s, u) == N._dh(s, u)
    check('50 random X25519 operations agree', same)


def main():
    pure = load_backend('pure')
    native = load_backend('native')
    rfc_vectors(pure)
    if native is not None:
        rfc_vectors(native)
        cross_check(pure, native)
    else:
        print('\n(cryptography not installed: native backend not tested)')
    noise_kat()
    print('\n%s' % ('all crypto checks passed' if not FAILURES
                    else 'FAILED: ' + ', '.join(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == '__main__':
    sys.exit(main())
