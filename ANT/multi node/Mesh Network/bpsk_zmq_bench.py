#!/usr/bin/env python3
"""Run the real r6.1 flowgraph with no radio: several nodes on an emulated air
that reproduces what broke r6.0.1 on the E200s.

bpsk_offline_demo.py and the protocol tests stop at the protocol layers. This
runs the actual flowgraph (bpsk_duplex_pluto.py) in GNU Radio - channel
filters, four receive chains, deframers, the transmit framer, the embedded
node block with its MAC, gain control and encryption - and replaces only the
radio: the two IIO Pluto blocks and the two IIO attribute sinks.

    node TX -> DAC model -> ZMQ PUB --+                   +--> node 1 RX
    node TX -> DAC model -> ZMQ PUB --+--> air (per pair: +--> node 2 RX ...
                                         gain, carrier offset, clock offset;
                                         per receiver: sum + noise)
    air -> ZMQ SUB -> ADC model -> flowgraph

What the models reproduce (each one a failure seen on hardware):

  self-coupling   every receiver hears its own transmitter, --self-db above
                  the reference level (the other nodes: --path-db) - the
                  'own transmitter overloads own receiver' failure;
  ADC             RX gain (IIO attribute 'hardwaregain', set by the node's
                  gain control) scales the input; I and Q clip at full scale
                  and are quantised to 12 bits, like the AD9361;
  TX attenuation  'hardwaregain' on the TX side scales the transmitter;
  IQ imbalance    --tx-iq and --rx-iq (amplitude %, phase deg) put an image
                  on the mirror frequency - the r6.0 mirror-channel failure
                  (0<->3, 1<->2) needs only this and the symmetric plan;
  crystals        --ppm per node: carrier offset (ppm x centre frequency)
                  and DAC clock offset (each node's symbols at their own
                  rate, which exposed the r6.0 lockstep measurement block);
  sample drops    --drop p: a whole receive buffer is lost with probability
                  p and 'O' is written to stderr, as gr-iio does;
  stall           --stall T,D: the receiver delivers nothing from T s for
                  D s (one node's receiver stalled);
  bad headers     --bad-headers p: the transmitter corrupts both copies of
                  the length of a frame (0x000D -> 0x300D, the 12301-byte
                  header from the hardware log) with probability p;
  buffer latency  --pluto-buffers n: samples are released n receive
                  buffers late (Pluto kernel buffers).

Gains are referenced so that rx_gain 30 dB / tx_atten 50 dB give the levels
set by --self-db and --path-db (dB relative to ADC full scale for a full-
scale transmitter). The defaults put the own transmitter 6 dB over full
scale at those gains - the 62 % clipping of the hardware log.

    python3 bpsk_zmq_bench.py run --nodes 2 --scale 8 --seconds 300
    python3 bpsk_zmq_bench.py run --nodes 4 --scale 8 --ppm 25,-25,12,-18

`run` creates a work directory, provisions keys, starts the air and every
node, sends traffic through the HTTP API, and summarises the telemetry
files against the r6.1 acceptance criteria. `air` and `node` start the
pieces by hand (see --help).

--scale S runs everything S times slower than real time (sample rate,
channel spacing, symbol rate and carrier offsets all divided by S) so that
several nodes fit on one PC. GNU Radio's buffers are sized in samples, so at
S > 1 they hold S times more time: the echo latency and lag warnings are
not hardware numbers. A FUNCTIONAL test, not a timing one. (The transmit
framer's queue cap, given in seconds, is scaled back to the hardware's size
in bits; otherwise at S = 32 it would hold less than two full frames.)
"""

import argparse
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

BASE_PORT = 5600
ADC_NOISE = 10 ** (-72.0 / 20.0) / 1.4142   # per component: -72 dBFS rms
ZMQ_HWM = 64     # messages queued per ZMQ socket: a slow reader loses data
                 # (as a Pluto overflows) instead of the latency growing
AIR_PORT = BASE_PORT + 100
CENTER = 910.2e6


def _floats(text, n, default):
    vals = [float(x) for x in (text or '').split(',') if x.strip()]
    return [vals[i] if i < len(vals) else default for i in range(n)]


# --------------------------------------------------------------------- air
def run_air(args):
    import math
    from gnuradio import analog, blocks, filter, gr, zeromq
    rate = 6e6 / args.scale
    n = args.nodes
    ppm = _floats(args.ppm, n, 0.0)
    rnd = random.Random(args.seed)
    if args.path_db:
        flat = [float(x) for x in args.path_db.split(',')]
        path = [[flat[(i * n + j) % len(flat)] for j in range(n)]
                for i in range(n)] if len(flat) >= n * n else \
            [[flat[0]] * n for _ in range(n)]
    else:
        path = [[rnd.uniform(-34.0, -26.0) for _ in range(n)]
                for _ in range(n)]
        for i in range(n):
            for j in range(i):
                path[i][j] = path[j][i]
    for i in range(n):
        path[i][i] = args.self_db
    lo = [p * 1e-6 * CENTER / args.scale for p in ppm]
    tb = gr.top_block('air')
    srcs = []
    for j in range(n):
        sub = zeromq.sub_source(gr.sizeof_gr_complex, 1,
                                'tcp://127.0.0.1:%d' % (BASE_PORT + 1 + j),
                                100, False, ZMQ_HWM, '', False)
        if ppm[j]:
            res = filter.mmse_resampler_cc(0.0, 1.0 + ppm[j] * 1e-6)
            tb.connect(sub, res)
            srcs.append(res)
        else:
            srcs.append(sub)
    keep = []
    for i in range(n):
        add = blocks.add_cc(1)
        for j in range(n):
            g = blocks.multiply_const_cc(10 ** (path[i][j] / 20.0))
            rot = blocks.rotator_cc(2 * math.pi * (lo[j] - lo[i]) / rate,
                                    False)
            tb.connect(srcs[j], g, rot, (add, j))
            keep += [g, rot]
        noise = analog.fastnoise_source_c(analog.GR_GAUSSIAN,
                                      10 ** (args.noise_db / 20.0), 7 + i,
                                          1 << 16)
        tb.connect(noise, (add, n))
        pub = zeromq.pub_sink(gr.sizeof_gr_complex, 1,
                              'tcp://127.0.0.1:%d' % (AIR_PORT + 1 + i), 100,
                              False, ZMQ_HWM, '', True, True)
        tb.connect(add, pub)
        keep += [add, noise, pub]
    print('[air] %d nodes at %.3f MS/s; carrier offsets %s Hz; path dB %s; '
          'noise %.0f dBFS' % (n, rate / 1e6, ['%+.0f' % x for x in lo],
                               [['%.0f' % v for v in row] for row in path],
                               args.noise_db), flush=True)
    tb.start()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        while not stop.wait(1.0):
            pass
    except KeyboardInterrupt:
        pass
    tb.stop()
    tb.wait()


# -------------------------------------------------------------------- node
def run_node(args):
    fg_args = args.flowgraph_args
    if fg_args and fg_args[0] == '--':
        fg_args = fg_args[1:]
    if args.headless:
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        os.environ['BROWSER'] = 'true'          # webbrowser.open -> no-op
    import numpy as np
    import pmt
    from gnuradio import gr, iio, zeromq

    rate = 6e6 / args.scale
    rnd = np.random.default_rng(args.seed + args.index)
    radio = {'rx_gain': 30.0, 'tx_atten': 50.0}

    tx_iq = _floats(args.tx_iq, 2, 0.0) if args.tx_iq else None
    rx_iq = _floats(args.rx_iq, 2, 0.0) if args.rx_iq else None

    from gnuradio import analog, blocks

    def imbalance_chain(tb, spec):
        """IQ imbalance as y = a x + b conj(x) (amplitude %, phase deg):
        puts an image b/a on the mirror frequency. Returns (in, out)."""
        amp, ph = (1.0 + spec[0] / 100.0), np.deg2rad(spec[1])
        a = 0.5 * (1 + amp * np.exp(-1j * ph))
        b = 0.5 * (1 - amp * np.exp(1j * ph))
        ma = blocks.multiply_const_cc(complex(a))
        cj = blocks.conjugate_cc()
        mb = blocks.multiply_const_cc(complex(b))
        add = blocks.add_cc(1)
        tb.connect(ma, (add, 0))
        tb.connect(cj, mb, (add, 1))
        return [ma, cj], add

    class Faults(gr.basic_block):
        """Receive buffer drops ('O' on stderr, as gr-iio) and a receiver
        stall. Only inserted when --drop or --stall asks for them."""

        def __init__(self, chunk):
            gr.basic_block.__init__(self, 'fault_model', [np.complex64],
                                    [np.complex64])
            self.chunk = int(chunk)
            self.t0 = None
            self.last_o = 0.0
            self.drops = 0
            self.dropping = 0

        def forecast(self, noutput_items, ninputs):
            return [noutput_items] * ninputs

        def general_work(self, input_items, output_items):
            x, out = input_items[0], output_items[0]
            n = min(len(x), len(out))
            now = time.time()
            if self.t0 is None:
                self.t0 = now
            el = now - self.t0
            if args.stall_at is not None and \
                    args.stall_at <= el < args.stall_at + args.stall_for:
                self.consume(0, n)
                time.sleep(0.002)
                return 0
            if not self.dropping and args.drop and \
                    rnd.random() < args.drop * n / float(self.chunk):
                self.dropping = self.chunk
                self.drops += 1
                if now - self.last_o > 1.0:
                    self.last_o = now
                    sys.stderr.write('O')
                    sys.stderr.flush()
            if self.dropping:
                k = min(n, self.dropping)
                self.dropping -= k
                self.consume(0, k)
                return 0
            out[:n] = x[:n]
            self.consume(0, n)
            return n

    class DacSink(gr.hier_block2):
        """Stands in for iio.fmcomms2_sink_fc32: TX attenuation, IQ imbalance,
        this node's DAC clock (wall clock x (1 + ppm)). C++ blocks only."""

        def __init__(self, uri, ch, buffer_size, *_):
            gr.hier_block2.__init__(self, 'dac_sink',
                                    gr.io_signature(1, 1, gr.sizeof_gr_complex),
                                    gr.io_signature(0, 0, 0))
            self.gain = blocks.multiply_const_cc(1.0)
            radio['tx_blk'] = self.gain
            self.thr = blocks.throttle(gr.sizeof_gr_complex,
                                       rate * (1.0 + args.ppm * 1e-6), True)
            self.pub = zeromq.pub_sink(
                gr.sizeof_gr_complex, 1,
                'tcp://127.0.0.1:%d' % (BASE_PORT + 1 + args.index), 100,
                False, ZMQ_HWM, '', True, True)
            if tx_iq:
                ins, out = imbalance_chain(self, tx_iq)
                self.connect(self, self.gain)
                for b in ins:
                    self.connect(self.gain, b)
                self.connect(out, self.thr, self.pub)
            else:
                self.connect(self, self.gain, self.thr, self.pub)

        def set_attenuation(self, ch, atten):
            set_tx(float(atten))

        def set_len_tag_key(self, *_): pass
        def set_bandwidth(self, *_): pass
        def set_frequency(self, *_): pass
        def set_samplerate(self, *_): pass
        def set_filter_params(self, *_): pass

    class AirSource(gr.hier_block2):
        """Stands in for iio.fmcomms2_source_fc32: IQ imbalance, RX gain,
        converter noise, 12-bit ADC (I and Q clip at full scale, quantised
        to 1/2048), Pluto buffer latency; drops and stalls on request."""

        def __init__(self, uri, ch, buffer_size, *_):
            gr.hier_block2.__init__(self, 'air_source',
                                    gr.io_signature(0, 0, 0),
                                    gr.io_signature(1, 1, gr.sizeof_gr_complex))
            self.sub = zeromq.sub_source(
                gr.sizeof_gr_complex, 1,
                'tcp://127.0.0.1:%d' % (AIR_PORT + 1 + args.index), 100,
                False, ZMQ_HWM, '', False)
            self.gain = blocks.multiply_const_cc(1.0)
            radio['rx_blk'] = self.gain
            noise = analog.fastnoise_source_c(analog.GR_GAUSSIAN,
                                          ADC_NOISE * 1.4142, 11 + args.index,
                                              1 << 16)
            add = blocks.add_cc(1)
            c2f = blocks.complex_to_float(1)
            fs = 2047.0 / 2048.0
            rails = [analog.rail_ff(-fs, fs), analog.rail_ff(-fs, fs)]
            q = [blocks.float_to_short(1, 2048.0), blocks.float_to_short(1, 2048.0)]
            d = [blocks.short_to_float(1, 2048.0), blocks.short_to_float(1, 2048.0)]
            f2c = blocks.float_to_complex(1)
            bs = max(1024, int(buffer_size) // max(1, int(args.scale)))
            lat = blocks.delay(gr.sizeof_gr_complex, bs * args.pluto_buffers)
            src = self.sub
            if rx_iq:
                ins, out = imbalance_chain(self, rx_iq)
                for b in ins:
                    self.connect(self.sub, b)
                src = out
            self.connect(src, self.gain, (add, 0))
            self.connect(noise, (add, 1))
            self.connect(add, c2f)
            for i in range(2):
                self.connect((c2f, i), rails[i], q[i], d[i], (f2c, i))
            last = f2c
            if args.drop or args.stall_at is not None:
                self.faults = Faults(bs)
                self.connect(last, self.faults)
                last = self.faults
            self.connect(last, lat, self)

        def set_gain(self, ch, gain):
            set_rx(float(gain))

        def set_len_tag_key(self, *_): pass
        def set_frequency(self, *_): pass
        def set_samplerate(self, *_): pass
        def set_gain_mode(self, *_): pass
        def set_quadrature(self, *_): pass
        def set_rfdc(self, *_): pass
        def set_bbdc(self, *_): pass
        def set_filter_params(self, *_): pass
        def set_bandwidth(self, *_): pass

    def set_rx(v):
        radio['rx_gain'] = v
        if radio.get('rx_blk') is not None:
            radio['rx_blk'].set_k(10 ** ((v - 30.0) / 20.0))

    def set_tx(v):
        radio['tx_atten'] = v
        if radio.get('tx_blk') is not None:
            radio['tx_blk'].set_k(10 ** (-(v - 50.0) / 20.0))

    class AttrSink(gr.basic_block):
        """Stands in for iio.attr_sink: 'hardwaregain' moves the models. Like
        the real one, a write it cannot do is an error, so the node's range
        checks are exercised: out-of-range gains raise."""

        def __init__(self, uri, device, channel, attr_type, output, *_):
            gr.basic_block.__init__(self, 'attr_sink', None, None)
            self.output = bool(output)
            self.message_port_register_in(pmt.intern('attr'))
            self.set_msg_handler(pmt.intern('attr'), self.handle)

        def handle(self, msg):
            d = pmt.to_python(msg)
            for key, val in d.items():
                if key == 'hardwaregain':
                    v = float(val)
                    if self.output:
                        if not -89.75 <= v <= 0:
                            raise RuntimeError('TX hardwaregain %s out of '
                                               'range' % val)
                        set_tx(-v)
                    else:
                        if not -3 <= v <= 71:
                            raise RuntimeError('RX hardwaregain %s out of '
                                               'range' % val)
                        set_rx(v)

    iio.fmcomms2_sink_fc32 = DacSink
    iio.fmcomms2_source_fc32 = AirSource
    iio.attr_sink = AttrSink

    if args.bad_headers:
        import bpsk_phy
        orig = bpsk_phy.frame_bytes
        rb = random.Random(args.seed * 7 + args.index)

        def bad_frame_bytes(payload, pre=64, post=64):
            out = bytearray(orig(payload, pre, post))
            if rb.random() < args.bad_headers:
                at = pre + 8               # the two length fields
                for k in (0, 2):
                    out[at + k] ^= 0x30    # 0x000D -> 0x300D
            return bytes(out)
        bpsk_phy.frame_bytes = bad_frame_bytes

    # The transmit framer caps its queue at max_backlog SECONDS of symbols,
    # i.e. max_backlog x sym_rate bits. At --scale S the symbol rate is S
    # times lower, so the same 0.5 s would hold S times fewer bits than on
    # the hardware (at S = 32: 3.9 kbit, less than two full frames) and the
    # bench dropped bursts the radio takes without effort (soak, 111 min:
    # ~5 frames/min per node, each one then retransmitted). Scale the cap
    # back to the hardware's size in bits - a bench correction only.
    import bpsk_phy
    _txf_init = bpsk_phy.TxFramer.__init__

    def _txf_scaled(self, preamble_size=64, postamble_size=64,
                    sym_rate=250e3, max_backlog=0.5):
        _txf_init(self, preamble_size, postamble_size, sym_rate,
                  max_backlog * float(args.scale))
    bpsk_phy.TxFramer.__init__ = _txf_scaled

    import bpsk_duplex_pluto as fg
    if os.environ.get('BENCH_TRACEMALLOC') and str(args.index) in \
            os.environ.get('BENCH_TRACEMALLOC_NODES', str(args.index)).split(','):
        # memory hunt: Python allocations that grow, every N seconds
        import tracemalloc
        tracemalloc.start(12)
        every = float(os.environ['BENCH_TRACEMALLOC'])

        def tm():
            # first snapshot after one period (start-up allocations done);
            # every later report is the growth since THAT baseline, so a
            # slow leak accumulates instead of hiding in period-to-period
            # noise
            time.sleep(every)
            base = tracemalloc.take_snapshot()
            t0 = time.time()
            while True:
                time.sleep(every)
                snap = tracemalloc.take_snapshot()
                top = snap.compare_to(base, 'traceback')[:6]
                cur, peak = tracemalloc.get_traced_memory()
                sys.stderr.write('[tm] t=%.0f s traced %.2f MB (since base '
                                 '%+.1f kB)\n' % (
                                     time.time() - t0, cur / 1e6,
                                     sum(s.size_diff for s in snap.compare_to(
                                         base, 'filename')) / 1e3))
                for st in top:
                    sys.stderr.write('[tm] %+.1f kB %d blocks\n' % (
                        st.size_diff / 1e3, st.count_diff))
                    for line in st.traceback.format()[-8:]:
                        sys.stderr.write('[tm]     %s\n' % line)
        threading.Thread(target=tm, daemon=True).start()
    if os.environ.get('BENCH_PROBE'):
        _start = fg.bpsk_duplex_pluto.start

        def probed_start(tb, *a):
            cap = os.environ.get('BENCH_CAPTURE')
            if cap:
                from gnuradio import blocks as _b
                for k in range(4):
                    for nm in ('rrc', 'afc'):
                        fs_ = _b.file_sink(gr.sizeof_gr_complex,
                                           '%s%d_%s%d.c64' % (cap, args.index, nm, k), False)
                        fs_.set_unbuffered(False)
                        tb.connect(getattr(tb, '%s_ch%d' % (nm, k)), fs_)
            r = _start(tb, *a)

            def probe():
                names = ['iio_pluto_source_0', 'adc_keep', 'tx_framer'] + \
                    ['%s_ch%d' % (b, k) for k in range(4) for b in
                     ('chan_filter', 'afc', 'agc', 'rrc', 'symbol_sync',
                      'costas', 'decoder', 'bits_to_vec', 'meas_keep',
                      'iq_keep')]
                while True:
                    time.sleep(float(os.environ['BENCH_PROBE']))
                    row = []
                    for nm in names:
                        b = getattr(tb, nm, None)
                        try:
                            row.append('%s=%d' % (nm, b.nitems_written(0)))
                        except Exception as e:
                            row.append('%s=?' % nm)
                    for k in range(4):
                        try:
                            row.append('agcgain_ch%d=%g' % (
                                k, getattr(tb, 'agc_ch%d' % k).gain()))
                            row.append('costasf_ch%d=%g' % (
                                k, getattr(tb, 'costas_ch%d' % k).get_frequency()))
                        except Exception as e:
                            row.append('err=%r' % e)
                    sys.stderr.write('[probe] ' + ' '.join(row) + '\n')
            threading.Thread(target=probe, daemon=True).start()
            return r
        fg.bpsk_duplex_pluto.start = probed_start
    s = float(args.scale)
    options = fg.argument_parser().parse_args(
        ['--radio-rate', str(6e6 / s), '--ch-spacing', str(600e3 / s),
         '--ch-first', str(450e3 / s)] + fg_args)
    print('[bench] node %d: radio replaced by models (%.3f MS/s, 1/%g real '
          'time, ppm %+.1f, drop %g, bad headers %g, stall %s)'
          % (args.index + 1, 6e6 / s / 1e6, s, args.ppm, args.drop,
             args.bad_headers, args.stall_at), flush=True)
    fg.main(options=options)


# --------------------------------------------------------------------- run
def http(port, path, data=None, timeout=5):
    req = urllib.request.Request('http://127.0.0.1:%d%s' % (port, path),
                                 data=None if data is None else
                                 json.dumps(data).encode(),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode() or 'null')


def run_scenario(args):
    """Air + N nodes as processes, traffic over HTTP, a summary at the end."""
    import bpsk_keys
    work = os.path.abspath(args.work or 'bench_run')
    if os.path.exists(work):
        shutil.rmtree(work)
    os.makedirs(work)
    names = ['Ann', 'Ben', 'Cat', 'Dan', 'Eve', 'Fay'][:args.nodes]
    dirs = {nm: os.path.join(work, nm) for nm in names}
    for d in dirs.values():
        os.makedirs(d)
        for f in os.listdir(HERE):
            if f.endswith('.py') or f == 'chat_ui.html':
                os.symlink(os.path.join(HERE, f), os.path.join(d, f))
    bpsk_keys.provision_mesh({nm: os.path.join(d, 'keys')
                              for nm, d in dirs.items()},
                             trust=not args.no_trust)
    logs = []
    procs = []
    me = os.path.abspath(__file__)
    py = sys.executable
    air = [py, '-u', me, 'air', '--nodes', str(args.nodes), '--scale',
           str(args.scale), '--self-db', str(args.self_db), '--noise-db',
           str(args.noise_db), '--seed', str(args.seed)]
    if args.ppm:
        air += ['--ppm', args.ppm]
    if args.path_db:
        air += ['--path-db', args.path_db]
    fh = open(os.path.join(work, 'air.log'), 'w')
    logs.append(fh)
    procs.append(subprocess.Popen(air, stdout=fh, stderr=subprocess.STDOUT,
                                  cwd=work))
    ppm = _floats(args.ppm, args.nodes, 0.0)
    ports = {}
    for i, nm in enumerate(names):
        port = 8100 + i
        ports[nm] = port
        cmd = [py, '-u', me, 'node', '--index', str(i), '--scale',
               str(args.scale), '--ppm', str(ppm[i]), '--headless',
               '--seed', str(args.seed), '--pluto-buffers',
               str(args.pluto_buffers)]
        for opt in ('tx_iq', 'rx_iq'):
            if getattr(args, opt):
                cmd += ['--' + opt.replace('_', '-'), getattr(args, opt)]
        if args.drop and (args.fault_node is None or args.fault_node == i):
            cmd += ['--drop', str(args.drop)]
        if args.bad_headers and (args.fault_node is None
                                 or args.fault_node == i):
            cmd += ['--bad-headers', str(args.bad_headers)]
        if args.stall and (args.fault_node is None or args.fault_node == i):
            cmd += ['--stall', args.stall]
        cmd += ['--', '--nickname', nm, '--http-port', str(port)]
        cmd += list(args.flowgraph_args or [])
        fh = open(os.path.join(work, '%s.log' % nm), 'w')
        logs.append(fh)
        procs.append(subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT,
                                      cwd=dirs[nm]))
    t0 = time.time()
    print('[run] %d nodes, work dir %s, %.0f s' % (args.nodes, work,
                                                  args.seconds), flush=True)
    sent = []
    try:
        next_msg = t0 + args.warmup
        k = 0
        while time.time() - t0 < args.seconds:
            time.sleep(0.5)
            if any(p.poll() is not None for p in procs):
                print('[run] a process exited early', flush=True)
                break
            if time.time() >= next_msg and \
                    time.time() - t0 < args.seconds - args.drain:
                next_msg = time.time() + args.msg_every
                nm = names[k % len(names)]
                k += 1
                try:
                    r = http(ports[nm], '/api/send',
                             {'text': 'bench %d from %s' % (k, nm),
                              'to': 'all'})
                    sent.append((nm, r.get('id'), time.time()))
                except Exception as exc:                   # noqa: BLE001
                    print('[run] send via %s failed: %s' % (nm, exc))
        states = {}
        for nm in names:
            try:
                states[nm] = http(ports[nm], '/api/state', timeout=10)
            except Exception as exc:                       # noqa: BLE001
                states[nm] = {'error': repr(exc)}
    finally:
        for p in procs[1:]:
            p.send_signal(signal.SIGTERM)
        time.sleep(2.0)
        procs[0].send_signal(signal.SIGTERM)
        for p in procs:
            try:
                p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                p.kill()
        for fh in logs:
            fh.close()
    with open(os.path.join(work, 'states.json'), 'w') as fh:
        json.dump(states, fh, indent=1, default=str)
    summary = summarise(work, names, states, sent, args)
    with open(os.path.join(work, 'summary.json'), 'w') as fh:
        json.dump(summary, fh, indent=1, default=str)
    print(json.dumps(summary, indent=1, default=str))
    return summary


def summarise(work, names, states, sent, args):
    import csv
    out = {'nodes': names, 'scale': args.scale, 'seconds': args.seconds}
    for nm in names:
        d = os.path.join(work, nm, 'telemetry')
        files = sorted(f for f in os.listdir(d)) if os.path.isdir(d) else []
        node = [f for f in files if f.endswith('.csv') and 'peers' not in f]
        peers = [f for f in files if f.endswith('_peers.csv')]
        info = {}
        if node:
            with open(os.path.join(d, node[0])) as fh:
                rows = list(csv.DictReader(fh))
            info['rows'] = len(rows)
            late = [r for r in rows if r['uptime'] and
                    float(r['uptime']) > 10 * max(1.0, args.scale / 4)]
            ov = [int(r['o_per_s'] or 0) for r in late if r.get('o_per_s')]
            info['overflows_after_warmup'] = sum(ov)
            un = [int(r['u_per_s'] or 0) for r in late if r.get('u_per_s')]
            info['underflows_after_warmup'] = sum(un)
            last = rows[-1] if rows else {}
            for key in ('channel', 'rx_gain', 'tx_atten', 'adc_peak_dbfs',
                        'adc_clip', 'cpu_cores', 'cpu_top', 'echo_ms',
                        'live_fault', 'rx_valid', 'rx_bad'):
                info[key] = last.get(key)
            peaks = [float(r['adc_peak_dbfs']) for r in late
                     if r['adc_peak_dbfs']]
            info['adc_peak_max_after_warmup'] = max(peaks) if peaks else None
            chans = [r['channel'] for r in late]
            info['channel_changes_after_warmup'] = sum(
                1 for a, b in zip(chans, chans[1:]) if a != b)
            cpu = [float(r['cpu_cores']) for r in late if r['cpu_cores']]
            info['cpu_mean'] = round(sum(cpu) / len(cpu), 2) if cpu else None
            rej = sum(int(r.get('rej%d' % k) or 0) for r in rows
                      for k in range(4))
            info['headers_rejected'] = rej
            info['faults'] = sorted({r['live_fault'] for r in rows
                                     if r['live_fault']})
            rss = [(float(r['uptime']), float(r['rss_mb'])) for r in late
                   if r.get('rss_mb')]
            if len(rss) > 10:
                n10 = max(1, len(rss) // 10)
                head = sum(v for _, v in rss[:n10]) / n10
                tail = sum(v for _, v in rss[-n10:]) / n10
                hours = max(1e-6, (rss[-1][0] - rss[0][0]) / 3600.0)
                info['rss_mb'] = {'first': round(head, 1),
                                  'last': round(tail, 1),
                                  'max': max(v for _, v in rss),
                                  'growth_mb_per_h': round(
                                      (tail - head) / hours, 2)}
            txd = [int(r['tx_dropped'] or 0) for r in late
                   if r.get('tx_dropped')]
            info['tx_dropped'] = (max(txd) - min(txd)) if txd else None
        if peers:
            with open(os.path.join(d, peers[0])) as fh:
                prow = list(csv.DictReader(fh))
            links = {}
            for r in prow[-30 * len(names):]:
                key = r['nick']
                links.setdefault(key, []).append(r)
            info['links'] = {}
            first = {}
            for r in prow:
                f = first.setdefault(r['nick'], {})
                if r['state'] and r['state'] != 'away' and 'seen' not in f:
                    f['seen'] = float(r['time'])
                if r['state'] == 'ok' and 'ok' not in f:
                    f['ok'] = float(r['time'])
            for key, rs in links.items():
                snr_in = [float(r['snr_in']) for r in rs if r['snr_in']]
                snr_out = [float(r['snr_out']) for r in rs if r['snr_out']]
                ok = sum(int(r['ok_ch'] or 0) for r in rs)
                bad = sum(int(r['bad_ch'] or 0) for r in rs)
                info['links'][key] = {
                    'snr_in_median': sorted(snr_in)[len(snr_in) // 2]
                    if snr_in else None,
                    'snr_out_median': sorted(snr_out)[len(snr_out) // 2]
                    if snr_out else None,
                    'fer': round(bad / float(ok + bad), 4) if ok + bad
                    else None, 'state': rs[-1]['state'],
                    'session_s': round(first[key]['ok'] - first[key]['seen'],
                                       1) if 'ok' in first.get(key, {}) and
                    'seen' in first[key] else None,
                    'reason': rs[-1]['reason']}
        st = states.get(nm) or {}
        msgs = st.get('messages') or []
        mine = [m for m in msgs if m.get('dir') == 'out'
                and m.get('kind') == 'text' and m.get('conv') == 'all']
        info['group_sent'] = len(mine)
        info['group_delivered_all'] = sum(1 for m in mine
                                          if m.get('state') == 'delivered')
        info['group_states'] = [m.get('state') for m in mine][-10:]
        out[nm] = info
    out['messages_sent'] = len(sent)
    return out


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='mode', required=True)
    a = sub.add_parser('air', help='the shared medium')
    a.add_argument('--nodes', type=int, default=2)
    a.add_argument('--scale', type=float, default=1.0)
    a.add_argument('--ppm', default='', help='crystal offset per node, ppm')
    a.add_argument('--self-db', type=float, default=6.0,
                   help='own transmitter at own receiver, dB re full scale '
                        'at rx_gain 30 / tx_atten 50')
    a.add_argument('--path-db', default='',
                   help='N*N gains (row = receiver), or one value')
    a.add_argument('--noise-db', type=float, default=-62.0,
                   help='receiver noise, dBFS at rx_gain 30')
    a.add_argument('--seed', type=int, default=1)
    n = sub.add_parser('node', help='one flowgraph')
    n.add_argument('--index', type=int, required=True, help='0-based')
    n.add_argument('--scale', type=float, default=1.0)
    n.add_argument('--ppm', type=float, default=0.0)
    n.add_argument('--tx-iq', default='', help='amplitude %%,phase deg')
    n.add_argument('--rx-iq', default='', help='amplitude %%,phase deg')
    n.add_argument('--drop', type=float, default=0.0)
    n.add_argument('--stall', default='', help='T,D seconds')
    n.add_argument('--bad-headers', type=float, default=0.0)
    n.add_argument('--pluto-buffers', type=int, default=2)
    n.add_argument('--seed', type=int, default=1)
    n.add_argument('--headless', action='store_true')
    n.add_argument('flowgraph_args', nargs=argparse.REMAINDER)
    r = sub.add_parser('run', help='air + nodes + traffic + summary')
    r.add_argument('--nodes', type=int, default=2)
    r.add_argument('--scale', type=float, default=8.0)
    r.add_argument('--seconds', type=float, default=180.0)
    r.add_argument('--warmup', type=float, default=40.0)
    r.add_argument('--msg-every', type=float, default=6.0)
    r.add_argument('--drain', type=float, default=30.0,
                   help='no new messages in the last DRAIN seconds, so '
                        'every one sent has time for its receipts')
    r.add_argument('--ppm', default='')
    r.add_argument('--self-db', type=float, default=6.0)
    r.add_argument('--path-db', default='')
    r.add_argument('--noise-db', type=float, default=-62.0)
    r.add_argument('--tx-iq', default='2,1')
    r.add_argument('--rx-iq', default='2,1')
    r.add_argument('--drop', type=float, default=0.0)
    r.add_argument('--bad-headers', type=float, default=0.0)
    r.add_argument('--stall', default='')
    r.add_argument('--fault-node', type=int, default=None)
    r.add_argument('--pluto-buffers', type=int, default=2)
    r.add_argument('--no-trust', action='store_true')
    r.add_argument('--seed', type=int, default=1)
    r.add_argument('--work', default='')
    r.add_argument('flowgraph_args', nargs=argparse.REMAINDER)
    args = ap.parse_args()
    if args.mode == 'node':
        st = _floats(args.stall, 2, 0.0) if args.stall else None
        args.stall_at, args.stall_for = (st[0], st[1]) if st else (None, 0.0)
    if args.mode == 'air':
        run_air(args)
    elif args.mode == 'node':
        run_node(args)
    else:
        if args.flowgraph_args and args.flowgraph_args[0] == '--':
            args.flowgraph_args = args.flowgraph_args[1:]
        run_scenario(args)


if __name__ == '__main__':
    main()
