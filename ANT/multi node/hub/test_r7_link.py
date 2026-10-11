"""r7 link layer and end-to-end security, over the MAC and the hub relay.

    python3 test_r7_link.py

Frame-level simulator (bpsk_mac_sim), complete nodes, encryption on unless a
case says otherwise.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bpsk_link as L                                           # noqa: E402
import bpsk_mac as M                                            # noqa: E402
import bpsk_mac_sim as S                                        # noqa: E402

FAILURES = []


def check(label, cond, detail=''):
    print('  [%s] %s%s' % ('PASS' if cond else 'FAIL', label,
                           (' - ' + detail) if detail else ''))
    if not cond:
        FAILURES.append(label)


def network(n, seed=1, security=True, loss=0.0):
    sim = S.FrameSim(security=security, seed=seed, loss=loss)
    addrs = list(range(1, n + 1))
    sim.provision(addrs)
    for a in addrs:
        sim.add(a)
    ok = sim.run(40, until=lambda s: s.joined())
    assert ok, 'network did not form'
    return sim, addrs


def state(app, mid):
    return app._by_mid.get(mid, {}).get('state')


def inbox(app, src=None):
    return [m['text'] for m in app.messages
            if m['dir'] == 'in' and m['kind'] == 'text'
            and (src is None or m['addr'] == src)]


def rx_file(sim, a, name):
    path = os.path.join(sim.root, 'store_%d' % a, 'rx_' + name)
    if not os.path.exists(path):
        return None
    with open(path, 'rb') as fh:
        return fh.read()


def t_concurrent_files():
    print('\n--- several transfers at once, to and from the same node')
    sim, addrs = network(4, seed=3)
    hub = sim.hubs()[0]
    a, b, c = [x for x in addrs if x != hub]
    jobs = []
    for src, dst, size in ((a, b, 60000), (c, b, 50000), (b, a, 40000),
                           (hub, c, 45000), (b, hub, 30000)):
        data = os.urandom(size)
        mid = sim.app(src).submit('file', name='%d_to_%d.bin' % (src, dst),
                                  mime='', data=data, to=dst)
        jobs.append((src, dst, mid, data))
    sim.run(200, until=lambda s: all(state(s.app(x), m) in
                                     ('delivered', 'failed')
                                     for x, _, m, _ in jobs))
    for src, dst, mid, data in jobs:
        check('%d -> %d (%d kB) delivered and intact'
              % (src, dst, len(data) // 1000),
              state(sim.app(src), mid) == 'delivered'
              and rx_file(sim, dst, '%d_to_%d.bin' % (src, dst)) == data)


def t_seq_wrap():
    print('\n--- 16-bit sequence numbers wrap')
    sim, addrs = network(3, seed=5)
    hub = sim.hubs()[0]
    a, b = [x for x in addrs if x != hub]
    sim.app(a).link.peer(b).tx_seq = 65500
    for i in range(80):
        sim.app(a).submit('text', text='w%02d' % i, to=b)
    sim.run(60, until=lambda s: len(inbox(s.app(b), a)) >= 80)
    got = inbox(sim.app(b), a)
    check('80 messages across seq 65500 -> 44 arrive exactly once, in order',
          got == ['w%02d' % i for i in range(80)],
          '%d received' % len(got))
    check('the sender wrapped', sim.app(a).link.tx[b].tx_seq < 100,
          str(sim.app(a).link.tx[b].tx_seq))


def t_relay_is_blind():
    print('\n--- the hub relays what it cannot read')
    sim, addrs = network(4, seed=7)
    hub = sim.hubs()[0]
    a, b, c = [x for x in addrs if x != hub]
    seen = []
    sim.tap(M.MacConfig().f_down, lambda raw, tx: seen.append(raw))
    secret = 'the launch code is 0000'
    mid = sim.app(a).submit('text', text=secret, to=b)
    data = os.urandom(20000)
    fm = sim.app(a).submit('file', name='plans.txt', mime='', data=data, to=b)
    sim.run(60, until=lambda s: state(s.app(a), fm) == 'delivered'
            and state(s.app(a), mid) == 'delivered')
    check('message and file delivered A -> B', secret in inbox(sim.app(b))
          and rx_file(sim, b, 'plans.txt') == data)
    relayed = []
    for raw in seen:
        try:
            frm = L.parse(raw)
        except L.BadFrame:
            continue
        if frm['src'] == a and frm['dst'] == b:
            relayed.append(frm)
    types = {L.FT_NAME[f['type']] for f in relayed}
    check('every relayed A -> B frame is sealed or a handshake (%s)'
          % sorted(types), relayed and types <= {'SEALED', 'HS_INIT'})
    blob = b''.join(f['payload'] for f in relayed)
    check('neither the text nor the file name appears on the downlink',
          secret.encode() not in blob and b'plans.txt' not in blob)
    check('the hub never handed it to its own application', secret not in
          inbox(sim.app(hub)) and sim.app(hub).link.rx.get(a) is None)
    check('a third node (%d, in the roster) opened nothing from A' % c,
          secret not in inbox(sim.app(c))
          and sim.app(c).link.rx.get(a) is None)


def t_forgery_and_replay():
    print('\n--- forged and replayed frames through the relay')
    sim, addrs = network(3, seed=9)
    hub = sim.hubs()[0]
    a, b = [x for x in addrs if x != hub]
    cap = []
    f_down = M.MacConfig().f_down
    sim.tap(f_down, lambda raw, tx: cap.append(raw))
    mid = sim.app(a).submit('text', text='pay 10', to=b)
    sim.run(20, until=lambda s: state(s.app(a), mid) == 'delivered')
    sealed = []
    for raw in cap:
        try:
            frm = L.parse(raw)
        except L.BadFrame:
            continue
        if frm['src'] == a and frm['dst'] == b and frm['type'] == L.FT_SEALED:
            sealed.append(frm)
    check('captured A -> B sealed frames', len(sealed) >= 1)
    st = sim.app(b).sec.stats
    replay0, auth0 = st['replay'], st['auth_fail']
    n_in = len(inbox(sim.app(b)))
    for frm in sealed:                                 # replay verbatim
        sim.inject(f_down, L.build(frm['type'], frm['dst'], frm['src'], 0, 0,
                                   frm['payload'], 0, 7), hub)
    for frm in sealed:                                 # tamper: new counter
        pl = bytearray(frm['payload'])                 # (so it is not just a
        pl[8] ^= 0x40                                  # replay) + one bit of
        pl[-20] ^= 0x01                                # the ciphertext
        sim.inject(f_down, L.build(frm['type'], frm['dst'], frm['src'], 0, 0,
                                   bytes(pl), 0, 9), hub)
    sim.run(2)
    check('replays dropped (%d)' % (st['replay'] - replay0),
          st['replay'] - replay0 >= len(sealed))
    check('tampered frames rejected (%d)' % (st['auth_fail'] - auth0),
          st['auth_fail'] - auth0 >= len(sealed))
    check('nothing extra delivered', len(inbox(sim.app(b))) == n_in)


def t_restart():
    print('\n--- a member restarts')
    sim, addrs = network(3, seed=11)
    hub = sim.hubs()[0]
    a, b = [x for x in addrs if x != hub]
    mid = sim.app(a).submit('text', text='before', to=b)
    sim.run(20, until=lambda s: state(s.app(a), mid) == 'delivered')
    sim.restart(b)
    sim.run(30, until=lambda s: s.joined())
    mid = sim.app(a).submit('text', text='after restart', to=b)
    sim.run(30, until=lambda s: state(s.app(a), mid) == 'delivered')
    check('the restarted node gets the next message',
          'after restart' in inbox(sim.app(b)), state(sim.app(a), mid))
    mid = sim.app(b).submit('text', text='and answers', to=a)
    sim.run(30, until=lambda s: state(s.app(b), mid) == 'delivered')
    check('and can answer', 'and answers' in inbox(sim.app(a)))


def t_offline_peer():
    print('\n--- a message for a peer that is not on the air')
    sim, addrs = network(3, seed=13)
    hub = sim.hubs()[0]
    a, b = [x for x in addrs if x != hub]
    sim.kill(b)
    sim.run(20, until=lambda s: b not in s.mac(hub).members)
    mid = sim.app(a).submit('text', text='are you there?', to=b)
    sim.run(5)
    check('queued while %d is offline (%s)' % (b, state(sim.app(a), mid)),
          state(sim.app(a), mid) == 'queued')
    sim.restart(b)
    sim.run(40, until=lambda s: state(s.app(a), mid) == 'delivered')
    check('delivered once it is back', 'are you there?' in inbox(sim.app(b)))


def t_insecure():
    print('\n--- security off')
    sim, addrs = network(3, seed=15, security=False)
    hub = sim.hubs()[0]
    a, b = [x for x in addrs if x != hub]
    mid = sim.app(a).submit('text', text='plain', to=b)
    sim.run(20, until=lambda s: state(s.app(a), mid) == 'delivered')
    check('member -> member works in the clear', 'plain' in inbox(sim.app(b)))
    mid = sim.app(hub).submit('text', text='from the hub', to=a)
    sim.run(20, until=lambda s: state(s.app(hub), mid) == 'delivered')
    check('hub -> member works', 'from the hub' in inbox(sim.app(a)))


def t_lossy_files():
    print('\n--- 15% loss on every link, file both ways')
    sim, addrs = network(3, seed=17, loss=0.15)
    hub = sim.hubs()[0]
    a, b = [x for x in addrs if x != hub]
    d1, d2 = os.urandom(50000), os.urandom(40000)
    m1 = sim.app(a).submit('file', name='ab.bin', mime='', data=d1, to=b)
    m2 = sim.app(b).submit('file', name='ba.bin', mime='', data=d2, to=a)
    sim.run(300, until=lambda s: state(s.app(a), m1) in ('delivered', 'failed')
            and state(s.app(b), m2) in ('delivered', 'failed'))
    check('both intact (%s, %s)' % (state(sim.app(a), m1),
                                    state(sim.app(b), m2)),
          rx_file(sim, b, 'ab.bin') == d1 and rx_file(sim, a, 'ba.bin') == d2)
    ls = sim.app(a).link.stats
    print('     A: %d retransmissions (%d fast), %d dropped'
          % (ls['retx'], ls['fast_retx'], ls['dropped']))


def main():
    for t in (t_concurrent_files, t_seq_wrap, t_relay_is_blind,
              t_forgery_and_replay, t_restart, t_offline_peer, t_insecure,
              t_lossy_files):
        t()
    print()
    if FAILURES:
        print('FAILED: %d check(s):' % len(FAILURES))
        for f in FAILURES:
            print('   ' + f)
        return 1
    print('all r7 link/security checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
