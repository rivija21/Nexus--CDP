"""Regression tests for the defects found in r5.1 and fixed in r5.2.

Each check reproduces one failure observed on r5.1 with the same scenario, so a
PASS here is evidence the defect is gone, not just that the code path runs.
No GNU Radio, no radio: two ChatApp nodes on a simulated channel whose modem
model respects each node's modelled transmit backlog.

    python3 test_regressions.py
"""

import importlib.util
import io
import contextlib
import os
import random
import shutil
import socket
import struct
import sys
import tempfile
import types
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bpsk_app as app                                          # noqa: E402
import bpsk_link as L                                           # noqa: E402

FAILURES = []


def check(label, condition, detail=''):
    print('  [%s] %s%s' % ('PASS' if condition else 'FAIL', label,
                           (' - ' + detail) if detail else ''))
    if not condition:
        FAILURES.append(label)


def blob(n, seed):
    rng = random.Random(seed)
    return bytes(rng.getrandbits(8) for _ in range(n))


class Sim(object):
    """Two nodes, 20 ms propagation, frames leave after the modelled backlog."""

    def __init__(self, root, drop=None, **kw):
        random.seed(1)
        opts = dict(ack_timeout=0.5, max_retries=5, frag_size=256,
                    sym_rate=250000.0, overhead=460)
        opts.update(kw)
        self.tmp = tempfile.mkdtemp(prefix='bpsk_reg_', dir=root)
        self.A = app.ChatApp(1, 2, 'A', os.path.join(self.tmp, 'a'), **opts)
        self.B = app.ChatApp(2, 1, 'B', os.path.join(self.tmp, 'b'), **opts)
        self.nodes = {1: self.A, 2: self.B}
        self.air, self.t, self.drop = [], 0.0, drop

    def _pump(self, me, actions):
        for kind, frame in actions:
            if kind != 'tx':
                continue
            if self.drop is not None:
                try:
                    if self.drop(me, L.parse(frame), self.t):
                        continue
                except L.BadFrame:
                    pass
            leave = max(self.t, self.nodes[me].link.busy_until)
            self.air.append((leave + 0.02, 2 if me == 1 else 1, frame))

    def run(self, seconds, until=None):
        end, step = self.t + seconds, 0
        while self.t < end:
            self.t = round(self.t + 0.001, 6)
            due = sorted((x for x in self.air if x[0] <= self.t),
                         key=lambda x: x[0])
            self.air = [x for x in self.air if x[0] > self.t]
            for _, dest, frame in due:
                self._pump(dest, self.nodes[dest].on_rx(frame, self.t))
            if step % 10 == 0:
                for addr, node in self.nodes.items():
                    self._pump(addr, node.on_tick(self.t))
            step += 1
            if until is not None and step % 50 == 0 and until(self):
                return True
        return False

    def read(self, node, name):
        path = os.path.join(self.tmp, node, name)
        if not os.path.exists(path):
            return None
        with open(path, 'rb') as fh:
            return fh.read()


def finished(node, mid):
    return lambda sim: node(sim)._by_mid[mid]['state'] in ('delivered',
                                                           'failed')


def frag_index(frm):
    return struct.unpack('!H', frm['payload'][:2])[0]


# ---------------------------------------------------------------------------
def t_bidirectional(root):
    print('\n--- files in both directions at once (perfect channel)')
    s = Sim(root)
    small, big = blob(20000, 1), blob(200000, 2)
    ma = s.A.submit('file', name='small.bin', mime='', data=small)
    mb = s.B.submit('file', name='big.bin', mime='', data=big)
    s.run(400, until=lambda x: finished(lambda y: y.A, ma)(x)
          and finished(lambda y: y.B, mb)(x))
    check('small file reported delivered while the peer is still sending',
          s.A._by_mid[ma]['state'] == 'delivered',
          '%s %s' % (s.A._by_mid[ma]['state'], s.A._by_mid[ma].get('note')
                     or ''))
    check('small file intact at the peer', s.read('b', 'rx_small.bin') == small)
    check('big file delivered and intact',
          s.B._by_mid[mb]['state'] == 'delivered'
          and s.read('a', 'rx_big.bin') == big)
    check('no spurious "asking again" rounds',
          s.A.link.stats['file_rounds'] == 0,
          '%d rounds' % s.A.link.stats['file_rounds'])


def t_chat_during_upload(root):
    print('\n--- a chat line typed during an upload')
    s = Sim(root)
    mf = s.A.submit('file', name='up.jpg', mime='image/jpeg',
                    data=blob(200000, 5))
    s.run(2.0)
    mt = s.A.submit('text', text='quick question while that uploads')
    t0 = s.t
    s.run(30, until=lambda x: x.A._by_mid[mt]['state'] == 'delivered')
    check('text overtakes the queued file fragments',
          s.A._by_mid[mt]['state'] == 'delivered' and s.t - t0 < 2.0,
          'delivered after %.1f s (r5.1: 114 s)' % (s.t - t0))
    s.run(200, until=finished(lambda y: y.A, mf))
    check('the upload still completes', s.A._by_mid[mf]['state'] == 'delivered')


def t_lost_verdict(root):
    print('\n--- FILE_DONE lost six times in a row')
    lost = {'n': 0}

    def drop(src, frm, t):
        if src == 2 and frm['type'] == L.FT_FILE_DONE and lost['n'] < 6:
            lost['n'] += 1
            return True
        return False
    s = Sim(root, drop=drop)
    data = blob(5000, 21)
    mid = s.A.submit('file', name='v.bin', mime='', data=data)
    s.run(120, until=finished(lambda y: y.A, mid))
    check('receiver repeats its verdict when asked again',
          s.A._by_mid[mid]['state'] == 'delivered',
          '%s %s' % (s.A._by_mid[mid]['state'],
                     s.A._by_mid[mid].get('note') or ''))
    check('file intact at the receiver', s.read('b', 'rx_v.bin') == data)


def t_stale_verdict():
    print('\n--- a late FILE_DONE must not complete the next transfer')
    lk = L.LinkState(1, 2)
    lk.send_file('second.bin', blob(3000, 3), 0.0, mid=7)
    stale = struct.pack('!BI', 0, 0x12345678)       # OK verdict, other CRC
    out = []
    lk._on_done(out, stale)
    check('stale OK verdict ignored', lk.tx_file is not None
          and not any(a[0] == 'evt' and a[1].get('e') == 'tx_file_done'
                      for a in out))
    out = []
    lk._on_done(out, struct.pack('!BI', 0, lk.tx_file['crc']))
    check('matching verdict still completes it', lk.tx_file is None
          and any(a[0] == 'evt' and a[1].get('e') == 'tx_file_done'
                  and a[1].get('ok') for a in out))


def t_purge_keeps_replies():
    print("\n--- restarting our transfer keeps the replies owed to the peer")
    lk = L.LinkState(1, 2)
    # Our own transfer is under way (its FILE_START is the frame in flight) ...
    lk.send_file('mine.bin', blob(3000, 4), 0.0, mid=9)
    # ... while the peer sends us a 3-fragment file and fragment 1 goes missing.
    lk.on_rx(L.build(L.FT_FILE_START, 1, 2, 0, 0,
                     L.pack_file_start(600, 3, 'peer.bin', b'{"i":1}'), 0, 0),
             0.0)
    lk.on_rx(L.build(L.FT_FILE_DATA, 1, 2, 1, 0, b'\x00\x00' + b'x' * 254,
                     0, 1), 0.0)
    lk.on_rx(L.build(L.FT_FILE_END, 1, 2, 2, 0, b'\x00' * 4, 0, 2), 0.0)
    # Now our FILE_START is dropped after max_retries and the transfer restarts.
    lk.pending = None
    lk._restart_file([])
    labels = [i['label'] for i in lk.txq_hi]
    check('FILE_NACK for the peer survives our restart', 'file-nack' in labels,
          repr(labels))
    check('our own transfer was requeued', any(
        i['label'] == 'file-start' for i in lk.txq))


def t_abandoned_same_name(root):
    print('\n--- a new "image.png" after an abandoned one')
    phase = {'p': 1, 'lost': 0}

    def drop(src, frm, t):
        if src != 1:
            return False
        if phase['p'] == 1:
            return (frm['type'] == L.FT_FILE_END
                    or (frm['type'] == L.FT_FILE_DATA and frag_index(frm) >= 5))
        if (frm['type'] == L.FT_FILE_DATA and frag_index(frm) == 2
                and phase['lost'] < 6):
            phase['lost'] += 1
            return True
        return False
    s = Sim(root, drop=drop)
    s.A.link.max_retries, s.A.link.max_file_rounds = 1, 1
    old, new = blob(20000, 7), blob(20000, 8)
    mo = s.A.submit('file', name='image.png', mime='image/png', data=old)
    s.run(300, until=finished(lambda y: y.A, mo))
    phase['p'] = 2
    s.A.link.max_retries, s.A.link.max_file_rounds = 5, 8
    mn = s.A.submit('file', name='image.png', mime='image/png', data=new)
    s.run(300, until=finished(lambda y: y.A, mn))
    check('second transfer repaired instead of corrupted',
          s.A._by_mid[mn]['state'] == 'delivered',
          '%s %s' % (s.A._by_mid[mn]['state'],
                     s.A._by_mid[mn].get('note') or ''))
    bubbles = [m['state'] for m in s.B.messages if m.get('kind') == 'file']
    check('abandoned transfer shown as incomplete, new one as received',
          bubbles == ['incomplete', 'received'], repr(bubbles))


def t_same_name_twice(root):
    print('\n--- two different files with the same name')
    s = Sim(root)
    one, two = blob(3000, 11), blob(3000, 12)
    m1 = s.A.submit('file', name='image.png', mime='image/png', data=one)
    s.run(60, until=lambda x: x.A._by_mid[m1]['state'] == 'delivered')
    m2 = s.A.submit('file', name='image.png', mime='image/png', data=two)
    s.run(60, until=lambda x: x.A._by_mid[m2]['state'] == 'delivered')
    rows = [m for m in s.B.messages if m.get('kind') == 'file']
    paths = [s.B.file_path(r['file']['url'].split('/')[-1]) for r in rows]
    data = []
    for p in paths:
        with open(p, 'rb') as fh:
            data.append(fh.read())
    check('each entry keeps its own file', data == [one, two],
          ', '.join(os.path.basename(p) for p in paths))


def t_failed_part(root):
    print('\n--- multi-frame text whose middle frame never gets through')

    def drop(src, frm, t):
        if src != 1 or frm['type'] != L.FT_DATA:
            return False
        env = app.unpack_envelope(frm['payload'])
        return (env is not None and env['type'] == app.AT_TEXT
                and env['parts'] == 3 and env['part'] == 1)
    s = Sim(root, drop=drop)
    mid = s.A.submit('text', text='x' * 600)
    s.run(20)
    check('entry stays failed after the later parts are acknowledged',
          s.A._by_mid[mid]['state'] == 'failed', s.A._by_mid[mid]['state'])


def t_malformed(root):
    print('\n--- malformed but CRC-valid frames')
    s = Sim(root)
    frames = [L.build(L.FT_FILE_START, 2, 1, 0, 0,
                      L.pack_file_start(10, 1, b'ev\x00il\r\n/../x.png', b'[1]'),
                      0, 0),
              L.build(L.FT_FILE_DATA, 2, 1, 1, 0, b'\x00\x00' + b'0123456789',
                      0, 1),
              L.build(L.FT_FILE_DATA, 2, 1, 2, 0, b'\xff\xff' + b'junk', 0, 2),
              L.build(L.FT_FILE_END, 2, 1, 3, 0, b'\x00' * 4, 0, 3)]
    try:
        for f in frames:
            s.B.on_rx(f, 1.0)
        raised = None
    except Exception as exc:                               # noqa: BLE001
        raised = repr(exc)
    check('NUL / CR-LF / separators in a filename do not raise', raised is None,
          raised or '')
    written = os.listdir(os.path.join(s.tmp, 'b'))
    check('the file lands inside the store under a clean name',
          all('\x00' not in n and '\n' not in n and '/' not in n
              for n in written) and written, repr(written))


def t_names():
    print('\n--- filenames in any script')
    samples = {'ඡායාරූපය.png': 'ඡායාරූපය.png', 'படம்.jpg': 'படம்.jpg',
               'my résumé.pdf': 'my_résumé.pdf', '../../etc/passwd': 'passwd',
               '.bashrc': 'bashrc', 'a\x00b.txt': 'ab.txt'}
    got = {k: app._safe_filename(k) for k in samples}
    check('scripts and extensions survive, separators do not',
          got == samples, repr({k: v for k, v in got.items()
                                if v != samples[k]}))
    long = 'x' * 400 + '.jpeg'
    cut = L.clean_name(long)
    check('long names are cut to the byte limit, extension kept',
          cut.endswith('.jpeg') and len(cut.encode()) <= L.MAX_NAME_BYTES)


def free_port():
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def t_http(root):
    print('\n--- local HTTP endpoint')
    node = app.ChatApp(1, 2, 'A', os.path.join(root, 'http'))
    port = free_port()
    srv, _ = app.serve(node, port)
    base = 'http://127.0.0.1:%d' % port

    def status(path, data=None, headers=None):
        req = urllib.request.Request(base + path, data=data,
                                     headers=headers or {})
        try:
            return urllib.request.urlopen(req, timeout=5).status
        except urllib.error.HTTPError as exc:
            return exc.code

    try:
        check('same-origin send accepted', status(
            '/api/send', b'{"text":"hi"}',
            {'Content-Type': 'application/json',
             'Origin': 'http://127.0.0.1:%d' % port}) == 200)
        check('cross-origin send refused (r5.1: accepted)', status(
            '/api/send', b'{"text":"from a web page"}',
            {'Content-Type': 'text/plain',
             'Origin': 'https://evil.example'}) == 403)
        check('foreign Host header refused (DNS rebinding)', status(
            '/api/state', headers={'Host': 'evil.example:%d' % port}) == 403)
        check('non-object JSON rejected cleanly', status(
            '/api/send', b'[1,2]', {'Content-Type': 'application/json'}) == 400)
        name = 'ඡායාරූපය.png'
        code = status('/api/upload', b'\x89PNG....',
                      {'X-Filename': urllib.request.quote(name),
                       'X-Filetype': 'image/png'})
        job = node._inbox[-1] if node._inbox else {}
        check('percent-encoded Sinhala filename accepted', code == 200
              and job.get('name') == name, repr(job.get('name')))
        node.on_tick(0.0)
        url = [m for m in node.messages if m.get('kind') == 'file'][-1]['file']['url']
        resp = urllib.request.urlopen(base + url, timeout=5)
        cd = resp.headers.get('Content-Disposition', '')
        check('attachment served with RFC 5987 name and a sandbox CSP',
              resp.status == 200 and "filename*=UTF-8''" in cd
              and resp.headers.get('Content-Security-Policy') == 'sandbox', cd)
    finally:
        app.shutdown(srv)
        node.close()


def t_console_and_guard(root):
    print('\n--- GNU Radio block: console output and fault containment')
    pmt = types.ModuleType('pmt')

    class Sym(str):
        pass

    class Pair(object):
        def __init__(self, car, cdr):
            self.car, self.cdr = car, cdr

    class U8(object):
        def __init__(self, data):
            self.data = bytes(data)
    pmt.PMT_NIL = object()
    pmt.intern = Sym
    pmt.string_to_symbol = Sym
    pmt.is_symbol = lambda o: isinstance(o, Sym)
    pmt.symbol_to_string = str
    pmt.is_pair = lambda o: isinstance(o, Pair)
    pmt.cdr = lambda o: o.cdr
    pmt.cons = Pair
    pmt.is_u8vector = lambda o: isinstance(o, U8)
    pmt.u8vector_elements = lambda o: list(o.data)
    pmt.init_u8vector = lambda n, lst: U8(bytes(lst))
    gr = types.ModuleType('gnuradio.gr')

    class sync_block(object):
        def __init__(self, name='', in_sig=None, out_sig=None):
            self.published = []

        def message_port_register_in(self, port):
            pass

        def message_port_register_out(self, port):
            pass

        def set_msg_handler(self, port, fn):
            pass

        def message_port_pub(self, port, msg):
            self.published.append((str(port), msg))
    gr.sync_block = sync_block
    pkg = types.ModuleType('gnuradio')
    pkg.gr = gr
    saved = {k: sys.modules.get(k) for k in ('pmt', 'gnuradio', 'gnuradio.gr')}
    sys.modules.update({'pmt': pmt, 'gnuradio': pkg, 'gnuradio.gr': gr})
    try:
        spec = importlib.util.spec_from_file_location(
            'shim_reg', os.path.join(HERE, 'bpsk_duplex_pluto_link_layer.py'))
        shim = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(shim)
        blk = shim.blk(my_addr=1, peer_addr=2, http_port=1, open_ui=False,
                       rx_dir=os.path.join(root, 'shim'))
        blk._ensure_running = lambda: None          # no server, no watchdog
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            blk._on_chat(pmt.intern('/stats'))
        check('/stats typed in the GNU Radio console prints on the terminal',
              'tx frames=' in buf.getvalue())

        def boom(raw, now):
            raise ValueError('synthetic fault')
        blk.app.on_rx = boom
        err = io.StringIO()
        try:
            with contextlib.redirect_stdout(err), contextlib.redirect_stderr(err):
                blk._on_rx(Pair(None, U8(b'\x00' * 20)))
            escaped = None
        except Exception as exc:                           # noqa: BLE001
            escaped = repr(exc)
        check('a fault in a handler is contained (no process abort)',
              escaped is None, escaped or '')
        check('... and its traceback is still printed',
              'synthetic fault' in err.getvalue()
              and 'Traceback' in err.getvalue())
        blk.stop()
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def main():
    root = tempfile.mkdtemp(prefix='bpsk_regressions_')
    try:
        t_bidirectional(root)
        t_chat_during_upload(root)
        t_lost_verdict(root)
        t_stale_verdict()
        t_purge_keeps_replies()
        t_abandoned_same_name(root)
        t_same_name_twice(root)
        t_failed_part(root)
        t_malformed(root)
        t_names()
        t_http(root)
        t_console_and_guard(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print('\n%s' % ('all regression checks passed' if not FAILURES
                    else 'FAILED: ' + ', '.join(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == '__main__':
    sys.exit(main())
