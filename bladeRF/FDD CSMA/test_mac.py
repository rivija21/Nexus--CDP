"""Unit tests for the r6b CSMA/CA state machine.

Simulated time, no radio and no GNU Radio import, so every case is
deterministic and repeatable. What is checked here is the part of r6b most
likely to be wrong: the contention rules. Whether gr-iio's burst mode behaves
on real hardware is a separate question these tests cannot answer.

    python3 test_mac.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bpsk_link as L                                           # noqa: E402
import bpsk_mac as M                                            # noqa: E402

TICK = 0.001
FAILURES = []


def check(label, condition, detail=''):
    print('  [%s] %s%s' % ('PASS' if condition else 'FAIL', label,
                           (' - ' + detail) if detail else ''))
    if not condition:
        FAILURES.append(label)


def data_frame(seq=1, retry=False):
    return L.build(L.FT_DATA, 2, 1, seq, 0, b'x' * 256,
                   L.FLAG_RETRY if retry else 0, seq)


def ack_frame(seq=1):
    return L.build(L.FT_ACK, 2, 1, 0, seq, b'', 0, seq)


def mac(**kw):
    kw.setdefault('seed', 7)
    return M.MacState(**kw)


def run(m, seconds, t0=0.0, busy=None):
    """Drive the MAC for a while; returns (release_times, frames)."""
    times, frames = [], []
    t = t0
    end = t0 + seconds
    while t < end:
        t = round(t + TICK, 6)
        if busy is not None:
            m.set_busy(busy(t), t)
        for f in m.on_tick(t):
            times.append(t)
            frames.append(f)
    return times, frames


def main():
    slot = 0.003

    print('\n  contention basics')
    m = mac(slot=slot)
    m.enqueue(data_frame(), 0.0)
    times, _ = run(m, 0.20)
    check('a data frame is eventually transmitted', len(times) == 1,
          '%d releases' % len(times))
    if times:
        difs = 2 * slot
        earliest = difs
        latest = difs + (m.cw_min - 1) * slot + 3 * TICK
        check('it waits at least DIFS', times[0] >= earliest - 1e-9,
              't=%.3f s, DIFS=%.3f s' % (times[0], difs))
        check('it waits no longer than DIFS + CWmin slots',
              times[0] <= latest, 't=%.3f s, bound=%.3f s' % (times[0], latest))

    print('\n  a busy channel defers')
    m = mac(slot=slot)
    m.enqueue(data_frame(), 0.0)
    times, _ = run(m, 0.20, busy=lambda t: True)
    check('nothing is transmitted while the channel is busy', not times,
          '%d releases' % len(times))
    check('deferrals were counted', m.stats['deferrals'] > 0,
          '%d' % m.stats['deferrals'])

    print('\n  backoff freezes rather than redraws')
    m = mac(slot=slot)
    m.enqueue(data_frame(), 0.0)
    run(m, 0.012)                                  # past DIFS, counting down
    mid = m.backoff
    m.set_busy(True, 0.012)
    run(m, 0.050, t0=0.012, busy=lambda t: True)
    frozen = m.backoff
    check('the countdown is retained across a busy period',
          mid is not None and frozen == mid,
          'before=%s during=%s' % (mid, frozen))
    m.set_busy(False, 0.062)
    times, _ = run(m, 0.20, t0=0.062)
    check('it resumes and transmits once the channel clears', len(times) == 1)

    print('\n  acknowledgements take priority')
    m = mac(slot=slot)
    m.enqueue(data_frame(), 0.0)                   # queued first
    m.enqueue(ack_frame(), 0.0)                    # but this must go first
    times, frames = run(m, 0.30)
    first = L.peek(frames[0])[0] if frames else None
    check('the acknowledgement is transmitted first', first == L.FT_ACK,
          'first frame type=%s' % first)
    check('it needs only SIFS, not DIFS', times and times[0] <= slot + 3 * TICK,
          't=%.4f s, SIFS=%.4f s' % (times[0], slot) if times else 'none')
    check('the data frame follows', len(frames) == 2, '%d frames' % len(frames))

    print('\n  virtual carrier sense')
    m = mac(slot=slot)
    m.note_rx(data_frame(), 0.0)                   # someone else is being ACKed
    m.enqueue(data_frame(), 0.0)
    nav = m.nav_until
    times, _ = run(m, 0.010)
    check('NAV is set from an observed data frame', nav > 0.0,
          'until %.4f s' % nav)
    check('nothing is sent inside the NAV window even though sensing is idle',
          not [t for t in times if t < nav], 'nav=%.4f s' % nav)
    times, _ = run(m, 0.30, t0=0.010)
    check('it transmits once the NAV expires', len(times) == 1)

    print('\n  contention window')
    m = mac(slot=slot)
    check('starts at CWmin', m.cw == m.cw_min, str(m.cw))
    m.enqueue(data_frame(retry=True), 0.0)
    doubled = m.cw
    check('a retry doubles the window', doubled == 2 * m.cw_min,
          '%d -> %d' % (m.cw_min, doubled))
    m.enqueue(data_frame(retry=True), 0.0)
    check('a second retry doubles it again', m.cw == 4 * m.cw_min, str(m.cw))
    for _ in range(12):
        m.enqueue(data_frame(retry=True), 0.0)
    check('it is capped at CWmax', m.cw == m.cw_max, str(m.cw))
    m2 = mac(slot=slot)
    m2.cw = 64
    m2.enqueue(data_frame(retry=False), 0.0)
    run(m2, 0.40)
    check('a clean transmission resets the window', m2.cw == m2.cw_min,
          '64 -> %d' % m2.cw)

    print('\n  self-interference guard')
    m = mac(slot=slot)
    m.enqueue(data_frame(), 0.0)
    m.enqueue(data_frame(seq=2), 0.0)
    times, _ = run(m, 0.60)
    check('two frames are not transmitted on top of each other',
          len(times) == 2 and (times[1] - times[0]) >= m._airtime(269) - 1e-6,
          'gap=%.1f ms, airtime=%.1f ms'
          % ((times[1] - times[0]) * 1e3, m._airtime(269) * 1e3) if len(times) == 2
          else '%d releases' % len(times))

    print('\n  an idle station stays quiet')
    m = mac(slot=slot)
    times, _ = run(m, 0.20)
    check('nothing is transmitted with an empty queue', not times)
    check('no backoff is left running', m.backoff is None, str(m.backoff))

    print('\n  fairness between two contending stations')
    a, b = mac(slot=slot, seed=1), mac(slot=slot, seed=2)
    for i in range(6):
        a.enqueue(data_frame(seq=i), 0.0)
        b.enqueue(data_frame(seq=i), 0.0)
    ta, tb = 0, 0
    t = 0.0
    # Each station senses the other's transmission as a busy channel.
    while t < 4.0:
        t = round(t + TICK, 6)
        a.set_busy(t < b.tx_until, t)
        b.set_busy(t < a.tx_until, t)
        ta += len(a.on_tick(t))
        tb += len(b.on_tick(t))
    check('both stations got the channel', ta > 0 and tb > 0,
          'A=%d B=%d frames' % (ta, tb))
    check('neither starved the other', min(ta, tb) >= 0.5 * max(ta, tb),
          'A=%d B=%d' % (ta, tb))

    print('\n  telemetry')
    snap = a.snapshot(t)
    check('snapshot is renderable',
          set(['state', 'busy', 'backoff', 'cw', 'queued', 'utilisation'])
          <= set(snap), ','.join(sorted(snap)))
    check('utilisation is a fraction', 0.0 <= snap['utilisation'] <= 1.0,
          '%.3f' % snap['utilisation'])

    print('\n%s' % ('all MAC checks passed' if not FAILURES
                    else 'FAILED: ' + ', '.join(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == '__main__':
    sys.exit(main())
