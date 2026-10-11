"""r5.2 regressions, carried over to the r7 multi-peer link and app.

    python3 test_r7_regressions.py

r5.2 fixed a list of defects (J-V in the r5 README), each with a reproduction.
The r7 link and app were restructured for many peers, so every one of those
reproductions is re-run here against r7. Frames go directly between the apps
(no MAC: these are link/app properties), in plaintext so a test can drop
frames by type, plus the HTTP checks against a live server.
"""

import json
import os
import shutil
import struct
import sys
import tempfile
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bpsk_app as app                                          # noqa: E402
import bpsk_link as L                                           # noqa: E402

FAILURES = []


def check(label, cond, detail=''):
    print('  [%s] %s%s' % ('PASS' if cond else 'FAIL', label,
                           (' - ' + detail) if detail else ''))
    if not cond:
        FAILURES.append(label)


class Direct(object):
    """Apps wired back to back: every 10 ms each pulls what it may send."""

    def __init__(self, root, addrs=(1, 2), budget=1400, drop=None):
        self.apps = {}
        for a in addrs:
            self.apps[a] = app.ChatApp(a, 'Node %d' % a,
                                       os.path.join(root, 's%d' % a),
                                       security=False)
        self.t = 0.0
        self.budget = budget
        self.drop = drop
        self.log = []

    def store(self, a):
        return self.apps[a].store_dir

    def run(self, seconds, until=None):
        end = self.t + seconds
        while self.t < end:
            self.t = round(self.t + 0.01, 6)
            for a, ap in self.apps.items():
                ap.on_tick(self.t)
                for raw in ap.mac_pull(self.budget, self.t):
                    frm = L.parse(raw)
                    self.log.append((self.t, a, frm))
                    if self.drop is not None and self.drop(a, frm, self.t):
                        continue
                    dst = self.apps.get(frm['dst'])
                    if dst is not None:
                        dst.mac_rx(frm, self.t)
            if until is not None and until(self):
                return True
        return False


def state(ap, mid):
    return ap._by_mid.get(mid, {}).get('state')


def rx_bytes(d, a, name):
    p = os.path.join(d.store(a), name)
    return open(p, 'rb').read() if os.path.exists(p) else None


def t_jk(root):
    print('\n--- J/K: replies and chat are not stuck behind a bulk upload')
    d = Direct(os.path.join(root, 'jk'), budget=400)
    a, b = d.apps[1], d.apps[2]
    big1, big2 = os.urandom(400000), os.urandom(15000)
    m1 = a.submit('file', name='up.bin', mime='', data=big1, to=2)
    m2 = b.submit('file', name='down.bin', mime='', data=big2, to=1)
    d.run(3)
    t0 = d.t
    c = a.submit('text', text='typed during the upload', to=2)
    d.run(10, until=lambda x: state(a, c) == 'delivered')
    check('K: chat during a 400 kB upload delivered in %.2f s' % (d.t - t0),
          d.t - t0 < 1.0)
    d.run(120, until=lambda x: state(b, m2) in ('delivered', 'failed'))
    check('J: the smaller transfer the other way completes (%s) while the '
          'upload is still running (%s)' % (state(b, m2), state(a, m1)),
          state(b, m2) == 'delivered' and state(a, m1) != 'delivered')
    d.run(300, until=lambda x: state(a, m1) in ('delivered', 'failed'))
    check('both files intact', rx_bytes(d, 2, 'rx_up.bin') == big1
          and rx_bytes(d, 1, 'rx_down.bin') == big2)


def t_l(root):
    print('\n--- L: FILE_DONE lost six times, the receiver repeats its verdict')
    lost = [0]

    def drop(src, frm, t):
        if src == 2 and frm['type'] == L.FT_FILE_DONE and lost[0] < 6:
            lost[0] += 1
            return True
        return False
    d = Direct(os.path.join(root, 'l'), drop=drop)
    data = os.urandom(3000)
    m = d.apps[1].submit('file', name='l.bin', mime='', data=data, to=2)
    d.run(200, until=lambda x: state(x.apps[1], m) in ('delivered', 'failed'))
    check('sender reports delivered after %d lost verdicts (%s)'
          % (lost[0], state(d.apps[1], m)), state(d.apps[1], m) == 'delivered')


def t_m(root):
    print('\n--- M: a stale OK verdict does not complete the next file')
    d = Direct(os.path.join(root, 'm'))
    a = d.apps[1]
    m = a.submit('file', name='first.bin', mime='', data=os.urandom(2000), to=2)
    d.run(30, until=lambda x: state(a, m) == 'delivered')
    m2 = a.submit('file', name='second.bin', mime='', data=os.urandom(40000),
                  to=2)
    d.run(0.3)
    stale = struct.pack('!BI', 0, 0x12345678)
    a.mac_rx({'type': L.FT_FILE_DONE, 'dst': 1, 'src': 2, 'seq': 0, 'ack': 0,
              'flags': 1 << L.EPOCH_SHIFT, 'payload': stale}, d.t)
    check('second file not marked delivered by it (%s)' % state(a, m2),
          state(a, m2) != 'delivered')
    d.run(60, until=lambda x: state(a, m2) == 'delivered')
    check('and it completes normally', state(a, m2) == 'delivered')


def t_p_r(root):
    print('\n--- P/R: same-named files, hostile names and metadata')
    d = Direct(os.path.join(root, 'p'))
    a = d.apps[1]
    x, y = os.urandom(900), os.urandom(901)
    m1 = a.submit('file', name='image.png', mime='image/png', data=x, to=2)
    d.run(30, until=lambda q: state(a, m1) == 'delivered')
    m2 = a.submit('file', name='image.png', mime='image/png', data=y, to=2)
    d.run(30, until=lambda q: state(a, m2) == 'delivered')
    check('P: two image.png kept apart', rx_bytes(d, 2, 'rx_image.png') == x
          and rx_bytes(d, 2, 'rx_image-2.png') == y)
    b = d.apps[2]
    evil = L.pack_file_start(10, 1, 'a\x00b/../../etc.txt', b'[1,2,3]')
    try:
        b.link.rx[1]['base']
        base = b.link.rx[1]['base']
        b.mac_rx({'type': L.FT_FILE_START, 'dst': 2, 'src': 1, 'seq': base,
                  'ack': base, 'flags': a.link.epoch << L.EPOCH_SHIFT,
                  'payload': evil}, d.t)
        ok = True
    except Exception as exc:                          # noqa: BLE001
        ok = False
        print('     raised %r' % exc)
    check('R: NUL and separators in a name, JSON that is not an object: no '
          'exception', ok)
    names = [m['file']['name'] for m in b.messages if m.get('kind') == 'file']
    check('R: the name was cleaned (%s)' % names[-1:],
          names and '\x00' not in names[-1] and '/' not in names[-1])


def t_q(root):
    print('\n--- Q: a multi-frame message that lost a part stays failed')
    d = Direct(os.path.join(root, 'q'),
               drop=lambda s, f, t: s == 1 and f['type'] == L.FT_DATA
               and f['seq'] == 1)
    a = d.apps[1]
    text = 'x' * 600                                     # three frames
    m = a.submit('text', text=text, to=2)
    d.run(60, until=lambda x: state(a, m) == 'failed')
    d.run(3)
    check('state stays failed after later parts are acknowledged (%s)'
          % state(a, m), state(a, m) == 'failed')


def t_http(root):
    print('\n--- T/U: the HTTP front end')
    ap = app.ChatApp(1, 'Node 1', os.path.join(root, 'http'), security=False)
    srv, _ = app.serve(ap, 0)
    port = srv.server_address[1]

    def req(path, body=None, headers=None, method=None):
        r = urllib.request.Request('http://127.0.0.1:%d%s' % (port, path),
                                   data=body, headers=headers or {},
                                   method=method)
        try:
            with urllib.request.urlopen(r, timeout=3) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
    try:
        st, _ = req('/api/state', headers={'Host': 'evil.example'})
        check('U: foreign Host refused', st == 403)
        for path in ('/api/send', '/api/select'):
            st, _ = req(path, json.dumps({'text': 'x', 'peer': 2, 'to': 2})
                        .encode(), {'Content-Type': 'application/json',
                                    'Origin': 'http://evil.example'})
            check('U: cross-origin POST %s refused' % path, st == 403)
        st, body = req('/api/send', json.dumps({'text': 'hi', 'to': 2})
                       .encode(), {'Content-Type': 'application/json'})
        check('U: same-origin send accepted', st == 200)
        name = 'ඡායාරූපය.png'
        st, body = req('/api/upload', b'\x89PNG' + os.urandom(100), {
            'X-Filename': urllib.request.quote(name),
            'X-Filetype': 'image/png', 'X-To': '2',
            'Content-Type': 'application/octet-stream'})
        ap.on_tick(0.0)
        files = [m for m in ap.messages if m.get('kind') == 'file']
        check('T: Sinhala filename accepted and kept (%s)'
              % (files[-1]['file']['name'] if files else None),
              st == 200 and files and files[-1]['file']['name'] == name
              and files[-1]['peer'] == 2)
        st, _ = req('/api/select', json.dumps({'peer': 3}).encode(),
                    {'Content-Type': 'application/json'})
        ap.on_tick(0.0)
        check('select a peer through the API', st == 200 and ap.selected == 3)
        st, _ = req('/api/select', json.dumps({'peer': 999}).encode(),
                    {'Content-Type': 'application/json'})
        check('an invalid peer address is refused', st == 400)
    finally:
        app.shutdown(srv)


def main():
    root = tempfile.mkdtemp(prefix='r7reg_')
    try:
        for t in (t_jk, t_l, t_m, t_p_r, t_q, t_http):
            t(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print()
    if FAILURES:
        print('FAILED: %d check(s):' % len(FAILURES))
        for f in FAILURES:
            print('   ' + f)
        return 1
    print('all regression checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
