"""Shared-medium simulator for the r6b CSMA testbed.

Four (or N) GNU Radio flowgraphs run bpsk_csma_zmq.py instead of the Pluto
flowgraph. Each PUSHes its transmit samples here and PULLs its receive samples
back. This process is the air between them.

The reason it exists
--------------------
The r6a harness passes whole link frames between nodes, so it can only deliver
or drop them. That models loss; it cannot model a COLLISION. Two stations
transmitting in the same interval do not lose their frames - their waveforms
SUM, and the receiver gets the sum. Nothing about carrier sense, backoff or the
hidden-terminal case can be tested without that, which is exactly what r6b is.

So this channel sums. For every chunk it:

  * reads whatever each node has transmitted (silence where a node is quiet -
    under burst mode a station emits nothing at all between frames),
  * delays each contribution by that link's propagation,
  * adds them, and adds thermal noise,
  * and gives each node the sum of everybody EXCEPT itself, which models a
    perfect transmit/receive switch: a real station does not hear its own
    transmission.

Run it before the flowgraphs:

    python3 csma_zmq_channel.py --nodes 4

Ports, for node i (0-based), with --base 5550:
    tcp://127.0.0.1:(base + i)         node PUSHes its TX samples here
    tcp://127.0.0.1:(base + 100 + i)   node PULLs its RX samples from here
"""

import argparse
import sys
import time

import numpy as np

try:
    import zmq
except ImportError:                                    # pragma: no cover
    sys.exit('pyzmq is required: pip install pyzmq')

DTYPE = np.complex64


class Medium(object):
    def __init__(self, nodes, base, chunk, noise_db, prop_samples, loss_db,
                 swap_bind=False):
        self.n = nodes
        self.chunk = chunk
        self.sigma = 10.0 ** (noise_db / 20.0)
        self.loss = 10.0 ** (-loss_db / 20.0)
        self.ctx = zmq.Context.instance()
        self.rx, self.tx = [], []
        # gr-zeromq convention: a push_sink BINDS and a pull_source CONNECTS.
        # So this process connects to each node's transmit socket and binds the
        # one each node receives from. If your gr-zeromq build is the other way
        # round, flip it with --swap-bind rather than editing this.
        for i in range(nodes):
            pull = self.ctx.socket(zmq.PULL)
            pull.setsockopt(zmq.RCVHWM, 64)
            pull.setsockopt(zmq.LINGER, 0)
            (pull.bind if swap_bind else pull.connect)(
                'tcp://127.0.0.1:%d' % (base + i))
            self.rx.append(pull)
            push = self.ctx.socket(zmq.PUSH)
            push.setsockopt(zmq.SNDHWM, 64)
            push.setsockopt(zmq.LINGER, 0)
            (push.connect if swap_bind else push.bind)(
                'tcp://127.0.0.1:%d' % (base + 100 + i))
            self.tx.append(push)
        # Per-node leftovers, so a chunk boundary never splits a burst.
        self.pending = [np.zeros(0, dtype=DTYPE) for _ in range(nodes)]
        # Propagation: a whole number of samples per link. At bench distances
        # this is far below one sample, so it is really a knob for exploring
        # what happens when it is not.
        self.delay = int(prop_samples)
        self.tail = [np.zeros(self.delay, dtype=DTYPE) for _ in range(nodes)]
        self.stats = dict(chunks=0, overlaps=0, samples=0, active=0)

    def _collect(self, i):
        """Take up to `chunk` samples from node i, zero-filling silence."""
        buf = self.pending[i]
        while len(buf) < self.chunk:
            try:
                raw = self.rx[i].recv(zmq.NOBLOCK)
            except zmq.Again:
                break
            buf = np.concatenate([buf, np.frombuffer(raw, dtype=DTYPE)])
        take = buf[:self.chunk]
        self.pending[i] = buf[self.chunk:]
        if len(take) < self.chunk:
            take = np.concatenate(
                [take, np.zeros(self.chunk - len(take), dtype=DTYPE)])
        return take

    def step(self):
        chunk = self.chunk
        contrib = []
        active = 0
        for i in range(self.n):
            block = self._collect(i)
            if self.delay:                        # carry the tail across chunks
                block = np.concatenate([self.tail[i], block])
                self.tail[i] = block[chunk:]
                block = block[:chunk]
            if np.any(block):
                active += 1
            contrib.append(block * self.loss)

        self.stats['chunks'] += 1
        self.stats['samples'] += chunk
        if active:
            self.stats['active'] += 1
        if active > 1:
            self.stats['overlaps'] += 1           # a real collision on the air

        total = np.sum(contrib, axis=0)
        for i in range(self.n):
            # Everything except our own transmission: a station does not hear
            # itself, so its receiver sees only the others plus noise.
            others = total - contrib[i]
            noise = (np.random.normal(0, self.sigma / np.sqrt(2), chunk)
                     + 1j * np.random.normal(0, self.sigma / np.sqrt(2), chunk))
            out = (others + noise).astype(DTYPE)
            try:
                self.tx[i].send(out.tobytes(), zmq.NOBLOCK)
            except zmq.Again:
                pass                              # receiver is behind; drop
        return active

    def close(self):
        for sock in self.rx + self.tx:
            sock.close(0)
        # LINGER 0 on every socket, so this cannot hang on Ctrl-C with a peer
        # still attached - which it otherwise does, every time.
        self.ctx.term()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--nodes', type=int, default=4)
    ap.add_argument('--base', type=int, default=5550)
    ap.add_argument('--chunk', type=int, default=2048,
                    help='samples per step; match the Pluto source buffer')
    ap.add_argument('--noise-db', type=float, default=-60.0,
                    help='thermal noise level')
    ap.add_argument('--loss-db', type=float, default=30.0,
                    help='path loss applied to every contribution')
    ap.add_argument('--prop-samples', type=int, default=0,
                    help='propagation delay per link, in samples')
    ap.add_argument('--swap-bind', action='store_true',
                    help='bind where this would connect and vice versa, if your '
                         'gr-zeromq build binds on the source side')
    ap.add_argument('--report', type=float, default=5.0,
                    help='seconds between statistics lines')
    args = ap.parse_args()

    med = Medium(args.nodes, args.base, args.chunk, args.noise_db,
                 args.prop_samples, args.loss_db, swap_bind=args.swap_bind)
    print('shared medium: %d nodes, chunk %d, noise %.0f dB, path loss %.0f dB'
          % (args.nodes, args.chunk, args.noise_db, args.loss_db))
    for i in range(args.nodes):
        print('  node %d  TX -> :%d   RX <- :%d'
              % (i + 1, args.base + i, args.base + 100 + i))
    print('waiting for flowgraphs. Ctrl-C to stop.')

    last = time.time()
    try:
        while True:
            if not med.step():
                time.sleep(0.001)                 # nothing on the air
            now = time.time()
            if now - last >= args.report:
                last = now
                s = med.stats
                busy = (100.0 * s['active'] / s['chunks']) if s['chunks'] else 0
                coll = (100.0 * s['overlaps'] / s['active']) if s['active'] else 0
                print('  chunks=%-9d airtime busy=%5.1f%%  of that, '
                      'overlapping=%5.1f%%  (%d collisions)'
                      % (s['chunks'], busy, coll, s['overlaps']))
    except KeyboardInterrupt:
        pass
    finally:
        s = med.stats
        print('\nfinal: %d chunks, %d with energy, %d with MORE THAN ONE '
              'station transmitting' % (s['chunks'], s['active'], s['overlaps']))
        print('the last number is the one CSMA is supposed to drive toward zero.')
        med.close()


if __name__ == '__main__':
    main()
