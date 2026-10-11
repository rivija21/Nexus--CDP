"""Run the whole chat application with no radio attached.

Two ChatApp nodes are connected by a virtual channel that adds propagation
delay and drops frames at a configurable rate, and each node serves its own
browser UI. Everything above the modem - envelope, ARQ receipts, fragment
repair, telemetry, the UI itself - is the same code the flowgraph runs, so
this is a faithful rehearsal for a demo, and a way to develop the front end
without occupying the hardware.

    python3 bpsk_offline_demo.py                 # 2 nodes, 8% frame loss
    python3 bpsk_offline_demo.py --nodes 4       # the 4-node room
    python3 bpsk_offline_demo.py --loss 0.25     # a deliberately bad channel
    python3 bpsk_offline_demo.py --loss 0        # a perfect one

Then open http://127.0.0.1:8088/, :8089/, :8090/ ... side by side.
"""

import argparse
import math
import os
import random
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bpsk_app as app                                          # noqa: E402


def channel(nodes, loss, prop, stop):
    """A shared medium: every transmission is offered to every other node.

    Each node then accepts only what is addressed to it, which is exactly what
    the r6b CSMA channel will be. Loss is drawn independently per receiver, so
    a frame can reach one node and miss another - the realistic case, and the
    one that exercises per-peer ARQ rather than a single shared timer.
    """
    air = []
    lock = threading.Lock()

    def pump(addr, actions):
        for kind, value in actions:
            if kind != 'tx':
                continue
            for other in nodes:
                if other == addr:
                    continue
                if random.random() < loss:
                    continue
                with lock:
                    air.append((time.time() + prop, other, value))

    while not stop.is_set():
        now = time.time()
        with lock:
            due = [x for x in air if x[0] <= now]
            air[:] = [x for x in air if x[0] > now]
        for _, dest, frame in due:
            pump(dest, nodes[dest].on_rx(frame, time.time()))
        for addr, node in nodes.items():
            pump(addr, node.on_tick(time.time()))
        time.sleep(0.005)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--loss', type=float, default=0.08,
                    help='fraction of frames the channel destroys')
    ap.add_argument('--prop', type=float, default=0.02,
                    help='one-way channel delay, seconds')
    ap.add_argument('--nodes', type=int, default=2,
                    help='how many nodes to run (2-8)')
    ap.add_argument('--port', type=int, default=8088,
                    help='port for node 1; each further node takes the next')
    ap.add_argument('--store', default='demo_files',
                    help='where attachments are kept')
    args = ap.parse_args()

    count = max(2, min(int(args.nodes), 8))
    addrs = list(range(1, count + 1))
    names = 'ABCDEFGH'
    nodes = {}
    for addr in addrs:
        nodes[addr] = app.ChatApp(
            my_addr=addr, peers=[a for a in addrs if a != addr],
            nick='Node %s' % names[addr - 1],
            store_dir=os.path.join(args.store, names[addr - 1].lower()),
            frag_size=256, sym_rate=250000.0,
            tx_freq=905.2e6, rx_freq=910.2e6)

    servers = [app.serve(nodes[a], args.port + i)[0]
               for i, a in enumerate(addrs)]
    stop = threading.Event()
    worker = threading.Thread(target=channel,
                              args=(nodes, args.loss, args.prop, stop),
                              daemon=True)
    worker.start()

    # A synthetic constellation so the signal-quality panel is not blank.
    def telemetry():
        k = 0
        while not stop.is_set():
            k += 1
            snr = 15.0 + 3.0 * math.sin(k / 9.0) + random.uniform(-0.8, 0.8)
            for node in nodes.values():
                node.set_radio_metrics(snr_db=round(snr, 2),
                                       level_db=round(-33 + 2 * math.sin(k / 6.0), 1),
                                       evm=round(100 * 10 ** (-snr / 20), 2),
                                       locked=True)
            time.sleep(0.5)
    threading.Thread(target=telemetry, daemon=True).start()

    for i, addr in enumerate(addrs):
        print('node %s  http://127.0.0.1:%d/' % (names[addr - 1], args.port + i))
    print('channel: %d nodes, %.0f%% frame loss, %.0f ms delay. Ctrl-C to stop.'
          % (count, args.loss * 100, args.prop * 1000))
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for srv in servers:
            app.shutdown(srv)
        for node in nodes.values():
            node.close()


if __name__ == '__main__':
    main()
