#!/usr/bin/env python3
"""Runs the two GRC embedded blocks (as GRC itself generates them) through the
real modem, so the .grc flowgraph is verified, not just parsed.

    frame_source(blk) -> generic_mod -> channel -> RX chain -> frame_sink(blk)

Requires the flowgraph to have been compiled once with:
    grcc -o <dir> qpsk_duplex_phy.grc
and <dir> passed as --gen-dir (default /tmp/grcout).
"""
import argparse
import os
import sys
import time

import pmt
from gnuradio import gr, blocks, channels

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qpsk_phy import PhyConfig, RxChain, TxChain


class MsgCollector(gr.basic_block):
    def __init__(self):
        gr.basic_block.__init__(self, name="collector", in_sig=[], out_sig=[])
        self.got = []
        self.message_port_register_in(pmt.intern("in"))
        self.set_msg_handler(pmt.intern("in"), self._h)

    def _h(self, msg):
        vec = pmt.cdr(msg) if pmt.is_pair(msg) else msg
        self.got.append(bytes(pmt.u8vector_elements(vec)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gen-dir", default="/tmp/grcout")
    ap.add_argument("--snr-db", type=float, default=15.0)
    ap.add_argument("--n", type=int, default=12)
    args = ap.parse_args()

    sys.path.insert(0, args.gen_dir)
    import qpsk_duplex_phy_frame_source as gsrc
    import qpsk_duplex_phy_frame_sink as gsnk

    cfg = PhyConfig(samp_rate=1e6)
    tb = gr.top_block("grc_block_test", catch_exceptions=True)

    src = gsrc.blk(idle_chunk=1024)
    src.set_max_output_buffer(4096)
    snk = gsnk.blk(tag_key="pkt_start")
    collector = MsgCollector()

    tx = TxChain(cfg)
    rx = RxChain(cfg, lambda p: None)
    chan = channels.channel_model(
        noise_voltage=10 ** (-args.snr_db / 20.0), frequency_offset=1e-4,
        epsilon=1.00002, taps=[1.0], noise_seed=0, block_tags=False)

    tb.connect(src, tx.mod, tx.scale, chan,
               blocks.throttle(gr.sizeof_gr_complex, cfg.samp_rate, True),
               *rx.blocks[:-1])          # everything up to the correlator
    tb.connect(rx.correlator, snk)
    tb.msg_connect((snk, "rx"), (collector, "in"))

    sent = [("GRC embedded block test #%d" % i).encode() for i in range(args.n)]
    tb.start()
    time.sleep(1.5)                      # let the loops lock on the PN filler
    for p in sent:
        src._on_msg(pmt.cons(pmt.PMT_NIL, pmt.init_u8vector(len(p), list(p))))
        time.sleep(0.05)
    time.sleep(1.5)
    tb.stop(); tb.wait()

    ok = collector.got == sent
    print("  sent %d PDUs, received %d, identical: %s"
          % (len(sent), len(collector.got), ok))
    print("  assembler stats: %s" % (snk.asm.stats(),))
    if collector.got[:1]:
        print("  first payload: %r" % collector.got[0])
    print("GRC BLOCK TEST PASS" if ok else "GRC BLOCK TEST FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
