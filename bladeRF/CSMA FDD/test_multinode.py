"""Four-node test for the r6a per-peer link layer.

Four complete ChatApp nodes on a shared virtual medium: every transmission is
offered to every other node, which is what the r6b CSMA channel will be, and
each node accepts only what is addressed to it or to broadcast. Frames are
lost independently per receiver so the ARQ is genuinely exercised.

What this stage proves: addressing, per-peer ARQ, room fan-out, direct
messages, per-peer file transfer, peer discovery and nickname propagation.

What it does NOT prove: anything about the MAC. There are no collisions here -
contention, carrier sense and backoff are r6b.

    python3 test_multinode.py
"""

import os
import random
import shutil
import sys
import tempfile
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bpsk_app as app                                          # noqa: E402

random.seed(11)
LOSS = 0.15
PROP = 0.02
TICK = 0.010
ADDRS = (1, 2, 3, 4)

FAILURES = []


def check(label, condition, detail=''):
    print('  [%s] %s%s' % ('PASS' if condition else 'FAIL', label,
                           (' - ' + detail) if detail else ''))
    if not condition:
        FAILURES.append(label)


def make_png(width=48, height=48):
    def chunk(tag, payload):
        body = tag + payload
        return (len(payload).to_bytes(4, 'big') + body
                + (zlib.crc32(body) & 0xFFFFFFFF).to_bytes(4, 'big'))
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            raw += bytes(((x * 5) % 256, (y * 5) % 256, ((x + y) * 3) % 256))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', width.to_bytes(4, 'big') + height.to_bytes(4, 'big')
                    + bytes((8, 2, 0, 0, 0)))
            + chunk(b'IDAT', zlib.compress(bytes(raw), 6))
            + chunk(b'IEND', b''))


def conv_texts(node, conv, direction='in'):
    return [m['text'] for m in node.messages
            if m.get('conv') == conv and m['dir'] == direction
            and m['kind'] == 'text']


def main():
    tmp = tempfile.mkdtemp(prefix='bpsk_multi_')
    nodes = {}
    for addr in ADDRS:
        nodes[addr] = app.ChatApp(
            my_addr=addr, peers=[a for a in ADDRS if a != addr],
            nick='Node%d' % addr, store_dir=os.path.join(tmp, str(addr)),
            ack_timeout=0.2, max_retries=8, frag_size=64, sym_rate=250000.0)

    air = []                       # (deliver_time, dest_addr, frame)
    now = [0.0]

    def pump(me, actions):
        """One transmitter, every other node hears it (or loses it)."""
        for kind, value in actions:
            if kind != 'tx':
                continue
            for dest in ADDRS:
                if dest == me:
                    continue
                if random.random() > LOSS:
                    air.append((now[0] + PROP, dest, value))

    picture = make_png()
    room_text = ('Bench check from node 1 to the whole room. This line is '
                 'deliberately longer than one 64-byte fragment so it has to '
                 'be split across several DATA frames and reassembled at '
                 'every recipient.')

    mid_room = nodes[1].submit('text', text=room_text, target='room')
    mid_dm = nodes[2].submit('text', text='private note for node 3 only',
                             target=3)
    mid_file = nodes[4].submit('file', name='plate.png', mime='image/png',
                               data=picture, target=1)
    nodes[3].submit('text', text='node 3 replying in the room', target='room')

    got_file = os.path.join(tmp, '1', 'rx_plate.png')
    drain_until = None
    for step in range(600000):
        now[0] = round(now[0] + 0.001, 6)
        due = [x for x in air if x[0] <= now[0]]
        air[:] = [x for x in air if x[0] > now[0]]
        for _, dest, frame in due:
            pump(dest, nodes[dest].on_rx(frame, now[0]))
        if step % int(TICK * 1000) == 0:
            for addr, node in nodes.items():
                pump(addr, node.on_tick(now[0]))
        done = (os.path.exists(got_file)
                and nodes[1]._by_mid[mid_room]['state'] in ('delivered', 'failed')
                and nodes[2]._by_mid[mid_dm]['state'] in ('delivered', 'failed')
                and nodes[4]._by_mid[mid_file]['state'] in ('delivered', 'failed'))
        if done and drain_until is None:
            # The exit condition trips one round trip early: the receiver's
            # FILE_DONE is still awaiting its own ACK. Let the network settle
            # so that quiescence means something.
            drain_until = now[0] + 3.0
        if drain_until is not None and now[0] >= drain_until:
            break

    # Nothing may be left holding an unacknowledged frame once the network has
    # been idle for three seconds - that is what proves no exchange wedges.
    quiesced = all(p.pending is None
                   for n in nodes.values() for p in n.link.peer_list())
    stuck = [(n.link.my_addr, p.addr, p.pending['label'])
             for n in nodes.values() for p in n.link.peer_list() if p.pending]

    print('\nsimulated %.2f s of link time, 4 nodes, %.0f%% frame loss\n'
          % (now[0], LOSS * 100))

    # ------------------------------------------------------------- the room
    print('  room')
    for addr in (2, 3, 4):
        check('node %d received the room message' % addr,
              any(room_text == t for t in conv_texts(nodes[addr], 'room')))
    check('room message reached delivered on the sender',
          nodes[1]._by_mid[mid_room]['state'] == 'delivered',
          nodes[1]._by_mid[mid_room]['state'])
    check('room receipt counted every recipient',
          nodes[1]._by_mid[mid_room]['acked']
          == nodes[1]._by_mid[mid_room]['frames'],
          '%s/%s acks' % (nodes[1]._by_mid[mid_room]['acked'],
                          nodes[1]._by_mid[mid_room]['frames']))
    check("node 1 sees node 3's room reply",
          any('node 3 replying' in t for t in conv_texts(nodes[1], 'room')))

    # --------------------------------------------------------------- direct
    print('\n  direct messages')
    check('node 3 received the direct message',
          any('private note' in t for t in conv_texts(nodes[3], 'p2')))
    for addr in (1, 4):
        leaked = any('private note' in (m.get('text') or '')
                     for m in nodes[addr].messages)
        check('node %d did NOT receive it' % addr, not leaked)
    check('direct message reached delivered',
          nodes[2]._by_mid[mid_dm]['state'] == 'delivered',
          nodes[2]._by_mid[mid_dm]['state'])
    check('non-addressed frames are counted, not delivered',
          nodes[1].link.stats['rx_notme'] > 0,
          'node 1 rx_notme=%d' % nodes[1].link.stats['rx_notme'])

    # ----------------------------------------------------------------- file
    print('\n  file transfer 4 -> 1')
    check('file written by node 1', os.path.exists(got_file))
    if os.path.exists(got_file):
        with open(got_file, 'rb') as fh:
            data = fh.read()
        check('file is byte-identical', data == picture,
              '%d of %d bytes' % (len(data), len(picture)))
    rx_files = [m for m in nodes[1].messages
                if m['dir'] == 'in' and m['kind'] == 'file']
    check('attachment filed under the sender conversation',
          bool(rx_files) and rx_files[-1].get('conv') == 'p4',
          rx_files[-1].get('conv') if rx_files else 'none')
    check('sender bubble reached delivered',
          nodes[4]._by_mid[mid_file]['state'] == 'delivered',
          nodes[4]._by_mid[mid_file]['state'])
    for addr in (2, 3):
        check('node %d did not receive the attachment' % addr,
              not any(m['dir'] == 'in' and m['kind'] == 'file'
                      for m in nodes[addr].messages))

    # ------------------------------------------------------------ the roster
    print('\n  roster and identity')
    for addr in ADDRS:
        roster = nodes[addr].state_payload()['peers']
        check('node %d knows its 3 peers' % addr, len(roster) == 3,
              ','.join(str(p['addr']) for p in roster))
    check('every nickname propagated',
          all(nodes[1].peer_nicks.get(a) == 'Node%d' % a for a in (2, 3, 4)),
          repr(nodes[1].peer_nicks))
    check('every peer is marked up on node 1',
          all(p['up'] for p in nodes[1].state_payload()['peers']))

    # --------------------------------------------------------- per-peer ARQ
    print('\n  per-peer ARQ')
    peers1 = nodes[1].link.peer_list()
    check('every peer has its own ARQ state object',
          len({id(p) for p in peers1}) == len(peers1)
          and len({id(p.txq) for p in peers1}) == len(peers1),
          '%d distinct peers' % len(peers1))
    check('all exchanges quiesced - nothing left unacknowledged', quiesced,
          repr(stuck) if stuck else 'every peer idle')
    depth = lambda n, a: len(n.link.peers[a].txq) + (1 if n.link.peers[a].pending else 0)
    before = {a: depth(nodes[1], a) for a in (2, 3, 4)}
    nodes[1].submit('text', text='targeting probe', target=2)
    nodes[1].on_tick(now[0])
    after = {a: depth(nodes[1], a) for a in (2, 3, 4)}
    check('a direct message enqueues only for its target',
          after[2] > before[2] and after[3] == before[3] and after[4] == before[4],
          'depth 2:%d->%d  3:%d->%d  4:%d->%d'
          % (before[2], after[2], before[3], after[3], before[4], after[4]))
    check('retransmissions actually happened',
          sum(n.link.stats['retx'] for n in nodes.values()) > 0,
          'total retx=%d' % sum(n.link.stats['retx'] for n in nodes.values()))
    check('nothing was dropped',
          sum(n.link.stats['dropped'] for n in nodes.values()) == 0,
          'dropped=%d' % sum(n.link.stats['dropped'] for n in nodes.values()))

    # ------------------------------------------------------------ discovery
    print('\n  peer discovery')
    import bpsk_link as L
    stranger = L.build(L.FT_DATA, 1, 9, 0, 0,
                       app.pack_envelope(app.AT_ROOM, 1, 0, 1, 'Newcomer',
                                         b'node 9 just joined'), 0, 0)
    before = len(nodes[1].link.peers)
    nodes[1].on_rx(stranger, now[0])
    check('an unconfigured node is discovered on first contact',
          len(nodes[1].link.peers) == before + 1 and 9 in nodes[1].link.peers,
          'peers=%s' % sorted(nodes[1].link.peers))
    check('its message is delivered',
          any('node 9 just joined' in t for t in conv_texts(nodes[1], 'room')))

    print('\n  counters')
    for addr in ADDRS:
        print('   node %d: %s' % (addr, nodes[addr].link.stats_line(now[0])))

    shutil.rmtree(tmp, ignore_errors=True)
    print('\n%s' % ('all multi-node checks passed' if not FAILURES
                    else 'FAILED: ' + ', '.join(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == '__main__':
    sys.exit(main())
