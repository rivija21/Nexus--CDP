"""r6 multi-node network: discovery, channels, addresses, trust, privacy.

Complete ChatApp nodes on the bpsk_meshsim air model (K channels, per-channel
collisions, optional loss and adjacent-channel leakage).

    python3 test_mesh.py            all scenarios
    python3 test_mesh.py group tofu one or more by name
"""

import os
import random
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bpsk_app as app                                          # noqa: E402
import bpsk_crypto as C                                         # noqa: E402
import bpsk_link as L                                           # noqa: E402
import bpsk_mac as M                                            # noqa: E402
import bpsk_meshsim as sim                                      # noqa: E402

FAILURES = []


def check(label, cond, detail=''):
    print('  [%s] %s%s' % ('PASS' if cond else 'FAIL', label,
                           (' - ' + detail) if detail else ''))
    if not cond:
        FAILURES.append(label)


class Scripted(random.Random):
    """Returns scripted values for the first uniform()/choice() calls."""

    def __init__(self, seed, uniforms=(), choices=()):
        random.Random.__init__(self, seed)
        self._u = list(uniforms)
        self._c = list(choices)

    def uniform(self, a, b):
        return self._u.pop(0) if self._u else random.Random.uniform(self, a, b)

    def choice(self, seq):
        if self._c:
            want = self._c.pop(0)
            if want in seq:
                return want
        return random.Random.choice(self, seq)


def net(root, name, **kw):
    return sim.MeshSim(tempfile.mkdtemp(prefix=name + '_', dir=root), **kw)


def all_secure(s, names):
    return all(s.secure(a, b) for a in names for b in names if a < b)


# --------------------------------------------------------------------------
def t_units(root):
    print('\n--- MAC building blocks')
    offs = [M.channel_offset(k) for k in range(4)]
    check('4 channels at -900/-300/+300/+900 kHz (none at DC)',
          offs == [-900e3, -300e3, 300e3, 900e3], str(offs))
    ident = bytes(range(32))
    a0 = M.derive_address(ident, 0)
    check('address derivation is deterministic and never 0 or broadcast',
          a0 == M.derive_address(ident, 0)
          and all(1 <= M.derive_address(os.urandom(32), n) <= 254
                  for n in range(3) for _ in range(300)))
    b = M.pack_beacon(2, 1, ident, 'Kasun සිංහල' * 4,
                      True)
    u = M.unpack_beacon(b)
    check('beacon round trip (nickname trimmed to 32 bytes, UTF-8 safe)',
          u['channel'] == 2 and u['n'] == 1 and u['identity'] == ident
          and u['is_key'] and len(u['nick'].encode('utf-8')) <= 32)
    check('garbage is not a beacon', M.unpack_beacon(b'\x01' * 10) is None
          and M.unpack_beacon(b'\x07' + b[1:]) is None)
    am = M.AddressManager(ident)
    other = os.urandom(32)
    ev = am.on_beacon(am.addr, other, active=False)
    check('a listening node yields its address to an active one',
          ev and ev[0] == 'readdress' and am.addr != a0 and am.n >= 1)
    hi, lo = b'\xff' * 32, b'\x00' * 32
    am_hi, am_lo = M.AddressManager(hi, fixed=None), M.AddressManager(lo)
    am_hi.addr = am_lo.addr = 77
    e_hi = am_hi.on_beacon(77, lo, active=True)
    e_lo = am_lo.on_beacon(77, hi, active=True)
    check('two active nodes: only the larger identity moves',
          e_hi[0] == 'readdress' and e_lo[0] == 'conflict')
    cm = M.ChannelManager(4, rng=random.Random(1))
    cm.start(0.0)
    for k in (0, 1, 3):
        cm.heard(k, 10 + k, 1.0)
    ev = []
    t = 1.0
    while cm.state == 'listen':
        t += 0.01
        ev += cm.tick(t)
        for k in (0, 1, 3):
            cm.heard(k, 10 + k, t)
    check('the only free channel is claimed', cm.channel == 2
          and ev[-1][0] == 'claim', repr(ev))
    cm2 = M.ChannelManager(2, rng=random.Random(2))
    cm2.start(0.0)
    t = 0.0
    evs = []
    while t < 5:
        t += 0.01
        cm2.heard(0, 5, t)
        cm2.heard(1, 6, t)
        evs += cm2.tick(t)
    check('all channels busy: full, nothing transmitted',
          cm2.state == 'full' and not cm2.tx_enabled
          and [e for e, _ in evs] == ['full'])


def t_discovery(root):
    print('\n--- four nodes find each other and share out the channels')
    s = net(root, 'disc', seed=11)
    for i, n in enumerate('ABCD'):
        s.add(n, start=i * 0.4)
    s.trust_all()
    ok = s.run(12, until=lambda x: x.settled())
    check('settled: 4 distinct channels, 4 distinct addresses, full peer '
          'lists', ok, 'after %.1f s' % s.t)
    a = s.app('A')
    check('addresses are derived from the public keys',
          all(n.app.my_addr == M.derive_address(n.app.identity,
                                                n.app.addrm.n)
              for n in s.alive()))
    check('peer table: nickname, address and channel of every other node',
          all(a.peers[s.pid(n)]['nick'] == n
              and a.peers[s.pid(n)]['channel'] == s.app(n).chan.channel
              and a.peers[s.pid(n)]['addr'] == s.app(n).my_addr
              for n in 'BCD'))
    ok = s.run(10, until=lambda x: all_secure(x, 'ABCD'))
    check('every pair sets up its own encrypted session (6 sessions)', ok,
          'at %.1f s' % s.t)
    check('no channel collisions in a staggered start',
          sum(n.app.chan.collisions for n in s.alive()) == 0)
    print('    %s' % ', '.join('%s: addr %d ch %d' % (n.name, n.app.my_addr,
                                                      n.app.chan.channel)
                              for n in s.alive()))


def t_channel_collision(root):
    print('\n--- two nodes claim the same channel at the same instant')
    s = net(root, 'chcol', seed=12)
    s.add('A', start=0.0, rng=Scripted(1, uniforms=[2.5], choices=[1]))
    s.add('B', start=0.0, rng=Scripted(2, uniforms=[2.5], choices=[1]))
    s.add('C', start=0.1)
    s.trust_all()
    s.run(2.6)
    check('both transmit on channel 1', s.app('A').chan.channel == 1
          and s.app('B').chan.channel == 1)
    ok = s.run(15, until=lambda x: x.settled())
    cols = {n: s.app(n).chan.collisions for n in 'AB'}
    check('the silence check detects it and one node moves', ok
          and sum(cols.values()) >= 1, 'collisions %r, settled at %.1f s'
          % (cols, s.t))
    ok = s.run(10, until=lambda x: all_secure(x, 'ABC'))
    mid = s.app('A').submit('text', text='after the move',
                            to=s.pid('B'))
    ok = s.run(5, until=lambda x: x.state('A', mid) == 'delivered')
    check('traffic flows afterwards', ok, s.state('A', mid))


def colliding_keys():
    """Two private keys whose public keys derive the same address."""
    seen = {}
    while True:
        priv = C.generate_private()
        addr = M.derive_address(C.x25519_base(priv), 0)
        if addr in seen:
            return seen[addr], priv, addr
        seen[addr] = priv


def t_address_collision(root):
    print('\n--- address collisions')
    k1, k2, addr = colliding_keys()
    s = net(root, 'addr1', seed=13)
    s.add('A', key=k1, start=0.0)
    s.add('B', key=k2, start=5.0)
    s.trust_all()
    s.run(5.0)
    check('A took the derived address %d' % addr, s.app('A').my_addr == addr)
    ok = s.run(10, until=lambda x: x.settled())
    b = s.app('B')
    check('B (still listening) heard A\'s beacon and moved on',
          ok and b.my_addr != addr and b.addrm.n >= 1
          and s.app('A').my_addr == addr,
          'B is %d (n=%d)' % (b.my_addr, b.addrm.n))
    check('B\'s choice is persisted (same address after a restart)',
          b.keys.get_state('addr_n') == b.addrm.n)
    s.run(8, until=lambda x: x.secure('A', 'B'))
    mid = b.submit('text', text='hello from the mover', to=s.pid('A'))
    ok = s.run(5, until=lambda x: x.state('B', mid) == 'delivered')
    check('and they talk', ok)

    print('  both already active with the same address:')
    s = net(root, 'addr2', seed=14)
    s.add('A', key=k1, start=0.0, rng=Scripted(3, uniforms=[2.5], choices=[0]))
    s.add('B', key=k2, start=0.0, rng=Scripted(4, uniforms=[2.5], choices=[2]))
    s.trust_all()
    ok = s.run(15, until=lambda x: x.settled())
    a, b = s.app('A'), s.app('B')
    big, small = (a, b) if a.identity > b.identity else (b, a)
    check('the larger identity moved, the smaller kept %d' % addr,
          ok and small.my_addr == addr and big.my_addr != addr,
          'A=%d B=%d' % (a.my_addr, b.my_addr))
    ok = s.run(10, until=lambda x: x.secure('A', 'B'))
    mid = big.submit('text', text='new address, same key',
                     to=app.peer_id(small.identity))
    ok = ok and s.run(5, until=lambda x: small._by_mid is not None and any(
        m.get('text') == 'new address, same key' for m in small.messages))
    check('sessions are rebuilt on the new address and traffic flows', ok)


def t_tofu(root):
    print('\n--- trust on first use: nothing flows until the operator approves')
    s = net(root, 'tofu', seed=15)
    for i, n in enumerate('ABC'):
        s.add(n, start=i * 0.3)
    ok = s.run(12, until=lambda x: x.settled())
    a, b, c = s.app('A'), s.app('B'), s.app('C')
    pb, pa = s.pid('B'), s.pid('A')
    check('new nodes are listed but not trusted', ok
          and not a.peers[pb]['trusted'] and a.peers[pb]['fp'] ==
          b.fingerprint, a.peers[pb]['fp'] or '')
    check('the operator is told to compare fingerprints',
          any('NEW node' in m.get('text', '') and b.fingerprint
              in m.get('text', '') for m in a.messages))
    mid = a.submit('text', text='too early', to=pb)
    s.run(0.5)
    check('a message to an unapproved node is refused, not sent',
          s.state('A', mid) == 'failed'
          and 'approved' in a._by_mid[mid]['note'])
    a.submit('trust', peer=pb, trust=True)
    s.run(4)
    check('approval on one side only: no session',
          not a.sec.ready(b.my_addr, s.t) and b.sec.stats['hs_fail'] > 0)
    b.submit('trust', peer=pa, trust=True)
    ok = s.run(8, until=lambda x: x.secure('A', 'B'))
    check('approved on both sides: session within seconds', ok,
          'at %.1f s' % s.t)
    mid = a.submit('text', text='now it works', to=pb)
    ok = s.run(5, until=lambda x: x.state('A', mid) == 'delivered')
    check('... and the message is delivered', ok
          and 'now it works' in s.texts('B'))
    g = a.submit('text', text='to whoever I trust', to='all')
    s.run(5, until=lambda x: x.state('A', g) == 'delivered')
    check('the group goes only to approved nodes',
          list(a._by_mid[g]['rcpt']) == [pb]
          and 'to whoever I trust' not in s.texts('C'))
    check('C, never approved, holds no session with anyone',
          not c.sec.ready(a.my_addr, s.t) and not c.sec.ready(b.my_addr, s.t))
    check('approvals are saved in trusted.json',
          os.path.isfile(a.keys.trusted_path)
          and b.identity in a.keys.trusted)
    a2 = s.restart('A')
    check('... and survive a restart', a2.keys.is_trusted(b.identity))
    ok = s.run(15, until=lambda x: x.secure('A', 'B'))
    check('the restarted node re-keys with B on its own', ok)
    a2.submit('trust', peer=pb, trust=False)
    s.run(0.5)
    check('withdrawing approval drops the session',
          not a2.keys.is_trusted(b.identity)
          and not a2.sec.ready(b.my_addr, s.t))


def t_group(root):
    print('\n--- group chat: separately encrypted copies, per-recipient receipts')
    s = net(root, 'group', seed=16)
    for i, n in enumerate('ABCD'):
        s.add(n, start=i * 0.3)
    s.trust_all()
    s.run(15, until=lambda x: x.settled() and all_secure(x, 'ABCD'))
    a = s.app('A')
    text = 'meeting at 3 - ' + 'x' * 400
    mid = a.submit('text', text=text, to='all')
    ok = s.run(5, until=lambda x: x.state('A', mid) == 'delivered')
    msg = a._by_mid[mid]
    check('delivered to all three', ok and len(msg['rcpt']) == 3
          and all(r['state'] == 'delivered' for r in msg['rcpt'].values()))
    check('each receiver shows it in the group conversation',
          all(text in s.texts(n, conv='all') for n in 'BCD'))
    replies = {n: s.app(n).submit('text', text='ack from ' + n, to='all')
               for n in 'BCD'}
    ok = s.run(5, until=lambda x: all(x.state(n, m) == 'delivered'
                                      for n, m in replies.items()))
    check('everyone can post; everyone sees every post',
          ok and all(sorted(s.texts(n, conv='all')) ==
                     sorted([text] + ['ack from ' + m for m in 'BCD'
                                      if m != n])
                     for n in 'BCD'))
    s.stop('D')
    mid = a.submit('text', text='D just left', to='all')
    ok = s.run(15, until=lambda x: x.state('A', mid) in ('partial',
                                                         'failed'))
    msg = a._by_mid[mid]
    check('a recipient that vanished: partial, and the note names it',
          msg['state'] == 'partial' and 'not to D' in (msg['note'] or ''),
          '%s / %s' % (msg['state'], msg['note']))
    s.run(6)
    mid = a.submit('text', text='only those here', to='all')
    s.run(5, until=lambda x: x.state('A', mid) == 'delivered')
    check('once D is away, the group goes to those in range only',
          len(a._by_mid[mid]['rcpt']) == 2
          and s.state('A', mid) == 'delivered')


def t_concurrent(root):
    print('\n--- two pairs moving files at once, 1% frame loss')
    s = net(root, 'conc', seed=17, loss=0.01)
    for i, n in enumerate('ABCD'):
        s.add(n, start=i * 0.3)
    s.trust_all()
    s.run(15, until=lambda x: x.settled() and all_secure(x, 'ABCD'))
    rnd = random.Random(5)
    fa = bytes(rnd.getrandbits(8) for _ in range(60000))
    fc = bytes(rnd.getrandbits(8) for _ in range(60000))
    fb = bytes(rnd.getrandbits(8) for _ in range(20000))
    t0 = s.t
    ma = s.app('A').submit('file', name='a.bin', mime='', data=fa,
                           to=s.pid('B'))
    mc = s.app('C').submit('file', name='c.bin', mime='', data=fc,
                           to=s.pid('D'))
    mb = s.app('B').submit('file', name='b.bin', mime='', data=fb,
                           to=s.pid('A'))
    texts = {n: [s.app(n).submit('text', text='%s line %d' % (n, i),
                                 to=s.pid(dst)) for i in range(10)]
             for n, dst in (('A', 'C'), ('D', 'A'), ('B', 'C'))}
    ok = s.run(300, until=lambda x: all(x.state(n, m) == 'delivered'
                                        for n, m in (('A', ma), ('C', mc),
                                                     ('B', mb)))
               and all(x.state(n, m) == 'delivered'
                       for n, ms in texts.items() for m in ms))
    dt = s.t - t0

    def got(name, fname):
        for m in s.files_in(name):
            if m['file']['name'] == fname and m['state'] == 'received':
                return open(s.app(name).file_path(
                    m['file']['url'].split('/')[-1]), 'rb').read()
        return None
    check('all three files arrive byte-identical', ok
          and got('B', 'a.bin') == fa and got('D', 'c.bin') == fc
          and got('A', 'b.bin') == fb, '%.1f s' % dt)
    check('all 30 text lines delivered in order to the right node',
          s.texts('C', frm='A') == ['A line %d' % i for i in range(10)]
          and s.texts('A', frm='D') == ['D line %d' % i for i in range(10)]
          and s.texts('C', frm='B') == ['B line %d' % i for i in range(10)])
    total = (len(fa) + len(fc) + len(fb)) * 8
    print('    %.0f kB of files plus 30 lines in %.1f s (%.0f kb/s aggregate)'
          % (total / 8e3, dt, total / dt / 1e3))

    # Speed: the same file over one pair alone, then over two pairs at once.
    s.run(30)                               # past the early channel checks
    data = fa[:40000]

    def timed(pairs):
        t0 = s.t
        ms = [(a, s.app(a).submit('file', name='s.bin', mime='', data=data,
                                  to=s.pid(b))) for a, b in pairs]
        s.run(120, until=lambda x: all(x.state(a, m) == 'delivered'
                                       for a, m in ms))
        return s.t - t0
    one = timed([('A', 'B')])
    two = timed([('A', 'B'), ('C', 'D')])
    print('    40 kB: %.1f s over one pair alone (%.0f kb/s), %.1f s each '
          'with a second pair busy (%.0f kb/s aggregate)'
          % (one, len(data) * 8 / one / 1e3, two,
             2 * len(data) * 8 / two / 1e3))
    check('a second pair on its own channel costs the first nothing',
          two < 1.15 * one, '%.1f s vs %.1f s' % (two, one))
    checks = sum(n.app.chan.checks for n in s.alive())
    check('silence checks ran during the transfer without breaking it',
          checks >= 4, '%d checks' % checks)


def t_fair(root):
    print('\n--- one node sending to two peers at once shares its channel')
    s = net(root, 'fair', seed=18)
    for i, n in enumerate('ABC'):
        s.add(n, start=i * 0.3)
    s.trust_all()
    s.run(15, until=lambda x: x.settled() and all_secure(x, 'ABC'))
    data = bytes(random.Random(3).getrandbits(8) for _ in range(40000))
    mb = s.app('A').submit('file', name='b.bin', mime='', data=data,
                           to=s.pid('B'))
    mc = s.app('A').submit('file', name='c.bin', mime='', data=data,
                           to=s.pid('C'))
    done = {}

    def until(x):
        for k, m in (('B', mb), ('C', mc)):
            if k not in done and x.state('A', m) == 'delivered':
                done[k] = x.t
        return len(done) == 2
    t0 = s.t
    ok = s.run(120, until=until)
    tb, tc = done.get('B', 1e9) - t0, done.get('C', 1e9) - t0
    check('both transfers run side by side and finish together', ok
          and max(tb, tc) / min(tb, tc) < 1.3, '%.1f s and %.1f s' % (tb, tc))


def t_isolation(root):
    print('\n--- privacy: what the other nodes and an eavesdropper see')
    s = net(root, 'iso', seed=19)
    for i, n in enumerate('ABCD'):
        s.add(n, start=i * 0.3)
    s.trust_all()
    s.run(15, until=lambda x: x.settled() and all_secure(x, 'ABCD'))
    s.record = True
    secret = 'the vault code is 4417'
    mid = s.app('A').submit('text', text=secret, to=s.pid('B'))
    fdata = b'PRIVATE-FILE-' * 500
    mf = s.app('A').submit('file', name='plan.txt', mime='', data=fdata,
                           to=s.pid('B'))
    ok = s.run(30, until=lambda x: x.state('A', mid) == 'delivered'
               and x.state('A', mf) == 'delivered')
    check('A -> B delivered', ok and secret in s.texts('B'))
    check('C and D never display it', all(secret not in s.texts(n)
                                          and not s.files_in(n)
                                          for n in 'CD'))
    on_air = b''.join(r for _, _, _, r in s.log)
    check('neither the text nor the file appears on air in the clear',
          secret.encode() not in on_air and b'PRIVATE-FILE' not in on_air
          and b'plan.txt' not in on_air, '%d frames recorded' % len(s.log))
    c = s.app('C')
    check('C drops A->B frames by address and never tries to open them',
          c.sec.stats['auth_fail'] == 0 and c.link.stats['rx_notme'] > 0)
    s.add('E', start=s.t)                      # rogue: approved by nobody
    s.run(15)
    e = s.app('E')
    check('a rogue node in range learns addresses but gets no session',
          not any(e.sec.ready(s.app(n).my_addr, s.t) for n in 'ABCD'))


def t_full(root):
    print('\n--- a fifth node when all four channels are taken')
    s = net(root, 'full', seed=20)
    for i, n in enumerate('ABCD'):
        s.add(n, start=i * 0.3)
    s.add('E', start=6.0)
    s.trust_all()
    s.run(15, until=lambda x: x.t > 12)
    e = s.nodes['E']
    check('E waits: every channel is busy, it transmits nothing',
          e.app.chan.state == 'full' and e.sent == 0)
    check('... but it hears everyone', len([p for p in e.app.peers.values()
                                            if p['present']]) == 4)
    s.stop('D')
    ok = s.run(10, until=lambda x: x.app('E').chan.state == 'active')
    check('D leaves: E takes the freed channel', ok
          and e.app.chan.channel == s.app('D').chan.channel,
          'after %.1f s' % (s.t - 12))
    ok = s.run(15, until=lambda x: x.secure('A', 'E'))
    mid = e.app.submit('text', text='finally', to=s.pid('A'))
    ok = ok and s.run(5, until=lambda x: x.state('E', mid) == 'delivered')
    check('and joins the conversation', ok)


def t_leak(root):
    print('\n--- adjacent-channel leakage (strong neighbours) is not a '
          'collision')
    s = net(root, 'leak', seed=21, leak=0.4)
    for i, n in enumerate('ABCD'):
        s.add(n, start=i * 0.3)
    s.trust_all()
    ok = s.run(15, until=lambda x: x.settled())
    s.run(60)
    cols = sum(n.app.chan.collisions for n in s.alive())
    check('channels stay put for a minute despite 40% leakage',
          ok and cols == 0 and s.settled(), '%d collisions' % cols)
    mid = s.app('A').submit('text', text='through the leakage', to='all')
    ok = s.run(5, until=lambda x: x.state('A', mid) == 'delivered')
    check('traffic unaffected', ok)


def t_restart(root):
    print('\n--- a node restarts')
    s = net(root, 'rst', seed=22)
    for i, n in enumerate('ABC'):
        s.add(n, start=i * 0.3)
    s.trust_all()
    s.run(15, until=lambda x: x.settled() and all_secure(x, 'ABC'))
    old_addr = s.app('B').my_addr
    m = s.app('B').submit('text', text='before', to=s.pid('A'))
    s.run(5, until=lambda x: x.state('B', m) == 'delivered')
    s.app('A').link.engines[old_addr].rx[old_addr] = {
        'base': 10, 'buf': {}, 'epoch': s.app('A').link.engines[old_addr]
        .rx.get(old_addr, {}).get('epoch', 1)}
    b = s.restart('B')
    check('same key, same address', b.my_addr == old_addr)
    m2 = b.submit('text', text='after', to=s.pid('A'))
    ok = s.run(20, until=lambda x: x.state('B', m2) == 'delivered')
    check('A notices, re-keys, and the first message after the restart '
          'arrives', ok and s.texts('A', frm='B') == ['before', 'after'],
          repr(s.texts('A')))


def t_plain(root):
    print('\n--- security off: the same network, in the clear')
    s = net(root, 'plain', seed=23)
    for i, n in enumerate('ABC'):
        s.add(n, start=i * 0.3, security=False)
    ok = s.run(12, until=lambda x: x.settled())
    check('discovery works without keys (random persistent node id)', ok)
    mid = s.app('A').submit('text', text='hello in the clear', to='all')
    ok = s.run(5, until=lambda x: x.state('A', mid) == 'delivered')
    check('group message delivered, marked not encrypted', ok
          and not s.app('A')._by_mid[mid]['secure'])
    s.add('S', start=s.t)                      # an encrypted node joins
    s.run(8)
    sn = s.app('S')
    peer = sn.peers[s.pid('A')]
    check('an encrypted node lists them as unencrypted',
          not peer['is_key'] and not peer['trusted'] and peer['fp'] is None)
    mid = sn.submit('text', text='x', to=s.pid('A'))
    s.run(0.2)
    check('... and refuses to send to one', s.state('S', mid) == 'failed')


def t_static(root):
    print('\n--- static mode (my_addr set) keeps the r5.4 behaviour')
    tmp = tempfile.mkdtemp(prefix='static_', dir=root)
    a = app.ChatApp(1, 2, 'A', os.path.join(tmp, 'a'), security=False)
    out = a.on_tick(0.0)
    check('fixed channel, no listening, no beacons',
          ('chan', 0) in out and a.link.radio.enabled
          and not any(L.parse(v)['type'] == L.FT_BEACON
                      for k, v in out if k == 'tx'))


TESTS = [('units', t_units), ('discovery', t_discovery),
         ('chcollision', t_channel_collision),
         ('addrcollision', t_address_collision), ('tofu', t_tofu),
         ('group', t_group), ('concurrent', t_concurrent), ('fair', t_fair),
         ('isolation', t_isolation), ('full', t_full), ('leak', t_leak),
         ('restart', t_restart), ('plain', t_plain), ('static', t_static)]


def main(argv):
    root = tempfile.mkdtemp(prefix='mesh_')
    want = set(argv[1:])
    t0 = time.time()
    try:
        for name, fn in TESTS:
            if not want or name in want:
                fn(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print('\n%d failure(s) in %.0f s' % (len(FAILURES), time.time() - t0))
    for f in FAILURES:
        print('  - %s' % f)
    if not FAILURES:
        print('all mesh checks passed')
    return 1 if FAILURES else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
