#!/usr/bin/env python3
"""Run the real flowgraph with no radio: Pluto blocks swapped for ZMQ + a channel.

bpsk_offline_demo.py stops at the link layer. This one runs the actual
generated flowgraph (bpsk_duplex_pluto.py) in GNU Radio - the DBPSK modem,
FLL, RRC, symbol sync, Costas loop, access-code correlator, the embedded chat
block and its encryption - and replaces only the two IIO Pluto blocks:

    Pluto sink    ->  throttle (samp_rate)  ->  ZMQ PUB  (--tx address)
    Pluto source  <-  channel model (AWGN, CFO, clock offset)  <-  ZMQ SUB (--rx)

Two nodes, cross-connected, on one machine (each in its own folder with its
own keys, exactly as on the bench):

    cd nodeA && python3 bpsk_zmq_bench.py --tx tcp://127.0.0.1:5601 \\
        --rx tcp://127.0.0.1:5602 -- --my-addr 1 --peer-addr 2 --http-port 8088
    cd nodeB && python3 bpsk_zmq_bench.py --tx tcp://127.0.0.1:5602 \\
        --rx tcp://127.0.0.1:5601 -- --my-addr 2 --peer-addr 1 --http-port 8089

Everything after `--` goes to the flowgraph unchanged. Add --headless to run
without a display (Qt offscreen). Needs gr-zeromq, which ships with GNU Radio.
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--tx', required=True, help='ZMQ PUB address this node binds')
    ap.add_argument('--rx', required=True, help='ZMQ address of the other node')
    ap.add_argument('--noise', type=float, default=0.05,
                    help='AWGN voltage at the receiver (signal amplitude ~0.5)')
    ap.add_argument('--cfo', type=float, default=2000.0,
                    help='carrier frequency offset, Hz')
    ap.add_argument('--ppm', type=float, default=20.0,
                    help='sample clock offset, ppm')
    ap.add_argument('--gain', type=float, default=1.0,
                    help='linear gain applied after the channel')
    ap.add_argument('--headless', action='store_true',
                    help='no display needed (Qt offscreen, no browser)')
    ap.add_argument('flowgraph_args', nargs=argparse.REMAINDER)
    args = ap.parse_args()
    fg_args = args.flowgraph_args
    if fg_args and fg_args[0] == '--':
        fg_args = fg_args[1:]
    if args.headless:
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        os.environ['BROWSER'] = 'true'          # webbrowser.open -> no-op

    from gnuradio import blocks, channels, gr, iio, zeromq

    samp_rate = 1e6

    class ZmqSink(gr.hier_block2):
        """Stands in for iio.fmcomms2_sink_fc32."""

        def __init__(self, *_):
            gr.hier_block2.__init__(self, 'zmq_pluto_sink',
                                    gr.io_signature(1, 1, gr.sizeof_gr_complex),
                                    gr.io_signature(0, 0, 0))
            # The throttle stands in for the DAC consuming at samp_rate.
            self.thr = blocks.throttle(gr.sizeof_gr_complex, samp_rate, True)
            self.pub = zeromq.pub_sink(gr.sizeof_gr_complex, 1, args.tx, 100,
                                       False, -1, '', False, True)
            self.connect(self, self.thr, self.pub)

        def set_len_tag_key(self, *_): pass
        def set_bandwidth(self, *_): pass
        def set_frequency(self, *_): pass
        def set_samplerate(self, *_): pass
        def set_attenuation(self, *_): pass
        def set_filter_params(self, *_): pass

    class ZmqSource(gr.hier_block2):
        """Stands in for iio.fmcomms2_source_fc32."""

        def __init__(self, *_):
            gr.hier_block2.__init__(self, 'zmq_pluto_source',
                                    gr.io_signature(0, 0, 0),
                                    gr.io_signature(1, 1, gr.sizeof_gr_complex))
            self.sub = zeromq.sub_source(gr.sizeof_gr_complex, 1, args.rx, 100,
                                         False, -1, '', False)
            self.ch = channels.channel_model(
                noise_voltage=args.noise,
                frequency_offset=args.cfo / samp_rate,
                epsilon=1.0 + args.ppm * 1e-6,
                taps=[1.0 + 0j], noise_seed=0)
            self.amp = blocks.multiply_const_cc(args.gain)
            self.connect(self.sub, self.ch, self.amp, self)

        def set_len_tag_key(self, *_): pass
        def set_frequency(self, *_): pass
        def set_samplerate(self, *_): pass
        def set_gain_mode(self, *_): pass
        def set_gain(self, *_): pass
        def set_quadrature(self, *_): pass
        def set_rfdc(self, *_): pass
        def set_bbdc(self, *_): pass
        def set_filter_params(self, *_): pass

    iio.fmcomms2_sink_fc32 = ZmqSink
    iio.fmcomms2_source_fc32 = ZmqSource

    import bpsk_duplex_pluto as fg
    options = fg.argument_parser().parse_args(fg_args)
    print('[bench] Pluto blocks replaced: TX -> %s, RX <- %s  (AWGN %.3f, '
          'CFO %.0f Hz, %.0f ppm)' % (args.tx, args.rx, args.noise, args.cfo,
                                      args.ppm), flush=True)
    fg.main(options=options)


if __name__ == '__main__':
    main()
