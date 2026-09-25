"""Two-node loopback test for the BPSK link and application layers.

No GNU Radio, no radio hardware: two full ChatApp nodes are wired together
through a lossy, delayed virtual channel and driven with simulated time. It
exercises what the over-the-air link is actually asked to do - multi-frame
text in both directions, an image transfer that loses fragments and has to be
repaired, and the delivery receipts the UI draws on the chat bubbles.

    python3 test_bpsk_link.py
"""

import os
import random
import shutil
import sys
import tempfile
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bpsk_app as app                                          # noqa: E402
import bpsk_link as L                                           # noqa: E402

random.seed(7)
LOSS = 0.15          # fraction of frames the channel destroys
PROP = 0.02          # one-way propagation + processing delay, seconds
TICK = 0.010         # how often on_tick is driven, seconds

FAILURES = []


def check(label, condition, detail=''):
    mark = 'PASS' if condition else 'FAIL'
    print('  [%s] %s%s' % (mark, label, (' - ' + detail) if detail else ''))
    if not condition:
        FAILURES.append(label)


def make_png(width=64, height=64):
    """A real, decodable PNG so the MIME path and the preview are honest."""
    def chunk(tag, payload):
        body = tag + payload
        return (len(payload).to_bytes(4, 'big') + body
                + (zlib.crc32(body) & 0xFFFFFFFF).to_bytes(4, 'big'))

    raw = bytearray()
    for y in range(height):
        raw.append(0)                                   # filter type 0
        for x in range(width):
            raw += bytes(((x * 4) % 256, (y * 4) % 256,
                          ((x ^ y) * 3) % 256))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', width.to_bytes(4, 'big') + height.to_bytes(4, 'big')
                    + bytes((8, 2, 0, 0, 0)))
            + chunk(b'IDAT', zlib.compress(bytes(raw), 6))
            + chunk(b'IEND', b''))


def main():
    tmp = tempfile.mkdtemp(prefix='bpsk_test_')
    a_dir, b_dir = os.path.join(tmp, 'a'), os.path.join(tmp, 'b')

    A = app.ChatApp(my_addr=1, peer_addr=2, nick='Node A', store_dir=a_dir,
                    ack_timeout=0.2, max_retries=8, frag_size=64,
                    sym_rate=250000.0)
    B = app.ChatApp(my_addr=2, peer_addr=1, nick='Node B', store_dir=b_dir,
                    ack_timeout=0.2, max_retries=8, frag_size=64,
                    sym_rate=250000.0)
    nodes = {1: A, 2: B}

    air = []                       # (deliver_time, dest_addr, frame)
    now = [0.0]

    def pump(node, me, actions):
        for kind, value in actions:
            if kind == 'tx' and random.random() > LOSS:
                air.append((now[0] + PROP, 2 if me == 1 else 1, value))

    picture = make_png()
    long_text = ('Bench log: FLL locked at 45 dB Rx gain, Costas residual '
                 'under two degrees, symbol sync holding at 4 sps. This line '
                 'is deliberately longer than one 64-byte fragment so the '
                 'application layer has to split it across several DATA '
                 'frames and reassemble them in order at the far end.')

    mid_text = A.submit('text', text=long_text)
    mid_file = A.submit('file', name='bench.png', mime='image/png',
                        data=picture)
    B.submit('text', text='Node B here, receiver is locked.')

    got_file = os.path.join(b_dir, 'rx_bench.png')
    steps = 0
    for steps in range(400000):
        now[0] = round(now[0] + 0.001, 6)
        due = [x for x in air if x[0] <= now[0]]
        air[:] = [x for x in air if x[0] > now[0]]
        for _, dest, frame in due:
            node = nodes[dest]
            pump(node, dest, node.on_rx(frame, now[0]))
        if steps % int(TICK * 1000) == 0:
            for addr, node in nodes.items():
                pump(node, addr, node.on_tick(now[0]))
        done = (os.path.exists(got_file)
                and A._by_mid[mid_file]['state'] in ('delivered', 'failed')
                and A._by_mid[mid_text]['state'] in ('delivered', 'failed')
                and any(m['dir'] == 'in' and m.get('kind') == 'text'
                        for m in A.messages))
        if done:
            break

    print('\nsimulated %.2f s of link time at %.0f%% frame loss\n'
          % (now[0], LOSS * 100))

    # ---------------------------------------------------------------- text
    rx_text = [m for m in B.messages if m['dir'] == 'in' and m['kind'] == 'text']
    check('multi-frame text arrives intact',
          any(m['text'] == long_text for m in rx_text),
          '%d inbound text messages' % len(rx_text))
    check('sender nickname travels with the message',
          any(m['nick'] == 'Node A' for m in rx_text),
          B.peer_nick)
    check('outbound text ends up acknowledged',
          A._by_mid[mid_text]['state'] == 'delivered',
          A._by_mid[mid_text]['state'])
    check('reverse direction works while a file is in flight',
          any(m['dir'] == 'in' and 'receiver is locked' in m.get('text', '')
              for m in A.messages))

    # ---------------------------------------------------------------- file
    check('image file written by the receiver', os.path.exists(got_file))
    if os.path.exists(got_file):
        with open(got_file, 'rb') as fh:
            data = fh.read()
        check('image is byte-identical after repair rounds',
              data == picture, '%d of %d bytes' % (len(data), len(picture)))
    rx_file = [m for m in B.messages if m['dir'] == 'in' and m['kind'] == 'file']
    check('receiver shows the attachment as a chat bubble', bool(rx_file))
    if rx_file:
        f = rx_file[-1]
        check('attachment MIME type survives the link',
              f['file']['mime'] == 'image/png', f['file']['mime'])
        check('attachment is served to the browser',
              bool(f['file']['url']), str(f['file']['url']))
        check('receiver reports a CRC verdict', f['state'] == 'received',
              f['state'])
        check('progress reached every fragment',
              f['progress']['done'] == f['progress']['total'],
              '%s/%s' % (f['progress']['done'], f['progress']['total']))
    sent = A._by_mid[mid_file]
    check('sender bubble reaches delivered',
          sent['state'] == 'delivered', sent['state'])

    # ------------------------------------------------------------ receipts
    check('delivery states are UI-renderable',
          all(m.get('state') in ('queued', 'sent', 'sending', 'delivered',
                                 'failed', 'received', 'receiving', 'corrupt',
                                 'info')
              for m in A.messages + B.messages))
    check('telemetry snapshot is JSON-serialisable', _json_ok(A))

    # ---------------------------------------------- backward compatibility
    legacy = L.build(L.FT_DATA, 2, 1, 9, 0, b'plain r4 text', 0, 9)
    before = len(B.messages)
    B.on_rx(legacy, now[0])
    check('an un-upgraded node still shows up in the transcript',
          len(B.messages) > before
          and any(m.get('legacy') for m in B.messages))

    print('\nA: %s' % A.link.stats_line(now[0]))
    print('B: %s' % B.link.stats_line(now[0]))
    s = A.stats_payload(now[0])
    print('\nA goodput tx=%.1f kb/s rx=%.1f kb/s, frame error rate %.2f%%, '
          'repair rounds %d' % (s['tx_bps'] / 1000, s['rx_bps'] / 1000,
                                s['per'], s['file_rounds']))

    shutil.rmtree(tmp, ignore_errors=True)
    print('\n%s' % ('all checks passed' if not FAILURES
                    else 'FAILED: ' + ', '.join(FAILURES)))
    return 1 if FAILURES else 0


def _json_ok(node):
    import json
    try:
        json.dumps(node.state_payload(), default=str)
        return True
    except (TypeError, ValueError):
        return False


if __name__ == '__main__':
    sys.exit(main())
