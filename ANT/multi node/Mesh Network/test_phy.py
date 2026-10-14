"""r6.1 physical-layer helpers (bpsk_phy) and gain staging (bpsk_gain).

numpy only - no GNU Radio. The GNU Radio level (the same pieces inside the
real flowgraph, and the r6.0.1 blocks they replace) is test_grc.py.

    python3 test_phy.py
"""

import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.environ.get('BPSK_TREE') or HERE)

FAILURES = []


def check(label, cond, detail=''):
    print('  [%s] %s%s' % ('PASS' if cond else 'FAIL', label,
                           (' - ' + detail) if detail else ''))
    if not cond:
        FAILURES.append(label)


def section(title):
    print('\n%s' % title)


def main():
    import numpy as np
    try:
        import bpsk_phy as P
        import bpsk_gain as G
    except ImportError as exc:
        check('bpsk_phy and bpsk_gain exist (r6.1)', False, repr(exc))
        return 1
    rng = np.random.default_rng(7)

    def payload(n):
        return bytes(rng.integers(0, 256, n, dtype=np.uint8))

    # ------------------------------------------------------------ deframer
    section('Deframer (replaces correlate_access_code_bb_ts -> repack_bits_bb '
            '-> tagged_stream_to_pdu)')
    frames = [payload(int(n)) for n in rng.integers(13, 600, 40)]
    bits = np.concatenate([P.frame_bits(f) for f in frames])
    d = P.Deframer()
    got = d.feed(bits)
    check('40 frames of 13..600 bytes in one stream all decode', got == frames,
          '%d of 40' % len(got))

    d = P.Deframer()
    got = []
    pos = 0
    while pos < len(bits):
        n = int(rng.integers(1, 5000))
        got += d.feed(bits[pos:pos + n])
        pos += n
    check('the same stream fed in random pieces of 1..5000 bits decodes '
          'identically', got == frames, '%d of 40' % len(got))

    # The r6.0.1 freeze: a DBPSK symbol error flips two adjacent bits, in
    # both copies of the length, so the copies still agree: 13 -> 0x300D.
    good = [payload(40) for _ in range(6)]
    idle = payload(13)
    bad = bytearray(P.frame_bytes(idle))
    at = 64 + 8
    bad[at] ^= 0x30
    bad[at + 2] ^= 0x30
    stream = np.concatenate(
        [P.frame_bits(good[0]),
         np.unpackbits(np.frombuffer(bytes(bad), np.uint8))] +
        [P.frame_bits(g) for g in good[1:]])
    d = P.Deframer()
    got = d.feed(stream)
    check('a header announcing 12301 bytes (both copies agree) is rejected '
          'at once', d.stats['hdr_bad_len'] == 1, repr(d.stats))
    check('... and costs nothing but that frame: all 6 frames around it '
          'decode', got == good, '%d of 6' % len(got))
    check('... and the deframer never holds more than one maximum frame of '
          'bits', d.held_bits <= P.HEADER_BITS + 8 * P.MAX_FRAME + 64,
          '%d bits held' % d.held_bits)

    # a plausible but wrong length (garbled to 500 bytes): only that frame
    bad = bytearray(P.frame_bytes(idle))
    bad[at:at + 4] = bytes([0x01, 0xF4, 0x01, 0xF4])
    stream = np.concatenate(
        [np.unpackbits(np.frombuffer(bytes(bad), np.uint8))] +
        [P.frame_bits(g) for g in good])
    d = P.Deframer()
    got = d.feed(stream)
    check('a plausible wrong length (500 B) loses only its own frame: the '
          'access codes inside it are still searched', got[-6:] == good,
          '%d frames' % len(got))

    # mismatching copies
    bad = bytearray(P.frame_bytes(idle))
    bad[at + 1] ^= 0x01
    d = P.Deframer()
    got = d.feed(np.concatenate(
        [np.unpackbits(np.frombuffer(bytes(bad), np.uint8)),
         P.frame_bits(good[0])]))
    check('length copies that disagree are rejected',
          d.stats['hdr_mismatch'] == 1 and got == [good[0]], repr(d.stats))

    # access code with 2 bit errors still found, 3 not
    for errs, want in ((2, True), (3, False)):
        fb = bytearray(P.frame_bytes(good[0]))
        bitsf = np.unpackbits(np.frombuffer(bytes(fb), np.uint8))
        for i in rng.choice(64, errs, replace=False):
            bitsf[64 * 8 + i] ^= 1           # inside the access code
        got = P.Deframer().feed(bitsf)
        check('access code with %d bit errors %s' % (
            errs, 'found (threshold 2, as r6)' if want else 'not found'),
            (got == [good[0]]) == want)

    # random garbage and noise: no frames, bounded memory
    d = P.Deframer()
    noise = rng.integers(0, 2, 2_000_000).astype(np.uint8)
    t0 = time.process_time()
    out = []
    for i in range(0, len(noise), 4096):
        out += d.feed(noise[i:i + 4096])
    dt = time.process_time() - t0
    check('2 Mbit of random bits: no frame, bounded memory', not out
          and d.held_bits < 20000, '%d held' % d.held_bits)
    check('throughput far above four channels at 250 kbit/s (r6.1 decodes '
          'in numpy, 4096 bits per call)', 2e6 / max(dt, 1e-6) > 5e6,
          '%.1f Mbit/s per core' % (2e6 / max(dt, 1e-6) / 1e6))

    # ------------------------------------------------------------ PRBS fill
    section('Transmit framer and PRBS-15 filler')
    prbs = np.concatenate([P.PRBS, P.PRBS[:63]])
    win = np.lib.stride_tricks.sliding_window_view(prbs, 64)
    dist = np.count_nonzero(win != P.AC_BITS, axis=1)
    pre = np.unpackbits(np.frombuffer(P.PRE_PATTERN * 32, np.uint8))
    joints = []
    for a, b in ((P.PRBS[-63:], pre[:63]), (pre[-63:], P.PRBS[:63])):
        j = np.concatenate([a, b])
        joints.append(np.count_nonzero(
            np.lib.stride_tricks.sliding_window_view(j, 64) != P.AC_BITS,
            axis=1).min())
    check('no 64-bit window of the PRBS-15 filler (or its joins with a '
          'preamble/postamble) is within 2 bits of the access code',
          dist.min() > P.CODE_THRESHOLD and min(joints) > P.CODE_THRESHOLD,
          'minimum distance %d (joins %s)' % (dist.min(), joints))
    check('PRBS-15 has its full period and balance',
          len(P.PRBS) == 32767 and int(P.PRBS.sum()) == 16384)

    tx = P.TxFramer(sym_rate=250e3, max_backlog=0.5)
    sent = [payload(300) for _ in range(5)]
    for f in sent:
        tx.push(f)
    sym = tx.next_symbols(30000)
    check('symbols are +-1 (BPSK)', set(np.unique(sym.real)) == {-1.0, 1.0}
          and not np.any(sym.imag))
    got = P.Deframer().feed(P.diff_decode(sym))
    check('frames -> differential BPSK -> reference receiver -> deframer: '
          'all 5 back', got == sent, '%d of 5' % len(got))
    check('the rest is filler: no phantom frames', len(got) == 5)
    tx2 = P.TxFramer(sym_rate=250e3, max_backlog=0.5)
    acc = sum(tx2.push(payload(600)) for _ in range(200))
    check('the transmit backlog is bounded (0.5 s): pushes beyond it are '
          'refused, counted, never queued', acc < 200 and tx2.backlog() <= 0.5
          and tx2.stats['dropped'] == 200 - acc,
          '%d accepted, backlog %.3f s' % (acc, tx2.backlog()))
    check('frames longer than 600 bytes are refused',
          not tx2.push(payload(601)) and tx2.stats['rejected'] == 1)
    a = P.TxFramer()
    s1 = np.concatenate([a.next_symbols(1000) for _ in range(7)])
    b = P.TxFramer()
    s2 = b.next_symbols(7000)
    check('output is continuous across calls (phase and filler position)',
          np.array_equal(s1, s2))

    # ------------------------------------------------------------ plan
    section('One-sided channel plan')
    plan = P.channel_plan(6e6, 450e3, 600e3, 4, 250e3, 4)
    rep = P.plan_report(plan)
    check('channels at +450/+1050/+1650/+2250 kHz',
          plan['offsets'] == [450e3, 1050e3, 1650e3, 2250e3])
    check('no channel\'s IQ image lands on any channel (r6.0.1: 0<->3, '
          '1<->2)', rep['image_free'], repr(rep['image_hits']))
    old = [(k - 1.5) * 600e3 for k in range(4)]
    old_hits = [[j for j, g in enumerate(old) if abs(-f - g) < 337.5e3]
                for f in old]
    check('(the r6.0.1 two-sided plan: every image on another channel)',
          old_hits == [[3], [2], [1], [0]], repr(old_hits))
    check('DC/LO leakage outside every channel, band edge inside Nyquist',
          rep['dc_guard'] > 200e3 and rep['inside_nyquist'],
          'dc guard %.0f kHz, edge %.0f%% of fs/2' % (
              rep['dc_guard'] / 1e3, 100 * rep['edge_fraction']))
    check('decimation 6 to 1 MS/s, 4 samples/symbol, TX interpolation 24',
          plan['decim'] == 6 and plan['out_rate'] == 1e6
          and plan['tx_interp'] == 24)

    # ------------------------------------------------------------ CFO / AFC
    section('Carrier offset estimate and AFC (replace four FLL band-edge '
            'blocks)')
    taps = P.rrc_taps(4, 0.35, 16)

    def bpsk_at(cfo, n=4096, snr_db=20.0, amp=1.0):
        b = rng.integers(0, 2, n // 4 + 40) * 2.0 - 1.0
        up = np.zeros(len(b) * 4, np.complex64)
        up[::4] = b
        x = np.convolve(up, taps)[:n] * amp
        t = np.arange(n) / 1e6
        x = x * np.exp(2j * np.pi * cfo * t)
        p = np.mean(np.abs(x) ** 2)
        nz = (rng.normal(size=n) + 1j * rng.normal(size=n)) * np.sqrt(
            p / 2 * 10 ** (-snr_db / 10))
        return (x + nz).astype(np.complex64)

    errs = []
    for cfo in (0.0, 3e3, -11e3, 23e3, -23e3, 45e3, -55e3):
        est, ratio = P.estimate_cfo(bpsk_at(cfo), 1e6)
        errs.append(None if est is None else abs(est - cfo))
    check('offsets 0..+-55 kHz (two +-25 ppm crystals at 910 MHz: up to '
          '45 kHz) estimated within 50 Hz', all(e is not None and e < 50
                                                for e in errs), repr(errs))
    est, ratio = P.estimate_cfo((rng.normal(size=4096) + 1j * rng.normal(
        size=4096)).astype(np.complex64), 1e6)
    check('noise alone gives no estimate', est is None,
          'ratio %.1f dB' % ratio)
    afc = P.Afc(1e6)
    incs = []
    for i in range(12):        # the rotator removes what the AFC corrects
        incs.append(afc.update(0.1 * i, bpsk_at(20e3 - afc.correction)))
    moved = [x for x in incs if x is not None]
    check('AFC moves only on two agreeing estimates, then holds off',
          incs[0] is None and len(moved) >= 1,
          'updates at %s' % [i for i, x in enumerate(incs) if x is not None])
    check('AFC correction converges on the offset',
          abs(afc.correction - 20e3) < 100 and moved
          and abs(moved[0] + 2 * math.pi * 20e3 / 1e6) < 1e-3,
          'correction %.0f Hz' % afc.correction)
    afc2 = P.Afc(1e6)
    r = [afc2.update(0.5 * i, bpsk_at(80e3)) for i in range(6)]
    check('an estimate outside the +-6 % limit is refused, not clipped',
          all(x is None for x in r) and afc2.correction == 0.0)
    noise = (rng.normal(size=4096) + 1j * rng.normal(size=4096)
             ).astype(np.complex64)
    afc.update(5.0, noise)
    out = afc.update(20.0, noise)
    check('a channel silent for 10 s drops its correction (the next '
          'transmitter has its own crystal)', out == 0.0 * out if out is not
          None else False, 'correction %.0f' % afc.correction)

    # ------------------------------------------------------------ measures
    section('Measurements')
    for target in (6.0, 15.0, 25.0):
        s = np.sign(rng.normal(size=4000)).astype(np.complex64)
        n = (rng.normal(size=4000) + 1j * rng.normal(size=4000)) / np.sqrt(2)
        est, evm = P.evm_snr(s + n * 10 ** (-target / 20))
        check('EVM SNR tracks %.0f dB' % target, abs(est - target) < 1.0,
              '%.2f dB' % est)
    est, _ = P.evm_snr(np.zeros(512, np.complex64))
    check('silence gives no SNR', est is None)
    on = bpsk_at(0.0, amp=0.1, snr_db=60)
    adj = on * np.exp(2j * np.pi * 600e3 / 1e6 * np.arange(4096)).astype(
        np.complex64)
    lv_on, lv_adj = P.inband_level_db(on), P.inband_level_db(adj)
    check('in-band level ignores a signal one channel away (> 40 dB down)',
          lv_on - lv_adj > 40, '%.1f vs %.1f dBFS' % (lv_on, lv_adj))
    x = (0.05 * (rng.normal(size=8192) + 1j * rng.normal(size=8192))
         ).astype(np.complex64)
    hot = x * 40
    hot = (np.clip(hot.real, -1, 1) + 1j * np.clip(hot.imag, -1, 1)
           ).astype(np.complex64)
    a, b = P.adc_stats(x), P.adc_stats(hot)
    check('ADC stats: a clean snapshot has no clipping, an overdriven one '
          'reports its clipped fraction and 0 dBFS',
          a['clip'] == 0 and b['clip'] > 0.3 and b['peak_dbfs'] > -0.1,
          '%r / %r' % (a['clip'], b['clip']))

    section('gr-iio O/U marks on stderr')
    # one mark per event (gr-iio prints at most one a second per direction)
    cases = [('O', 1, 0), ('UUU', 0, 3), ('OOOO\n', 4, 0),
             ('[link] FAULT: receiver OK\n', 0, 0), ('USB OK\n', 0, 0),
             ('xO', 0, 0), ('O[link] hi\nU', 1, 1), ('\nOU\n', 1, 1)]
    ok = True
    for text, wo, wu in cases:
        o, u, _ = P.count_ou(text, {'prev': '\n'})
        ok &= (o, u) == (wo, wu)
    check('O/U marks are counted, one per event; letters inside words are '
          'not', ok)
    st = {'prev': '\n'}
    o1, u1, st = P.count_ou('hello W', st)
    o2, u2, st = P.count_ou('O', st)
    check('state carries across chunk boundaries ("...W" + "O" is a word)',
          o2 == 0)
    st, tot = {'prev': '\n'}, 0
    for chunk in ('O', 'O', 'O', 'O[link] x\n', 'U', 'U\n'):
        o, u, st = P.count_ou(chunk, st)
        tot += o + u
    check('one mark a second, each read on its own ("O" "O" "O" ... as '
          'gr-iio prints them): every one counted', tot == 6, '%d of 6' % tot)

    # ------------------------------------------------------------ gain law
    section('Gain staging law (bpsk_gain)')
    g = G.GainControl(rx_gain=30, tx_atten=50)
    t = 10.0
    g.observe_adc(t, 0.0, 0.40, tx_on=True)
    out = g.step(t)
    check('clipping (Pasindu: 61.8 % at 0 dBFS) drops RX gain 12 dB at once',
          out and out[0][:2] == ('rx', 18), repr(out))
    g.observe_adc(t + 0.1, 0.0, 0.4)
    check('readings within 0.25 s of a change are ignored, at most one '
          'change per 0.5 s', g.step(t + 0.3) == [])
    seq = []
    lvl = {'rx': 18}
    for i in range(40):
        now = t + 1.0 + 0.5 * i
        peak = -6.0 + (g.rx_gain - 30) + 12      # own signal follows gain
        clip = 0.3 if peak > 0 else 0.0
        g.observe_adc(now, min(peak, 0.0), clip, tx_on=True)
        seq += g.step(now)
    peak = -6.0 + (g.rx_gain - 30) + 12
    check('the loop settles inside the hysteresis band (-20..-9 dBFS) and '
          'stops changing', -20 <= peak <= -9 and len(seq) <= 4,
          'RX gain %d, peak %.1f dBFS, %d changes' % (g.rx_gain, peak,
                                                      len(seq)))
    g = G.GainControl(rx_gain=20, tx_atten=50)
    for i in range(20):
        g.observe_adc(i * 0.5, -40.0, 0.0, tx_on=False)
        g.step(i * 0.5)
    check('no RX gain increase on readings taken with the transmitter off',
          g.rx_gain == 20, 'rx %d' % g.rx_gain)
    for i in range(20, 23):
        g.observe_adc(i * 0.5, -40.0, 0.0, tx_on=True)
        g.step(i * 0.5)
    check('... nor on headroom lasting less than 2 s (a channel check\'s '
          'gap reaching the ADC late)', g.rx_gain == 20, 'rx %d' % g.rx_gain)
    for i in range(23, 40):
        g.observe_adc(i * 0.5, -40.0, 0.0, tx_on=True)
        g.step(i * 0.5)
    check('... but up 2 dB per 2 s of steady headroom with it on',
          24 <= g.rx_gain <= 20 + 2 * 4, 'rx %d after 9.5 s' % g.rx_gain)
    g = G.GainControl(rx_gain=0, tx_atten=50)
    g.observe_adc(1.0, 0.0, 0.2)
    out = g.step(1.0)
    check('clipping at minimum RX gain raises TX attenuation 3 dB',
          out == [('tx', 53, out[0][2])] if out else False, repr(out))
    g = G.GainControl(rx_gain=70, tx_atten=89)
    g.observe_adc(1.0, -60.0, 0.0)
    g.step(1.0)
    g2 = G.GainControl(rx_gain=0, tx_atten=89)
    g2.observe_adc(1.0, 0.0, 0.5)
    check('bounds hold (RX 0..70, TX 10..89)', g.rx_gain == 70
          and g2.step(1.0) == [] and g2.tx_atten == 89)
    g = G.GainControl(rx_gain=30, tx_atten=50)
    g.observe_adc(1.0, -15.0, 0.0)
    out = g.step(1.0, [{'snr': 9.0}, {'snr': 30.0}])
    check('TX loop: weakest neighbour at 9 dB -> 2 dB more power',
          out and out[0][:2] == ('tx', 48), repr(out))
    g.observe_adc(4.0, -15.0, 0.0)
    out = g.step(4.0, [{'snr': 9.0, 'impaired': True}, {'snr': 30.0}])
    check('... an impaired (overloaded) neighbour is left out: everyone '
          'else above 23 dB -> 1 dB less power',
          out and out[0][:2] == ('tx', 49), repr(out))
    g.observe_adc(7.0, -15.0, 0.0)
    out = g.step(7.0, [{'snr': None}])
    check('a neighbour that does not list this node counts as 0 dB',
          out and out[0][0] == 'tx' and out[0][1] < 49, repr(out))
    g.observe_adc(10.0, -15.0, 0.0)
    check('between 13 and 23 dB nothing changes (hysteresis)',
          g.step(10.0, [{'snr': 18.0}]) == [])
    g = G.GainControl(rx_gain=30, tx_atten=50, auto=False)
    g.observe_adc(1.0, 0.0, 0.5)
    check('manual override: auto off freezes both', g.step(1.0) == [])
    check('set_manual moves either one, within bounds',
          g.set_manual(rx_gain=99, tx_atten=5) and (g.rx_gain, g.tx_atten) ==
          (70, 10))

    print('\n%s' % ('all phy checks passed' if not FAILURES
                    else 'FAILED: ' + ', '.join(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == '__main__':
    sys.exit(main())
