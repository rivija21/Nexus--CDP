"""r7 MAC, frame level: membership, ranging, scheduling, failover.

    python3 test_mac.py

Complete nodes (app + encryption + link + MAC) in bpsk_mac_sim: per-node
sample clocks with offsets and drift, buffer latencies, FDD tuning, air time
and collisions. No modem - see test_r7_samples.py for that.
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bpsk_mac as M                                            # noqa: E402
import bpsk_mac_sim as S                                        # noqa: E402

FAILURES = []


def check(label, cond, detail=''):
    print('  [%s] %s%s' % ('PASS' if cond else 'FAIL', label,
                           (' - ' + detail) if detail else ''))
    if not cond:
        FAILURES.append(label)


def network(n, seed=1, security=True, loss=0.0, roles=None, **kw):
    sim = S.FrameSim(security=security, seed=seed, loss=loss, **kw)
    addrs = list(range(1, n + 1))
    sim.provision(addrs)
    for a in addrs:
        sim.add(a, role=(roles or {}).get(a, 'auto'))
    return sim, addrs


def state(app, mid):
    return app._by_mid.get(mid, {}).get('state')


def inbox(app, src=None):
    return [m['text'] for m in app.messages
            if m['dir'] == 'in' and m['kind'] == 'text'
            and (src is None or m['addr'] == src)]


def send_file(sim, a, b, size, name):
    data = os.urandom(size)
    mid = sim.app(a).submit('file', name=name, mime='', data=data, to=b)
    return mid, data


def rx_file(sim, a, name):
    path = os.path.join(sim.root, 'store_%d' % a, 'rx_' + name)
    if not os.path.exists(path):
        return None
    with open(path, 'rb') as fh:
        return fh.read()


# ---------------------------------------------------------------------------
def t_join():
    print('\n--- start-up: one hub, everyone admitted, ranging converges')
    for n in (4, 12):
        sim, addrs = network(n, seed=n)
        ok = sim.run(30, until=lambda s: s.joined())
        hubs = sim.hubs()
        check('%d nodes: exactly one hub (%s), all joined by %.1f s'
              % (n, hubs, sim.now), ok and len(hubs) == 1)
        check('%d nodes: the hub is the lowest address' % n, hubs == [1])
        sim.run(8)                  # past the first maintenance grants
        hub = sim.mac(hubs[0])
        errs = [m.err for m in hub.members.values() if m.err is not None]
        check('%d nodes: every member within +-64 samples of its slot '
              '(guard %d)' % (n, M.MacConfig().guard),
              errs and max(abs(e) for e in errs) <= 64,
              'worst %s' % (max(abs(e) for e in errs) if errs else None))
        online = sim.app(n).online
        check('%d nodes: a member sees all %d others online' % (n, n - 1),
              len(online) == n - 1, str(sorted(online)))


def progress_rate(sim, a, mid, size, limit=150):
    """Run until mid is done. Returns (total s, steady kb/s from 10% to 90%)."""
    t0 = sim.now
    marks = {}

    def watch(s):
        msg = s.app(a)._by_mid.get(mid, {})
        prog = msg.get('progress') or {}
        if prog.get('total'):
            frac = prog['done'] / float(prog['total'])
            for q in (0.1, 0.9):
                if frac >= q and q not in marks:
                    marks[q] = s.now
        return msg.get('state') in ('delivered', 'failed')
    sim.run(limit, until=watch, check_every=0.02)
    steady = None
    if 0.1 in marks and 0.9 in marks and marks[0.9] > marks[0.1]:
        steady = 0.8 * size * 8 / 1000.0 / (marks[0.9] - marks[0.1])
    return sim.now - t0, steady


def t_throughput():
    print('\n--- one sender: goodput does not depend on how many are idle')
    rates = {}
    for n in (3, 8, 16):
        sim, addrs = network(n, seed=20 + n)
        sim.run(40, until=lambda s: s.joined())
        a, b = addrs[1], addrs[2]
        mid, data = send_file(sim, a, b, 30000, 'warm.bin')     # keys the pair
        sim.run(60, until=lambda s: state(s.app(a), mid) == 'delivered')
        mid, data = send_file(sim, a, b, 300000, 'bulk.bin')
        total, steady = progress_rate(sim, a, mid, 300000)
        rates[n] = steady or 0.0
        check('%2d nodes: 300 kB member -> member intact in %.1f s, steady '
              '%.0f kb/s' % (n, total, rates[n]),
              rx_file(sim, b, 'bulk.bin') == data)
    spread = max(rates.values()) / max(1e-9, min(rates.values()))
    check('goodput with 16 nodes within 10%% of 3 nodes (%.2fx)' % spread,
          spread < 1.1)
    check('member -> member steady goodput above 150 kb/s (BPSK 250 kb/s '
          'raw, two hops)', min(rates.values()) > 150,
          '%.0f' % min(rates.values()))


def t_aggregate():
    print('\n--- everyone sending at once: shared fairly, nothing lost')
    sim, addrs = network(6, seed=7)
    sim.run(40, until=lambda s: s.joined())
    mids = []
    pairs = [(a, addrs[(i + 1) % len(addrs)]) for i, a in enumerate(addrs)]
    for a, b in pairs:                       # key every pair first
        sim.app(a).submit('text', text='key', to=b)
    sim.run(20, until=lambda s: all(
        s.app(a).sec.ready(b, s.now) for a, b in pairs))
    t0 = sim.now
    for a, b in pairs:
        mid, data = send_file(sim, a, b, 60000, 'f%d.bin' % a)
        mids.append((a, b, mid, data))
    sim.run(200, until=lambda s: all(state(s.app(a), m) in
                                      ('delivered', 'failed')
                                      for a, _, m, _ in mids))
    dt = sim.now - t0
    intact = all(rx_file(sim, b, 'f%d.bin' % a) == d for a, b, _, d in mids)
    check('6 simultaneous 60 kB transfers all intact', intact)
    agg = 6 * 480.0 / dt
    check('aggregate goodput %.0f kb/s (one shared uplink)' % agg, agg > 120)
    done_at = {}
    for a, b, m, _ in mids:
        done_at[a] = None
    sim2, addrs2 = network(4, seed=8)
    sim2.run(40, until=lambda s: s.joined())
    hub = sim2.hubs()[0]
    senders = [x for x in addrs2 if x != hub]
    for a in senders:
        sim2.app(a).submit('text', text='key', to=hub)
    sim2.run(20, until=lambda s: all(s.app(a).sec.ready(hub, s.now)
                                     for a in senders))
    files = [send_file(sim2, a, hub, 100000, 'g%d.bin' % a) + (a,)
             for a in senders]
    t0 = sim2.now
    finish = {}

    def watch(s):
        for mid, _, a in files:
            if a not in finish and state(s.app(a), mid) == 'delivered':
                finish[a] = s.now - t0
        return len(finish) == len(files)
    sim2.run(200, until=watch, check_every=0.05)
    times = sorted(finish.values())
    check('3 members uploading to the hub finish within 25%% of each other '
          '(%s s)' % ', '.join('%.1f' % t for t in times),
          len(times) == 3 and times[-1] / times[0] < 1.25)


def t_chat_latency():
    print('\n--- chat latency')
    sim, addrs = network(5, seed=3)
    sim.run(40, until=lambda s: s.joined())
    hub = sim.hubs()[0]
    a, b = [x for x in addrs if x != hub][:2]
    mid = sim.app(a).submit('text', text='first', to=b)
    t0 = sim.now
    sim.run(20, until=lambda s: state(s.app(a), mid) == 'delivered')
    first = sim.now - t0
    check('first message (includes the handshake) delivered in %.2f s'
          % first, first < 4.0)
    for gap, label in ((1.3, 'conversation'), (4.1, 'after silence')):
        lat = []
        ok0 = sim.mac(a).stats['cont_ok']
        for i in range(8):
            sim.run(gap)
            t0 = sim.now
            n0 = len(inbox(sim.app(b)))
            sim.app(a).submit('text', text='msg %d' % i, to=b)
            sim.run(5, until=lambda s: len(inbox(s.app(b))) > n0,
                    check_every=0.01)
            lat.append(sim.now - t0)
        lat.sort()
        check('member -> member, %s: median %.2f s, worst %.2f s'
              % (label, lat[len(lat) // 2], lat[-1]),
              lat[len(lat) // 2] < 0.8 and lat[-1] < 1.5)
        if label == 'after silence':
            used = sim.mac(a).stats['cont_ok'] - ok0
            check('with no grant pending, the line went out in a contention '
                  'minislot (%d of 8)' % used, used >= 4)


def t_contention():
    print('\n--- many members contend at once')
    sim, addrs = network(10, seed=11)
    sim.run(40, until=lambda s: s.joined())
    hub = sim.hubs()[0]
    members = [x for x in addrs if x != hub]
    for a in members:
        sim.app(a).submit('text', text='key', to=hub)
    sim.run(30, until=lambda s: all(s.app(a).sec.ready(hub, s.now)
                                    for a in members))
    sim.run(2)
    n0 = len(inbox(sim.app(hub)))
    for a in members:
        sim.app(a).submit('text', text='burst from %d' % a, to=hub)
    t0 = sim.now
    ok = sim.run(15, until=lambda s: len(inbox(s.app(hub))) - n0
                 >= len(members), check_every=0.02)
    check('9 members, same instant: all delivered in %.2f s' % (sim.now - t0),
          ok)
    fails = sum(sim.mac(a).stats['cont_fail'] for a in members)
    print('     contention collisions resolved by backoff: %d' % fails)


def t_leave_and_failover():
    print('\n--- a member leaves; the hub dies')
    sim, addrs = network(5, seed=5)
    sim.keep_log = True
    sim.run(40, until=lambda s: s.joined())
    hub = sim.hubs()[0]
    gone = addrs[-1]
    sim.kill(gone)
    t0 = sim.now
    sim.run(25, until=lambda s: gone not in s.mac(hub).members)
    check('dead member %d dropped after %.1f s' % (gone, sim.now - t0),
          gone not in sim.mac(hub).members)
    sim.run(1)
    others = [a for a in addrs if a not in (hub, gone)]
    check('the others see it gone',
          all(gone not in sim.app(a).online for a in others))
    term0 = sim.mac(hub).term
    sim.kill(hub)
    t0 = sim.now
    ok = sim.run(20, until=lambda s: s.joined())
    new = sim.hubs()
    check('hub %d killed: hub %s takes over, all rejoined in %.1f s'
          % (hub, new, sim.now - t0), ok and len(new) == 1
          and sim.now - t0 < 6.0)
    check('new term (%s > %d)' % (sim.mac(new[0]).term if new else '?', term0),
          bool(new) and sim.mac(new[0]).term > term0)
    a, b = [x for x in others if x not in new][:2]
    mid = sim.app(a).submit('text', text='after failover', to=b)
    sim.run(15, until=lambda s: state(s.app(a), mid) == 'delivered')
    check('chat works through the new hub', 'after failover'
          in inbox(sim.app(b)))
    mid = sim.app(a).submit('text', text='to the new hub', to=new[0])
    sim.run(15, until=lambda s: state(s.app(a), mid) == 'delivered')
    check('and to the new hub itself', 'to the new hub'
          in inbox(sim.app(new[0])))


def t_split_brain():
    print('\n--- two halves that cannot hear each other, then can')
    sim, addrs = network(6, seed=13)
    left, right = [1, 2, 3], [4, 5, 6]
    for a in left:
        for b in right:
            sim.gain[(a, b)] = sim.gain[(b, a)] = -200.0
    for a in left + right:
        for b in left + right:
            if sim.gain.get((a, b)) == -200.0:
                sim.link_loss[(a, b)] = 1.0
    sim.run(20)
    hubs = sorted(sim.hubs())
    check('partitioned: one hub per half (%s)' % hubs, len(hubs) == 2)
    sim.gain.clear()
    sim.link_loss.clear()
    t0 = sim.now
    ok = sim.run(30, until=lambda s: s.joined() and len(s.hubs()) == 1)
    check('healed: a single hub (%s) and everyone joined in %.1f s'
          % (sim.hubs(), sim.now - t0), ok)


def t_split_brain_no_members():
    print('\n--- two nodes that both became hub (nobody to report it)')
    sim = S.FrameSim(security=True, seed=37)
    sim.provision([1, 2])
    for a in (1, 2):
        sim.add(a)
    sim.gain[(1, 2)] = sim.gain[(2, 1)] = -200.0
    sim.link_loss[(1, 2)] = sim.link_loss[(2, 1)] = 1.0
    sim.run(6)
    check('both hubs while they cannot hear each other (%s)' % sim.hubs(),
          sorted(sim.hubs()) == [1, 2])
    sim.gain.clear()
    sim.link_loss.clear()
    t0 = sim.now
    ok = sim.run(60, until=lambda s: s.joined())
    check('a hub listening on the downlink finds the other: one hub (%s) '
          'after %.1f s' % (sim.hubs(), sim.now - t0), ok)
    listens = sum(sim.mac(a).stats['listens'] for a in (1, 2))
    check('found by listening (%d listens)' % listens, listens >= 1)


def t_roles():
    print('\n--- roles: preferred hub, never-hub')
    sim, addrs = network(4, seed=17, roles={4: 'hub', 1: 'member'})
    sim.run(30, until=lambda s: s.joined())
    check('role=hub node 4 is hub despite the highest address',
          sim.hubs() == [4])
    sim.kill(4)
    sim.run(20, until=lambda s: s.joined())
    check('after it dies, the hub is not the role=member node 1 (%s)'
          % sim.hubs(), sim.hubs() and 1 not in sim.hubs())


def t_lossy():
    print('\n--- 10% frame loss everywhere, MAPs included')
    sim, addrs = network(4, seed=19, loss=0.10)
    ok = sim.run(60, until=lambda s: s.joined())
    check('joins on a lossy channel (%.1f s)' % sim.now, ok)
    hub = sim.hubs()[0]
    a, b = [x for x in addrs if x != hub][:2]
    mid, data = send_file(sim, a, b, 80000, 'lossy.bin')
    sim.run(200, until=lambda s: state(s.app(a), mid) in
            ('delivered', 'failed'))
    check('80 kB file intact through 10%% loss on both hops (%s)'
          % state(sim.app(a), mid), rx_file(sim, b, 'lossy.bin') == data)
    missed = sim.mac(a).stats['map_missed']
    check('MAP loss tolerated (%d missed, still a member)' % missed,
          missed > 0 and sim.mac(a).role == M.ROLE_MEMBER)


def t_late_grants():
    print('\n--- loop latency longer than the MAP lead')
    sim = S.FrameSim(security=False, seed=23, lat=(300000, 360000))
    for a in (1, 2, 3):
        sim.add(a, lam=None)
    sim.run(40, until=lambda s: s.joined())
    hub = sim.mac(sim.hubs()[0]) if sim.hubs() else None
    a, b = [x for x in (1, 2, 3) if x != sim.hubs()[0]][:2]
    sim.app(a).submit('text', text='late?', to=b)
    sim.run(30, until=lambda s: 'late?' in inbox(s.app(b)))
    check('members report late grants and the hub raises the lead to %s'
          % (hub.lead if hub else '?'), hub is not None and hub.lead > 2)
    check('traffic flows afterwards', 'late?' in inbox(sim.app(b)))


def t_discontinuity():
    print('\n--- a member loses samples (RX overflow)')
    sim, addrs = network(3, seed=29)
    sim.run(30, until=lambda s: s.joined())
    hub = sim.hubs()[0]
    a, b = [x for x in addrs if x != hub]
    r = sim.radios[a]
    r.t0 -= 7000                                       # 7 ms of samples lost
    sim.run(2)
    check('detected', sim.mac(a).stats['discontinuity'] >= 1)
    mid = sim.app(a).submit('text', text='after the jump', to=b)
    sim.run(15, until=lambda s: state(s.app(a), mid) == 'delivered')
    check('and corrected by ranging: traffic still flows',
          'after the jump' in inbox(sim.app(b)))
    sim.run(2)
    err = sim.mac(hub).members[a].err
    check('timing error back within +-64 samples (%s)' % err,
          err is not None and abs(err) <= 64)


def t_roster():
    print('\n--- a node missing from the roster cannot join')
    sim, addrs = network(3, seed=31)
    sim.run(30, until=lambda s: s.joined())
    import bpsk_keys
    rogue = os.path.join(sim.root, 'keys_9')
    bpsk_keys.provision({9: rogue}, network=sim.network)
    sim.keys[9] = rogue
    sim.add(9)
    sim.run(10)
    hub = sim.mac(sim.hubs()[0])
    check('ranging from 9 refused (%d)' % hub.stats['refused'],
          9 not in hub.members and hub.stats['refused'] >= 1)


def main():
    t0 = time.time()
    for t in (t_join, t_throughput, t_aggregate, t_chat_latency,
              t_contention, t_leave_and_failover, t_split_brain,
              t_split_brain_no_members, t_roles,
              t_lossy, t_late_grants, t_discontinuity, t_roster):
        t()
    print('\n(%.0f s)' % (time.time() - t0))
    if FAILURES:
        print('FAILED: %d check(s):' % len(FAILURES))
        for f in FAILURES:
            print('   ' + f)
        return 1
    print('all MAC checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
