"""Security properties of the encrypted link, tested end to end.

Two (sometimes three) complete ChatApp nodes on a simulated channel that
records every frame, so the tests can act as an eavesdropper, tamper with
frames, replay them, restart a node, or play a third node with its own valid
roster key.

    python3 test_secure.py
"""

import os
import random
import shutil
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bpsk_app as app                                          # noqa: E402
import bpsk_crypto as C                                         # noqa: E402
import bpsk_keys                                                # noqa: E402
import bpsk_link as L                                           # noqa: E402
import bpsk_secure as S                                         # noqa: E402

FAILURES = []


def check(label, cond, detail=''):
    print('  [%s] %s%s' % ('PASS' if cond else 'FAIL', label,
                           (' - ' + detail) if detail else ''))
    if not cond:
        FAILURES.append(label)


OPTS = dict(ack_timeout=0.5, max_retries=5, frag_size=256, sym_rate=250000.0,
            overhead=460)


class Net(object):
    """Nodes on one shared channel. Every frame is recorded in self.air_log.

    A frame goes to every other node (a real broadcast medium, as in r6);
    each node's own address filtering decides what it keeps.
    """

    def __init__(self, root, addrs=(1, 2), roster=None, loss=0.0, seed=1,
                 drop=None):
        random.seed(seed)
        self.root = tempfile.mkdtemp(prefix='net_', dir=root)
        self.keys = {a: os.path.join(self.root, 'keys%d' % a) for a in addrs}
        bpsk_keys.provision({a: self.keys[a] for a in (roster or addrs)})
        self.nodes = {}
        for a in addrs:
            self.nodes[a] = self.make(a)
        self.air, self.air_log, self.t = [], [], 0.0
        self.loss, self.drop = loss, drop

    def make(self, a, peer=None):
        peer = peer if peer is not None else (2 if a == 1 else 1)
        return app.ChatApp(a, peer, 'Node %d' % a,
                           os.path.join(self.root, 'store%d' % a),
                           keys_dir=self.keys[a], **OPTS)

    def pump(self, me, actions):
        for kind, frame in actions:
            if kind != 'tx':
                continue
            self.air_log.append((self.t, me, frame))
            if random.random() < self.loss:
                continue
            if self.drop is not None and self.drop(me, L.parse(frame), self.t):
                continue
            leave = max(self.t, self.nodes[me].link.busy_until) + 0.02
            for dest in self.nodes:
                if dest != me:
                    self.air.append((leave, dest, frame))

    def inject(self, dest, frame):
        self.air.append((self.t + 0.001, dest, frame))

    def run(self, seconds, until=None):
        end, step = self.t + seconds, 0
        while self.t < end:
            self.t = round(self.t + 0.001, 6)
            due = sorted((x for x in self.air if x[0] <= self.t),
                         key=lambda x: x[0])
            self.air = [x for x in self.air if x[0] > self.t]
            for _, dest, frame in due:
                if dest in self.nodes:
                    self.pump(dest, self.nodes[dest].on_rx(frame, self.t))
            if step % 10 == 0:
                for addr, node in list(self.nodes.items()):
                    self.pump(addr, node.on_tick(self.t))
            step += 1
            if until is not None and step % 20 == 0 and until(self):
                return True
        return False

    def secure(self, a, b):
        return (self.nodes[a].sec.ready(b, self.t)
                and self.nodes[b].sec.ready(a, self.t))

    def texts(self, a):
        return [m['text'] for m in self.nodes[a].messages
                if m['dir'] == 'in' and m['kind'] == 'text']

    def sealed_frames(self, src=None, dst=None):
        out = []
        for t, me, raw in self.air_log:
            f = L.parse(raw)
            if f['type'] == S.FT_SEALED and (src is None or me == src) \
                    and (dst is None or f['dst'] == dst):
                out.append((t, raw, f))
        return out


def state(node, mid):
    return node._by_mid.get(mid, {}).get('state')


def reframe(frm, payload):
    """Rebuild a frame around a modified payload with a valid CRC."""
    return L.build(frm['type'], frm['dst'], frm['src'], frm['seq'], frm['ack'],
                   payload, frm['flags'], 0)


# ---------------------------------------------------------------------------
def t_establish(root):
    print('\n--- session establishment')
    net = Net(root)
    ok = net.run(10, until=lambda n: n.secure(1, 2))
    a, b = net.nodes[1].sec, net.nodes[2].sec
    check('both nodes reach a secure session', ok, '%.2f s' % net.t)
    check('they agree on the session (same handshake hash)',
          ok and a.peers[2].current.hash == b.peers[1].current.hash)
    check('simultaneous start converges on one session',
          a.peers[2].established == 1 and b.peers[1].established == 1)
    check('fingerprints shown on each side match the other\'s own key',
          a.fingerprint(2) == b.fingerprint() and b.fingerprint(1)
          == a.fingerprint())
    types = {L.FT_NAME[L.parse(r)['type']] for _, _, r in net.air_log}
    check('only IDLE filler, handshake and sealed frames are on air',
          types <= {'IDLE', 'HS_INIT', 'HS_RESP', 'SEALED'}, repr(sorted(types)))


def t_staggered(root):
    print('\n--- nodes switched on at different moments converge on ONE session')
    results = []
    for first in (1, 2):
        for off in (0.0, 0.005, 0.05, 0.3, 1.0, 2.5):
            net = Net(root)
            late = 2 if first == 1 else 1
            m1 = net.nodes[1].submit('text', text='x')
            m2 = net.nodes[2].submit('text', text='y')
            end, step = 30.0, 0
            while net.t < end:
                net.t = round(net.t + 0.001, 6)
                due = sorted((x for x in net.air if x[0] <= net.t),
                             key=lambda x: x[0])
                net.air = [x for x in net.air if x[0] > net.t]
                for _, dest, frame in due:
                    net.pump(dest, net.nodes[dest].on_rx(frame, net.t))
                if step % 10 == 0:
                    for addr, node in net.nodes.items():
                        if addr != late or net.t >= off:
                            net.pump(addr, node.on_tick(net.t))
                step += 1
            a, b = net.nodes[1].sec.peers[2], net.nodes[2].sec.peers[1]
            results.append(a.established == 1 and b.established == 1
                           and a.current.hash == b.current.hash
                           and state(net.nodes[1], m1) == 'delivered'
                           and state(net.nodes[2], m2) == 'delivered')
    check('12 start orders/offsets: one shared session each, traffic flows',
          all(results), '%d of %d' % (sum(results), len(results)))


def t_confidentiality(root):
    print('\n--- confidentiality: an eavesdropper records everything')
    net = Net(root, loss=0.1)
    secret = 'the combination is 31-41-59, keep it quiet'
    data = os.urandom(20000)
    ma = net.nodes[1].submit('text', text=secret)
    mf = net.nodes[1].submit('file', name='floorplan.png', mime='image/png',
                             data=data)
    net.nodes[2].submit('text', text='reply from bench B')
    net.run(200, until=lambda n: state(n.nodes[1], mf) == 'delivered'
            and state(n.nodes[1], ma) == 'delivered'
            and n.texts(1))
    check('text and file delivered through the seal', secret in net.texts(2)
          and state(net.nodes[1], mf) == 'delivered')
    # The scrambler is a fixed public keystream, so undo it before looking:
    # that is exactly what an eavesdropper with this source code would do.
    clear = b''.join(L._scramble(r[1:], r[0]) for _, _, r in net.air_log)
    leaks = [w for w in (secret.encode(), b'floorplan', b'image/png', b'Node 1',
                         b'reply from bench', data[:24], data[-24:])
             if w in clear]
    check('no plaintext, filename, MIME type, nickname or file bytes on air',
          not leaks, repr(leaks))


def t_tamper(root):
    print('\n--- integrity: a modified frame is rejected')
    net = Net(root)
    net.run(5, until=lambda n: n.secure(1, 2))
    b = net.nodes[2]
    n0 = len(net.air_log)
    mid = net.nodes[1].submit('text', text='transfer 100 to account 7')
    held = {}

    def hold(me, frm, t):
        # Swallow A's first sealed DATA frame; keep it for tampering.
        if me == 1 and frm['type'] == S.FT_SEALED and 'x' not in held \
                and len(frm['payload']) > 60:
            held['x'] = frm
            return True
        return False
    net.drop = hold
    net.run(0.3)
    net.drop = None
    frm = held['x']
    pl = bytearray(frm['payload'])
    pl[30] ^= 0x04                                   # one bit of ciphertext
    before = b.sec.stats['auth_fail']
    net.inject(2, reframe(frm, bytes(pl)))
    net.run(0.2)
    check('bit-flipped frame fails authentication and is not delivered',
          b.sec.stats['auth_fail'] == before + 1
          and not any('account' in t for t in net.texts(2)))
    hdr = bytearray(frm['payload'])
    hdr[2] = 9                                       # rewrite final dest
    net.inject(2, reframe(frm, bytes(hdr)))
    net.run(0.2)
    check('altered addressing is rejected as well',
          b.sec.stats['auth_fail'] == before + 2)
    net.run(10, until=lambda n: state(n.nodes[1], mid) == 'delivered')
    check('the genuine retransmission still gets through',
          net.texts(2) == ['transfer 100 to account 7'])
    del n0


def t_replay(root):
    print('\n--- replay: old frames and old handshakes are refused')
    net = Net(root)
    net.run(5, until=lambda n: n.secure(1, 2))
    mid = net.nodes[1].submit('text', text='open the gate')
    net.run(5, until=lambda n: state(n.nodes[1], mid) == 'delivered')
    b = net.nodes[2]
    data_frames = [r for t, r, f in net.sealed_frames(src=1)
                   if len(f['payload']) > 60]
    acks_before = b.link.stats['ack_tx']
    for raw in data_frames:
        net.inject(2, raw)
    net.run(0.5)
    check('replayed DATA frames are dropped', b.sec.stats['replay']
          >= len(data_frames), '%d replays' % b.sec.stats['replay'])
    check('... not delivered twice', net.texts(2) == ['open the gate'])
    check('... and not acknowledged (a replay cannot fake a receipt)',
          b.link.stats['ack_tx'] == acks_before)
    inits = [r for _, me, r in net.air_log
             if me == 1 and L.parse(r)['type'] == S.FT_HS_INIT]
    sess = b.sec.peers[1].current
    fails = b.sec.stats['hs_fail']
    net.inject(2, inits[0])
    net.run(0.3)
    check('a replayed handshake initiation is refused',
          b.sec.stats['hs_fail'] == fails + 1 and b.sec.peers[1].current is sess)


def t_third_node(root):
    print('\n--- isolation: a third node with its own valid key')
    net = Net(root, addrs=(1, 2, 3))
    net.nodes[3] = net.make(3, peer=2)          # C talks to B legitimately
    net.run(5, until=lambda n: n.secure(1, 2) and n.secure(3, 2))
    check('A-B and C-B sessions both exist', net.secure(1, 2)
          and net.secure(3, 2))
    mid = net.nodes[1].submit('text', text='for B only: meet at 5')
    net.run(5, until=lambda n: state(n.nodes[1], mid) == 'delivered')
    c = net.nodes[3]
    heard = sum(1 for _, me, raw in net.air_log if me == 1
                and L.parse(raw)['type'] == S.FT_SEALED)
    check('C receives every A->B frame but opens none of them',
          'for B only: meet at 5' in net.texts(2) and not net.texts(3)
          and heard > 0 and c.sec.stats['opened_rx'] == 0
          and c.link.stats['rx_notme'] >= heard,
          '%d sealed A->B frames on air' % heard)
    # C now forges a frame claiming to come from A, sealed with C's own
    # (valid) session with B.
    sess = c.sec.peers[2].current
    ctr = sess.tx_ctr
    sess.tx_ctr += 1
    head = struct.pack('!BBBIQ', S.SEC_VERSION, 1, 2, sess.remote_idx, ctr)
    inner = struct.pack('!BBBB', L.FT_DATA, 77, 0, 0) + b'A says: cancel'
    body = C.aead_encrypt(sess.k_send, S._nonce(ctr), inner, head)
    b = net.nodes[2]
    fails = b.sec.stats['unknown_session']
    net.inject(2, L.build(S.FT_SEALED, 2, 1, 0, 0, head + body, 0, 0))
    net.run(0.3)
    check('C cannot impersonate A, even with its own roster key',
          b.sec.stats['unknown_session'] == fails + 1
          and not any('cancel' in t for t in net.texts(2)))


def t_not_in_roster(root):
    print('\n--- a node that is not in the roster')
    net = Net(root, addrs=(1, 2))
    # Node 4 has its own key and even lists node 2 (with a key it got from
    # somewhere), but node 2's roster does not list node 4.
    k4 = os.path.join(net.root, 'keys4')
    rogue_roster = {4: k4, 2: os.path.join(net.root, 'k2copy')}
    bpsk_keys.provision(rogue_roster)
    import json
    with open(os.path.join(net.keys[2], 'roster.json')) as fh:
        real_b = json.load(fh)['nodes']['2']['public_key']
    ks4 = S.KeyStore(k4, 4)
    nodes4 = dict(ks4.nodes)
    nodes4[2] = {'public_key': bytes.fromhex(real_b), 'name': 'Node 2'}
    S.save_roster(ks4.roster_path, ks4.network, nodes4)
    net.nodes[4] = app.ChatApp(4, 2, 'Rogue', os.path.join(net.root, 's4'),
                               keys_dir=k4, **OPTS)
    net.run(5)
    check('it cannot open a session with anyone',
          not net.nodes[4].sec.ready(2, net.t)
          and 4 not in {s.peer for s in net.nodes[2].sec.by_idx.values()})
    check('its handshakes are refused by the node it targets',
          net.nodes[2].sec.stats['hs_fail'] > 0)


def t_restart(root):
    print('\n--- a node restarts mid-conversation')
    net = Net(root)
    net.run(5, until=lambda n: n.secure(1, 2))
    m = net.nodes[2].submit('text', text='before restart')
    net.run(5, until=lambda n: state(n.nodes[2], m) == 'delivered')
    # Make A's receive window for B point somewhere B's fresh numbering
    # (starting at 0) would look like old frames - the restart hazard.
    net.nodes[1].link.rx[2]['base'] = 10
    net.nodes[2] = net.make(2)                  # same keys, fresh process
    m2 = net.nodes[2].submit('text', text='after restart')
    ok = net.run(20, until=lambda n: state(n.nodes[2], m2) == 'delivered')
    check('a new session is set up automatically', net.secure(1, 2))
    check('the first message after the restart is delivered, not '
          'mistaken for a duplicate', ok and 'after restart' in net.texts(1),
          repr(net.texts(1)))
    check('A noticed the restart',
          any('restarted' in m.get('text', '')
              for m in net.nodes[1].messages))


def t_rekey(root):
    print('\n--- rekeying under load (forward secrecy in practice)')
    saved = S.REKEY_AFTER
    S.REKEY_AFTER = 1.0
    try:
        net = Net(root, loss=0.05)
        data = os.urandom(60000)
        mf = net.nodes[1].submit('file', name='long.bin', mime='', data=data)
        net.run(300, until=lambda n: state(n.nodes[1], mf) == 'delivered')
        a = net.nodes[1].sec
        got_path = os.path.join(net.root, 'store2', 'rx_long.bin')
        with open(got_path, 'rb') as fh:
            got = fh.read()
        check('several sessions were used during one transfer',
              a.peers[2].established >= 3, '%d sessions' % a.peers[2].established)
        check('the file survived every key change intact', got == data)
    finally:
        S.REKEY_AFTER = saved


def t_forward_secrecy(root):
    print('\n--- forward secrecy: stolen static keys vs a recording')
    net = Net(root)
    mid = net.nodes[1].submit('text', text='recorded then keys stolen')
    net.run(10, until=lambda n: state(n.nodes[1], mid) == 'delivered')
    p = net.nodes[1].sec.peers[2]
    check('no ephemeral private key is kept once the handshake is done',
          p.init is None and all(
              not hasattr(s, 'e_priv') for s in net.nodes[1].sec.by_idx.values()))
    # The attacker has both private keys and every frame on air.
    ks_a = S.KeyStore(net.keys[1], 1)
    ks_b = S.KeyStore(net.keys[2], 2)
    init = [r for _, me, r in net.air_log
            if L.parse(r)['type'] == S.FT_HS_INIT]
    frames = net.sealed_frames(src=1)
    opened = 0
    for raw in init:
        pl = L.parse(raw)['payload']
        ini, rsp = pl[1], pl[2]
        stat = {ini: ks_a, rsp: ks_b}
        try:
            # Replay the responder side with the stolen key; the attacker has
            # to supply SOME responder ephemeral - it never saw the real one.
            _, _, k_send, k_recv, _ = S.kk_responder(
                S.prologue('bpsk-lab', ini, rsp), stat[rsp].private,
                stat[rsp].public, stat[ini].public, pl[3:], lambda x: b'')
        except C.AuthError:
            continue
        for _, raw2, f in frames:
            head = f['payload'][:15]
            ctr = struct.unpack('!Q', head[7:15])[0]
            for k in (k_send, k_recv):
                try:
                    C.aead_decrypt(k, S._nonce(ctr), f['payload'][15:], head)
                    opened += 1
                except C.AuthError:
                    pass
    check('with both static keys and the full recording, no transport '
          'frame can be opened', opened == 0 and frames,
          '%d frames tried' % len(frames))


def t_lossy_handshake(root):
    print('\n--- handshake over a bad channel')
    lost = {'n': 0}

    def drop(me, frm, t):
        if frm['type'] in (S.FT_HS_INIT, S.FT_HS_RESP) and lost['n'] < 5:
            lost['n'] += 1
            return True
        return False
    net = Net(root, drop=drop)
    mid = net.nodes[1].submit('text', text='sent before any session')
    ok = net.run(30, until=lambda n: state(n.nodes[1], mid) == 'delivered')
    check('five lost handshake frames are recovered by retries', ok,
          '%.1f s' % net.t)
    check('the queued message waited and was delivered, never sent in clear',
          ok and net.texts(2) == ['sent before any session'])


def t_plaintext_refused(root):
    print('\n--- strict mode: unencrypted frames are refused')
    net = Net(root)
    net.run(5, until=lambda n: n.secure(1, 2))
    b = net.nodes[2]
    for f in [L.build(L.FT_DATA, 2, 1, 50, 0, b'plain from an r5.2 node', 0, 1),
              L.build(L.FT_ACK, 2, 1, 0, 3, b'', 0, 3),
              L.build(L.FT_FILE_START, 2, 1, 60, 0,
                      L.pack_file_start(1, 1, 'x', b''), 0, 0)]:
        net.inject(2, f)
    net.run(0.3)
    check('plaintext DATA / ACK / FILE_START all dropped',
          b.sec.stats['plain_dropped'] == 3 and not net.texts(2))
    check('the operator is told why',
          any('UNENCRYPTED' in line for _, line in b.logs))


def main():
    root = tempfile.mkdtemp(prefix='bpsk_secure_')
    print('crypto backend: %s' % C.BACKEND)
    try:
        for fn in (t_establish, t_staggered, t_confidentiality, t_tamper, t_replay,
                   t_third_node, t_not_in_roster, t_restart, t_rekey,
                   t_forward_secrecy, t_lossy_handshake, t_plaintext_refused):
            fn(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print('\n%s' % ('all security checks passed' if not FAILURES
                    else 'FAILED: ' + ', '.join(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == '__main__':
    sys.exit(main())
