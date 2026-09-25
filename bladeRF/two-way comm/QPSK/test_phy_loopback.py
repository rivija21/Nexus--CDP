#!/usr/bin/env python3
"""End-to-end test of the real modem without any hardware.

Builds both nodes' TX and RX chains in one flowgraph and joins them with
channels.channel_model in each direction (AWGN + carrier offset + timing
offset + multipath), then runs the actual ChatLink protocol over it.

    node A  TX ---> channel ---> RX  node B
    node B  TX ---> channel ---> RX  node A

Run:  python3 test_phy_loopback.py [--snr-db 20] [--file-bytes 8000]
"""
import argparse
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gnuradio import gr, blocks, channels

from qpsk_phy import PhyConfig, RxChain, TxChain
from qpsk_chat import ChatLink


class LoopbackPhy(gr.top_block):
    """Two nodes, two channels, one flowgraph."""

    def __init__(self, cfg, on_frame_a, on_frame_b, noise_volt=0.05,
                 freq_offset=0.0, epsilon=1.0, taps=(1.0,), throttle=True):
        gr.top_block.__init__(self, "qpsk_loopback", catch_exceptions=True)
        self.cfg = cfg
        self.tx_a, self.rx_a = TxChain(cfg), RxChain(cfg, on_frame_a)
        self.tx_b, self.rx_b = TxChain(cfg), RxChain(cfg, on_frame_b)

        for tx, rx in ((self.tx_a, self.rx_b), (self.tx_b, self.rx_a)):
            chan = channels.channel_model(
                noise_voltage=noise_volt,
                frequency_offset=freq_offset,
                epsilon=epsilon,
                taps=list(taps),
                noise_seed=0,
                block_tags=False,
            )
            path = list(tx.blocks) + [chan]
            if throttle:
                path.append(blocks.throttle(gr.sizeof_gr_complex, cfg.samp_rate,
                                            True))
            path += list(rx.blocks)
            self.connect(*path)


class NodeAdapter:
    """Presents one node's half of the loopback with the QpskPhy API."""

    def __init__(self, tx_chain, rx_chain):
        self._tx = tx_chain
        self._rx = rx_chain

    def send_frame(self, payload, urgent=False):
        self._tx.frame_source.send(payload, urgent=urgent)

    def tx_backlog(self):
        return self._tx.frame_source.backlog()

    def phy_stats(self):
        s = dict(self._rx.frame_sink.asm.stats())
        s["tx_frames"] = self._tx.frame_source.n_frames_sent
        return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snr-db", type=float, default=20.0)
    ap.add_argument("--freq-offset", type=float, default=1e-4,
                    help="normalised carrier offset (fraction of samp_rate)")
    ap.add_argument("--epsilon", type=float, default=1.00005,
                    help="timing/clock ratio between the two nodes")
    ap.add_argument("--multipath", action="store_true")
    ap.add_argument("--file-bytes", type=int, default=8000)
    ap.add_argument("--settle", type=float, default=2.0)
    ap.add_argument("--samp-rate", type=float, default=1e6)
    args = ap.parse_args()

    noise = 10 ** (-args.snr_db / 20.0)
    taps = [1.0, 0.25 - 0.25j, 0.1 + 0.05j] if args.multipath else [1.0]
    cfg = PhyConfig(samp_rate=args.samp_rate)

    tmp = tempfile.mkdtemp(prefix="qpsk_loop_")
    holder = {}
    tb = LoopbackPhy(
        cfg,
        lambda p: holder["a"].on_frame(p),
        lambda p: holder["b"].on_frame(p),
        noise_volt=noise, freq_offset=args.freq_offset,
        epsilon=args.epsilon, taps=taps,
    )
    a = ChatLink(NodeAdapter(tb.tx_a, tb.rx_a), out_dir=os.path.join(tmp, "a"))
    b = ChatLink(NodeAdapter(tb.tx_b, tb.rx_b), out_dir=os.path.join(tmp, "b"))
    holder["a"], holder["b"] = a, b

    print("loopback: %.1f MSPS, sps=%d, SNR=%.0f dB, foffset=%.1e, eps=%.5f, "
          "multipath=%s" % (cfg.samp_rate / 1e6, cfg.sps, args.snr_db,
                            args.freq_offset, args.epsilon, args.multipath))
    tb.start()
    print("settling receivers for %.1f s..." % args.settle)
    time.sleep(args.settle)

    rc = 0
    try:
        t0 = time.time()
        ok, dt = a.send_text("A -> B: differential QPSK, full duplex")
        print("  text A->B      : %s (%.2f s)" % ("ACK" if ok else "FAILED", dt))
        rc |= 0 if ok else 1

        ok, dt = b.send_text("B -> A: reply on the reverse channel")
        print("  text B->A      : %s (%.2f s)" % ("ACK" if ok else "FAILED", dt))
        rc |= 0 if ok else 1

        src = os.path.join(tmp, "blob.bin")
        data = os.urandom(args.file_bytes)
        with open(src, "wb") as f:
            f.write(data)
        ok, dt = a.send_file(src)
        out = os.path.join(tmp, "b", "blob.bin")
        good = ok and os.path.exists(out) and open(out, "rb").read() == data
        print("  file A->B %5d B: %s (%.2f s, %.1f kB/s)"
              % (args.file_bytes, "OK" if good else "FAILED", dt,
                 args.file_bytes / max(dt, 1e-9) / 1e3))
        rc |= 0 if good else 1

        print("  elapsed %.1f s" % (time.time() - t0))
        for name, node in (("A", a), ("B", b)):
            print("  node %s stats: %s" % (name, node.stats()))
    finally:
        a.stop(); b.stop()
        tb.stop(); tb.wait()
    print("LOOPBACK PASS" if rc == 0 else "LOOPBACK FAIL")
    return rc


if __name__ == "__main__":
    sys.exit(main())
