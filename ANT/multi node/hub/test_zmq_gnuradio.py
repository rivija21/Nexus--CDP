#!/usr/bin/env python3
"""r7 in GNU Radio: three node processes and the air, over ZMQ, in real time.

    python3 test_zmq_gnuradio.py          (needs GNU Radio with gr-zeromq)

Each node is a separate GNU Radio process running the BPSK MAC Node block -
the same file the .grc embeds - behind the flowgraph's RRC filter, in its own
directory with its own keys. bpsk_zmq_air.py is the channel. Everything is
driven through each node's HTTP API, as the browser would: the nodes must
elect a hub, range, carry a chat line and a file, and survive the hub being
killed.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FAILURES = []
PY = sys.executable
BASE = 5700
PORTS = {1: 8191, 2: 8192, 3: 8193}


def check(label, cond, detail=''):
    print('  [%s] %s%s' % ('PASS' if cond else 'FAIL', label,
                           (' - ' + detail) if detail else ''), flush=True)
    if not cond:
        FAILURES.append(label)


def api(addr, path, body=None, headers=None, timeout=3.0):
    url = 'http://127.0.0.1:%d%s' % (PORTS[addr], path)
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        hdrs.setdefault('Content-Type', 'application/json')
    req = urllib.request.Request(url, data=data, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def mac(addr):
    try:
        return api(addr, '/api/stats').get('mac') or {}
    except Exception:
        return {}


def wait(cond, timeout, step=0.5):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(step)
    return False


def main():
    try:
        import gnuradio.zeromq                       # noqa: F401
    except ImportError:
        print('GNU Radio with gr-zeromq is needed for this test - skipped')
        return 0
    import bpsk_keys
    root = tempfile.mkdtemp(prefix='r7zmq_')
    dirs = {a: os.path.join(root, 'node%d' % a) for a in PORTS}
    for d in dirs.values():
        os.makedirs(d)
    bpsk_keys.provision({a: os.path.join(d, 'keys') for a, d in dirs.items()},
                        network='zmq-bench')
    procs = {}
    logs = {}
    air_log = open(os.path.join(root, 'air.log'), 'w')
    air = subprocess.Popen([PY, '-u', os.path.join(HERE, 'bpsk_zmq_air.py'),
                            '--nodes', '3', '--base', str(BASE)],
                           stdout=air_log, stderr=subprocess.STDOUT)
    time.sleep(0.5)

    def start(a):
        logs[a] = open(os.path.join(dirs[a], 'node.log'), 'w')
        procs[a] = subprocess.Popen(
            [PY, '-u', os.path.join(HERE, 'bpsk_zmq_node.py'),
             '--index', str(a - 1), '--base', str(BASE), '--my-addr', str(a),
             '--http-port', str(PORTS[a])],
            cwd=dirs[a], stdout=logs[a], stderr=subprocess.STDOUT)
    t0 = time.time()
    try:
        for a in PORTS:
            start(a)
        print('\n--- three GNU Radio processes and the air', flush=True)

        def joined(alive):
            ms = {a: mac(a) for a in alive}
            hubs = [a for a, m in ms.items() if m.get('role') == 'hub']
            if len(hubs) != 1:
                return False
            return all(ms[a].get('admitted') for a in alive if a != hubs[0])
        ok = wait(lambda: joined(PORTS), 40)
        ms = {a: mac(a) for a in PORTS}
        hub = [a for a, m in ms.items() if m.get('role') == 'hub']
        check('one hub elected and both members admitted after %.0f s (%s)'
              % (time.time() - t0, hub), ok and len(hub) == 1)
        if not ok:
            return 1
        hub = hub[0]
        a, b = [x for x in PORTS if x != hub]
        print('\n--- chat and a file through the HTTP API', flush=True)
        r = api(a, '/api/send', {'text': 'hello through gnuradio', 'to': b})
        mid = r.get('id')

        def got_text(addr, text):
            st = api(addr, '/api/state')
            return any(m.get('text') == text and m.get('dir') == 'in'
                       for m in st['messages'])

        def msg_state(addr, mid):
            st = api(addr, '/api/state')
            for m in st['messages']:
                if m.get('id') == mid:
                    return m.get('state')
            return None
        ok = wait(lambda: got_text(b, 'hello through gnuradio'), 20)
        check('member %d -> member %d chat arrives' % (a, b), ok)
        ok = wait(lambda: msg_state(a, mid) == 'delivered', 10)
        check('and the sender sees it delivered', ok)
        data = os.urandom(25000)
        r = api(b, '/api/upload', data, headers={
            'Content-Type': 'application/octet-stream',
            'X-Filename': 'zmq.bin', 'X-To': str(hub)})
        fid = r.get('id')
        t1 = time.time()
        ok = wait(lambda: msg_state(b, fid) == 'delivered', 40)
        path = os.path.join(dirs[hub], 'chat_files', 'rx_zmq.bin')
        intact = os.path.exists(path) and open(path, 'rb').read() == data
        check('25 kB file member -> hub delivered and intact (%.1f s)'
              % (time.time() - t1), ok and intact)

        # A node that falls behind real time underflows its TX stream in the
        # air (as a Pluto would): its loop latency steps by a chunk, and
        # ranging must pull it back. Poll a few bursts for convergence.
        seen = []

        def ranged():
            for x in (a, b):
                api(x, '/api/send', {'text': 'tick', 'to': hub})
            time.sleep(1.5)
            errs = {k: v['err'] for k, v in mac(hub).get('members', {}).items()}
            seen.append(errs)
            return errs and all(e is not None and abs(e) <= 8
                                for e in errs.values())
        ok = wait(ranged, 20, step=0.1)
        print('     timing errors of the last bursts at the hub: %s samples'
              % seen, flush=True)
        check('members land within +-8 samples of their slots under the '
              'GNU Radio scheduler', ok)

        print('\n--- kill the hub process', flush=True)
        procs[hub].send_signal(signal.SIGTERM)
        procs[hub].wait(timeout=10)
        alive = [x for x in PORTS if x != hub]
        t1 = time.time()
        ok = wait(lambda: joined(alive), 30)
        new = [x for x in alive if mac(x).get('role') == 'hub']
        check('a new hub (%s) took over in %.0f s' % (new, time.time() - t1),
              ok and len(new) == 1)
        if ok:
            other = [x for x in alive if x not in new][0]
            api(other, '/api/send', {'text': 'after failover', 'to': new[0]})
            ok = wait(lambda: got_text(new[0], 'after failover'), 20)
            check('chat to the new hub', ok)
    finally:
        for p in procs.values():
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
        clean = True
        for a, p in procs.items():
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                clean = False
        air.kill()
        for f in list(logs.values()) + [air_log]:
            f.close()
        outs = {a: open(os.path.join(dirs[a], 'node.log')).read()
                for a in PORTS}
        check('every node process stopped cleanly', clean and all(
            'stopped cleanly' in o for o in outs.values()))
        faults = [a for a, o in outs.items() if 'INTERNAL ERROR' in o]
        check('no internal errors in any node log', not faults, str(faults))
        if FAILURES:
            print('\nlogs kept in %s' % root)
            for a, o in outs.items():
                print('---- node %d (last 25 lines)' % a)
                print('\n'.join(o.splitlines()[-25:]))
        else:
            shutil.rmtree(root, ignore_errors=True)
    print()
    if FAILURES:
        print('FAILED: %d check(s)' % len(FAILURES))
        return 1
    print('all GNU Radio / ZMQ checks passed (%.0f s)' % (time.time() - t0))
    return 0


if __name__ == '__main__':
    sys.exit(main())
