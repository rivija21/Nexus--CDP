#!/usr/bin/env python3
"""One CSMA node driven over ZeroMQ instead of a radio - the r6b testbed.

Identical DSP chain and identical protocol stack to bpsk_csma_pluto.py; only
the Pluto sink and source are replaced by ZeroMQ, so four of these plus
csma_zmq_channel.py give you a four-node network whose collisions are real
waveform overlap rather than dropped frames.

Headless on purpose: four Qt windows is not a testbed. Each node still serves
its browser UI, so open the four ports side by side.

    # terminal 1
    python3 csma_zmq_channel.py --nodes 4

    # terminals 2-5
    python3 bpsk_csma_zmq.py --node 1 --nodes 4 --http-port 8088
    python3 bpsk_csma_zmq.py --node 2 --nodes 4 --http-port 8089
    python3 bpsk_csma_zmq.py --node 3 --nodes 4 --http-port 8090
    python3 bpsk_csma_zmq.py --node 4 --nodes 4 --http-port 8091

What to watch: the channel process prints how much airtime carried energy and
what fraction of that had MORE THAN ONE station transmitting. That second
number is what CSMA exists to drive toward zero. Run it once with
--mac-slot 0 (contention effectively disabled) to see what it looks like
without the MAC, then at the default, and compare.
"""

import argparse
import signal
import sys
import time

from gnuradio import blocks, digital, filter, gr, pdu, zeromq
from gnuradio.filter import firdes
import pmt

import bpsk_csma_link_layer as link_layer


class csma_node(gr.top_block):

    def __init__(self, node=1, nodes=4, base=5550, http_port=8088,
                 nickname='', samp_rate=1000000, sps=4, frag_size=256,
                 preamble_size=384, postamble_size=64, ack_timeout=0.5,
                 max_retries=5, mac_slot=0.003, cw_min=8, cw_max=128,
                 busy_threshold_db=-55.0, beacon_interval=2.0,
                 peer_timeout=8.0, rx_dir='.'):
        gr.top_block.__init__(self, 'BPSK CSMA node %d (ZMQ)' % node,
                              catch_exceptions=False)

        my_addr = node
        peers = ','.join(str(a) for a in range(1, nodes + 1) if a != node)
        overhead = preamble_size + postamble_size + 12
        constel = digital.constellation_bpsk().base()
        constel.set_npwr(1.0)
        hdr = digital.header_format_default(
            digital.packet_utils.default_access_code, 0)

        tx_addr = 'tcp://127.0.0.1:%d' % (base + node - 1)
        rx_addr = 'tcp://127.0.0.1:%d' % (base + 100 + node - 1)

        # ---------------------------------------------------------- blocks
        self.zmq_sink = zeromq.push_sink(gr.sizeof_gr_complex, 1, tx_addr,
                                         100, False, (-1), False)
        self.zmq_source = zeromq.pull_source(gr.sizeof_gr_complex, 1, rx_addr,
                                             100, False, (-1), False)

        self.link_layer = link_layer.blk(
            my_addr=my_addr, peer_addr=(2 if node == 1 else 1),
            ack_timeout=ack_timeout, max_retries=max_retries,
            frag_size=frag_size, queue_ahead=0, sym_rate=samp_rate / sps,
            overhead=overhead, rx_dir=rx_dir,
            nickname=nickname or ('Node %d' % node), http_port=http_port,
            open_ui=False, tx_freq=0.0, rx_freq=0.0, peers=peers,
            mac_slot=mac_slot, cw_min=cw_min, cw_max=cw_max,
            busy_threshold_db=busy_threshold_db,
            beacon_interval=beacon_interval, peer_timeout=peer_timeout)

        self.strobe = blocks.message_strobe(pmt.intern('tick'), 10)
        self.keep = blocks.keep_one_in_n(gr.sizeof_gr_complex * 1, sps)
        self.scale = blocks.multiply_const_cc(0.5)

        self.pdu_to_stream = pdu.pdu_to_tagged_stream(gr.types.byte_t, 'packet_len')
        self.formatter = digital.protocol_formatter_bb(hdr, 'packet_len')
        self.pre = blocks.stream_to_tagged_stream(gr.sizeof_char, 1,
                                                  preamble_size, 'packet_len')
        self.post = blocks.stream_to_tagged_stream(gr.sizeof_char, 1,
                                                   postamble_size, 'packet_len')
        self.pre_src = blocks.vector_source_b([0xc0, 0xaf], True, 1, [])
        self.post_src = blocks.vector_source_b([0xc0, 0xaf], True, 1, [])
        self.mux = blocks.tagged_stream_mux(gr.sizeof_char * 1, 'packet_len', 0)
        self.mod = digital.generic_mod(constellation=constel, differential=True,
                                       samples_per_symbol=sps,
                                       pre_diff_code=True, excess_bw=0.35,
                                       verbose=False, log=False, truncate=False)

        self.fll = digital.fll_band_edge_cc(sps, 0.35, 44, 0.03, False)
        self.rrc = filter.fft_filter_ccc(
            1, firdes.root_raised_cosine(1, samp_rate, samp_rate / sps, 0.35,
                                         11 * sps), 1)
        self.sync = digital.symbol_sync_cc(
            digital.TED_SIGNAL_TIMES_SLOPE_ML, sps, 0.045, 1.0, 0.1, 1.5, 1,
            constel.base(), digital.IR_MMSE_8TAP, 32, [])
        self.costas = digital.costas_loop_cc(3.14 / 100,
                                             len(constel.points()), False)
        self.decoder = digital.constellation_decoder_cb(constel)
        self.diff = digital.diff_decoder_bb(len(constel.points()),
                                            digital.DIFF_DIFFERENTIAL)
        self.correlate = digital.correlate_access_code_bb_ts(
            digital.packet_utils.default_access_code, 2, 'packet_len')
        self.repack = blocks.repack_bits_bb(1, 8, 'packet_len', True,
                                            gr.GR_MSB_FIRST)
        self.stream_to_pdu = pdu.tagged_stream_to_pdu(gr.types.byte_t, 'packet_len')

        # ----------------------------------------------------- connections
        self.msg_connect((self.strobe, 'strobe'), (self.link_layer, 'tick'))
        self.msg_connect((self.link_layer, 'tx_frame'), (self.pdu_to_stream, 'pdus'))
        self.msg_connect((self.stream_to_pdu, 'pdus'), (self.link_layer, 'rx_frame'))

        self.connect((self.pdu_to_stream, 0), (self.formatter, 0))
        self.connect((self.pre_src, 0), (self.pre, 0))
        self.connect((self.post_src, 0), (self.post, 0))
        self.connect((self.pre, 0), (self.mux, 0))
        self.connect((self.formatter, 0), (self.mux, 1))
        self.connect((self.pdu_to_stream, 0), (self.mux, 2))
        self.connect((self.post, 0), (self.mux, 3))
        self.connect((self.mux, 0), (self.mod, 0))
        self.connect((self.mod, 0), (self.scale, 0))
        self.connect((self.scale, 0), (self.zmq_sink, 0))

        self.connect((self.zmq_source, 0), (self.fll, 0))
        self.connect((self.zmq_source, 0), (self.keep, 0))
        self.connect((self.keep, 0), (self.link_layer, 1))     # carrier sense
        self.connect((self.fll, 0), (self.rrc, 0))
        self.connect((self.rrc, 0), (self.sync, 0))
        self.connect((self.sync, 0), (self.costas, 0))
        self.connect((self.costas, 0), (self.link_layer, 0))   # EVM / SNR
        self.connect((self.costas, 0), (self.decoder, 0))
        self.connect((self.decoder, 0), (self.diff, 0))
        self.connect((self.diff, 0), (self.correlate, 0))
        self.connect((self.correlate, 0), (self.repack, 0))
        self.connect((self.repack, 0), (self.stream_to_pdu, 0))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--node', type=int, required=True, help='this node address, 1-N')
    ap.add_argument('--nodes', type=int, default=4)
    ap.add_argument('--base', type=int, default=5550)
    ap.add_argument('--http-port', type=int, default=8088)
    ap.add_argument('--nickname', default='')
    ap.add_argument('--mac-slot', type=float, default=0.003)
    ap.add_argument('--cw-min', type=int, default=8)
    ap.add_argument('--cw-max', type=int, default=128)
    ap.add_argument('--busy-threshold-db', type=float, default=-55.0)
    ap.add_argument('--store', default='zmq_files')
    args = ap.parse_args()

    tb = csma_node(node=args.node, nodes=args.nodes, base=args.base,
                   http_port=args.http_port, nickname=args.nickname,
                   mac_slot=args.mac_slot, cw_min=args.cw_min,
                   cw_max=args.cw_max,
                   busy_threshold_db=args.busy_threshold_db,
                   rx_dir=args.store)
    tb.start()
    print('node %d up. chat UI: http://127.0.0.1:%d/  (Ctrl-C to stop)'
          % (args.node, args.http_port))

    def bye(sig=None, frame=None):
        tb.stop()
        tb.wait()
        sys.exit(0)
    signal.signal(signal.SIGINT, bye)
    signal.signal(signal.SIGTERM, bye)
    while True:
        time.sleep(0.5)


if __name__ == '__main__':
    main()
