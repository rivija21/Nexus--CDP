"""r7 end to end through the real modem (bpsk_sample_sim).

    python3 test_r7_samples.py

Four complete nodes exchange complex samples: TX and RX buffer latency per
node, carrier offsets, AWGN, a weak member, collisions as physical sums.
Nothing between the MAC's bytes and the modem's samples is abstracted.
About two minutes.
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bpsk_sample_sim as S                                     # noqa: E402

FAILURES = []


def check(label, cond, detail=''):
    print('  [%s] %s%s' % ('PASS' if cond else 'FAIL', label,
                           (' - ' + detail) if detail else ''))
    if not cond:
        FAILURES.append(label)


def state(app, mid):
    return app._by_mid.get(mid, {}).get('state')


def inbox(app):
    return [m['text'] for m in app.messages
            if m['dir'] == 'in' and m['kind'] == 'text']


def rx_file(sim, a, name):
    path = os.path.join(sim.root, 'store_%d' % a, 'rx_' + name)
    if not os.path.exists(path):
        return None
    with open(path, 'rb') as fh:
        return fh.read()


def main():
    t_start = time.time()
    # Amplitude ~0.7 peak; noise 0.03 per sample -> Es/N0 about 20 dB.
    sim = S.SampleSim(security=True, seed=4, noise=0.03)
    addrs = [1, 2, 3, 4]
    sim.provision(addrs)
    cfos = {1: 0.0, 2: 4200.0, 3: -3100.0, 4: 1500.0}
    for a in addrs:
        sim.add(a, cfo_hz=cfos[a])
    # Node 4 is 14 dB further away than the others, in both directions.
    for a in addrs:
        if a != 4:
            sim.gain[(4, a)] = sim.gain[(a, 4)] = 0.2

    print('\n--- start-up through the modem')
    ok = sim.run(15, until=lambda s: s.joined())
    hubs = sim.hubs()
    check('one hub (%s), all joined at %.1f s' % (hubs, sim.now),
          ok and len(hubs) == 1)
    sim.run(6)
    hub = sim.mac(hubs[0])
    errs = {a: m.err for a, m in hub.members.items()}
    check('ranged to within +-4 samples (%s)' % errs,
          errs and all(e is not None and abs(e) <= 4 for e in errs.values()))
    snr = {a: m.snr for a, m in hub.members.items()}
    print('     uplink SNR at the hub: %s' % {a: round(v or 0, 1)
                                                for a, v in snr.items()})
    check('the weak member is heard ~14 dB lower and still admitted',
          4 in hub.members and snr[4] is not None
          and min(snr[a] for a in snr if a != 4) - snr[4] > 8)

    print('\n--- traffic')
    m = [a for a in addrs if a != hubs[0]]
    a, b, c = m
    mid = sim.app(a).submit('text', text='sample-level hello', to=b)
    mid2 = sim.app(c).submit('text', text='weak node speaks', to=hubs[0])
    sim.run(15, until=lambda s: state(s.app(a), mid) == 'delivered'
            and state(s.app(c), mid2) == 'delivered')
    check('member -> member chat', 'sample-level hello' in inbox(sim.app(b)))
    check('weak member -> hub chat', 'weak node speaks'
          in inbox(sim.app(hubs[0])))
    data = os.urandom(40000)
    fm = sim.app(b).submit('file', name='s.bin', mime='', data=data, to=c)
    t0 = sim.now
    sim.run(40, until=lambda s: state(s.app(b), fm) in ('delivered', 'failed'))
    check('40 kB member -> weak member intact (%.1f s)' % (sim.now - t0),
          rx_file(sim, c, 's.bin') == data)
    st = sim.radios[b].node.rx.stats
    print('     receiver at %d: %s' % (b, st))

    print('\n--- the hub disappears')
    old = hubs[0]
    sim.radios[old].alive = False
    t0 = sim.now
    ok = sim.run(15, until=lambda s: s.joined())
    new = sim.hubs()
    check('hub %d gone: %s took over, all rejoined in %.1f s'
          % (old, new, sim.now - t0), ok and len(new) == 1 and new != [old])
    rest = [x for x in addrs if x not in (old,) and x not in new]
    mid = sim.app(rest[0]).submit('text', text='new hub, same chat',
                                  to=rest[-1] if len(rest) > 1 else new[0])
    dest = rest[-1] if len(rest) > 1 else new[0]
    sim.run(15, until=lambda s: state(s.app(rest[0]), mid) == 'delivered')
    check('traffic through the new hub', 'new hub, same chat'
          in inbox(sim.app(dest)))
    print('\n(%.0f s wall)' % (time.time() - t_start))
    if FAILURES:
        print('FAILED: %d check(s):' % len(FAILURES))
        for f in FAILURES:
            print('   ' + f)
        return 1
    print('all sample-level checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
