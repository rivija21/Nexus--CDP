"""r7 PHY: the numpy DBPSK burst modem (bpsk_phy).

    python3 test_phy.py

Every case runs the real modulator into a channel model (delay, carrier and
clock offset, AWGN, other transmitters) and the real receiver, and checks
what the MAC relies on: frames decode, the sync index is exact, and the SNR
reading is honest.
"""

import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bpsk_phy as P                                             # noqa: E402

FAILURES = []
RNG = np.random.default_rng(11)
FS = 1e6


def check(label, cond, detail=''):
    print('  [%s] %s%s' % ('PASS' if cond else 'FAIL', label,
                           (' - ' + detail) if detail else ''))
    if not cond:
        FAILURES.append(label)


def frames_of(n, size):
    return [RNG.integers(0, 256, size, dtype=np.uint8).tobytes()
            for _ in range(n)]


def channel(w, delay=0, cfo=0.0, phase=0.0, ppm=0.0, snr_db=None,
            tail=4000):
    if ppm:
        n = np.arange(len(w))
        t = n * (1 + ppm * 1e-6)
        w = (np.interp(t, n, w.real) + 1j * np.interp(t, n, w.imag)
             ).astype(np.complex64)
    x = np.concatenate((np.zeros(delay, np.complex64), w,
                        np.zeros(tail, np.complex64)))
    x = x * np.exp(1j * (2 * np.pi * cfo * np.arange(len(x)) / FS + phase))
    if snr_db is not None:
        es = np.mean(np.abs(w) ** 2) * P.SPS       # energy per symbol
        n0 = es / 10 ** (snr_db / 10.0)
        x = x + np.sqrt(n0 / 2) * (RNG.standard_normal(len(x))
                                   + 1j * RNG.standard_normal(len(x)))
    return x.astype(np.complex64)


def receive(x, i0=1000, block=8192):
    y = P.matched_filter(x)
    rx = P.Receiver()
    out = []
    for i in range(0, len(y), block):
        out += rx.process(y[i:i + block], i0 + i)
    return out, rx


def burst(frames, pre=8):
    stream, offs = P.burst_layout(frames, preamble=pre)
    return P.Modulator.burst(stream), offs


def t_clean():
    print('\n--- clean channel: decode and sync index')
    frames = frames_of(12, 300)
    w, offs = burst(frames)
    out, rx = receive(channel(w, delay=17, cfo=1500, phase=1.1, snr_db=25))
    got = {f['data']: f for f in out}
    check('all 12 frames decoded', all(f in got for f in frames),
          '%d/12' % len(got))
    want = [1000 + 17 + 32 * o + 2 * P.DELAY for o in offs]
    err = [got[f]['sync'] - s for f, s in zip(frames, want) if f in got]
    check('sync index exact (TX delay + RX matched filter)',
          err and max(abs(e) for e in err) == 0, str(sorted(set(err))))
    snr = np.mean([f['snr_db'] for f in out])
    check('SNR estimate within 1 dB at 25 dB', abs(snr - 25) < 1.0,
          '%.2f dB' % snr)
    cfo = np.mean([f['cfo_hz'] for f in out])
    check('carrier offset estimate within 100 Hz', abs(cfo - 1500) < 100,
          '%.0f Hz' % cfo)
    check('no false frames', len(out) == 12, '%d' % len(out))


def t_snr_sweep():
    print('\n--- AWGN: frame success and SNR estimate (300-byte frames)')
    rows = []
    for snr in (16, 12, 10, 9):
        frames = frames_of(40, 300)
        w, _ = burst(frames)
        out, _ = receive(channel(w, delay=5, cfo=800, snr_db=snr))
        ok = sum(1 for f in out if f['data'] in set(frames))
        est = np.mean([f['snr_db'] for f in out]) if out else float('nan')
        rows.append((snr, ok, est))
        print('     Es/N0 %2d dB: %2d/40 frames  estimate %.1f dB'
              % (snr, ok, est))
    check('>= 39/40 frames at 12 dB and above',
          all(r[1] >= 39 for r in rows if r[0] >= 12))
    check('estimate within 1.5 dB from 9 to 16 dB',
          all(abs(r[2] - r[0]) < 1.5 for r in rows if not np.isnan(r[2])))
    # Differential detection: BER = 0.5 exp(-Eb/N0); 2400 bits per frame.
    p10 = 1 - (1 - 0.5 * np.exp(-10 ** (10 / 10.0))) ** 2400
    print('     theory (differential detection) at 10 dB: %.1f%% frame loss'
          % (100 * p10))
    check('10 dB within a few frames of theory',
          rows[2][1] >= 40 * (1 - p10) - 4, '%d/40' % rows[2][1])


def t_offsets():
    print('\n--- carrier offset, clock offset, long frames')
    for cfo in (-20000, 20000):
        frames = frames_of(10, 300)
        w, _ = burst(frames)
        out, _ = receive(channel(w, delay=3, cfo=cfo, snr_db=18))
        ok = sum(1 for f in out if f['data'] in set(frames))
        check('CFO %+d Hz: 10/10' % cfo, ok == 10, '%d' % ok)
    for ppm in (25, -50):
        frames = frames_of(4, 1000)
        w, _ = burst(frames)
        out, _ = receive(channel(w, delay=3, cfo=1000, ppm=ppm, snr_db=18))
        ok = sum(1 for f in out if f['data'] in set(frames))
        check('%+d ppm clock, 1000-byte frames: 4/4 (timing tracked)' % ppm,
              ok == 4, '%d' % ok)


def t_blocks():
    print('\n--- block boundaries and frames spanning blocks')
    frames = frames_of(20, 700)
    w, offs = burst(frames)
    x = channel(w, delay=9, snr_db=20)
    for block in (517, 4096, 65536):
        out, _ = receive(x, block=block)
        ok = sum(1 for f in out if f['data'] in set(frames))
        check('block size %d: 20/20, no duplicates' % block,
              ok == 20 and len(out) == 20, '%d of %d' % (ok, len(out)))


def t_stream():
    print('\n--- continuous downlink: StreamModulator == one burst')
    frames = frames_of(6, 120)
    stream, offs = P.burst_layout(frames, preamble=4)
    whole = P.Modulator.burst(stream)
    sm = P.StreamModulator()
    parts = []
    cuts = [0, 7, 100, 101, 260, len(stream)]
    for a, b in zip(cuts[:-1], cuts[1:]):
        parts.append(sm.push(stream[a:b]))
    joined = np.concatenate(parts)
    n = len(joined)
    check('identical samples however the bytes are cut',
          np.allclose(joined, whole[:n], atol=1e-5))
    check('32 samples per byte', n == 32 * len(stream))


def t_collision():
    print('\n--- overlapping bursts: capture and loss')
    a = frames_of(1, 200)
    b = frames_of(1, 200)
    wa, _ = burst(a)
    wb, _ = burst(b)
    n = max(len(wa), len(wb)) + 3000
    xa = np.zeros(n, np.complex64)
    xb = np.zeros(n, np.complex64)
    xa[100:100 + len(wa)] = wa
    xb[900:900 + len(wb)] = wb
    rot = np.exp(1j * 2 * np.pi * 3000 * np.arange(n) / FS)
    for sir, expect in ((20, 'a'), (0, None)):
        x = xa + xb * rot * 10 ** (-sir / 20.0)
        x = channel(x, snr_db=30)
        out, _ = receive(x)
        got = {f['data'] for f in out}
        if expect == 'a':
            check('SIR 20 dB: the stronger burst decodes', a[0] in got)
        else:
            check('SIR 0 dB: neither is mis-decoded (CRC is the judge above)',
                  len(out) <= 2)


def t_noise_only():
    print('\n--- noise and filler only: no frames')
    x = (RNG.standard_normal(2_000_000) + 1j * RNG.standard_normal(2_000_000)
         ).astype(np.complex64) * 0.1
    out, rx = receive(x)
    check('2 s of noise: no frame', len(out) == 0, '%d' % len(out))
    sm = P.StreamModulator()
    filler = sm.push(P.fill(40000))
    out, rx = receive(channel(filler, snr_db=20))
    check('1.3 s of filler: no frame', len(out) == 0, '%d' % len(out))


def t_speed():
    print('\n--- speed')
    import time
    frames = frames_of(60, 300)
    w, _ = burst(frames)
    x = channel(w, snr_db=20, tail=200000)
    y = P.matched_filter(x)
    rx = P.Receiver()
    t0 = time.time()
    for i in range(0, len(y), 32768):
        rx.process(y[i:i + 32768], i)
    dt = time.time() - t0
    rate = len(y) / dt / 1e6
    print('     %.1f MS/s receive (%.0fx real time at 1 MS/s)' % (rate, rate))
    check('receiver faster than 2x real time', rate > 2.0)


def main():
    for t in (t_clean, t_snr_sweep, t_offsets, t_blocks, t_stream,
              t_collision, t_noise_only, t_speed):
        t()
    print()
    if FAILURES:
        print('FAILED: %d check(s): %s' % (len(FAILURES), FAILURES))
        return 1
    print('all PHY checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
