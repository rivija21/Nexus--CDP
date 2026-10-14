"""GNU Radio level: the shipped flowgraph, its embedded blocks, and the
r6.0.1 blocks they replace. Needs GNU Radio (3.10); skips cleanly without.

  G1 every embedded source in the .grc is byte-identical to its file
  G2 the shipped .py is what the .grc compiles to (3.10.12 layout)
  G3 bpsk_phy.rrc_taps is GNU Radio's root-raised cosine
  G4 F3 at block level: a corrupted header in the r6.0.1 receive blocks
     (correlate_access_code_bb_ts -> repack_bits_bb -> tagged_stream_to_pdu)
     against the r6.1 deframer block from the .grc
  G5 the receive chain's AGC/symbol sync, parameters taken from the .grc:
     a transmitter switching on cannot wedge it (the r6.1 bench found that
     agc2 + symbol_sync did); a node 50 dB below another still decodes
  G7 the r6.0.1 chain (FLL, no AGC) loses neighbours 30 dB below full
     scale; the r6.1 chain decodes them
  G6 the WHOLE generated flowgraph, radio replaced by a file of samples:
     own transmitter on channel 0 near full scale with its IQ image, two
     neighbours 35 and 50 dB below it with +18 / -21 kHz carrier offsets,
     a corrupted header and a 40 ms gap (dropped samples) in the stream;
     every channel's frames are counted at the node block

    python3 test_grc.py
"""

import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
FAILURES = []


def check(label, cond, detail=''):
    print('  [%s] %s%s' % ('PASS' if cond else 'FAIL', label,
                           (' - ' + detail) if detail else ''))
    if not cond:
        FAILURES.append(label)


def section(title):
    print('\n%s' % title)


def grc():
    import yaml
    with open(os.path.join(HERE, 'bpsk_duplex_pluto.grc')) as fh:
        return yaml.safe_load(fh)


def g1(d):
    section('G1 embedded sources')
    epy = [b for b in d['blocks'] if b['id'] in ('epy_block',)]
    names = {'link_layer': 'bpsk_duplex_pluto_link_layer.py',
             'tx_framer': 'bpsk_duplex_pluto_tx_framer.py'}
    for k in range(4):
        names['deframer_ch%d' % k] = 'bpsk_duplex_pluto_deframer_ch%d.py' % k
    seen = set()
    for b in epy:
        fn = names.get(b['name'])
        src = b['parameters']['_source_code']
        if fn is None:
            check('embedded block %s has a file' % b['name'], False)
            continue
        with open(os.path.join(HERE, fn)) as fh:
            same = fh.read() == src
        seen.add(b['name'])
        check('%s == the source embedded as %s' % (fn, b['name']), same)
    check('all six embedded blocks present', seen == set(names),
          repr(sorted(set(names) - seen)))
    with open(os.path.join(HERE, 'dev', 'epy_deframer.py')) as fh:
        dsrc = fh.read()
    with open(os.path.join(HERE, 'bpsk_duplex_pluto_deframer_ch0.py')) as fh:
        check('the deframer blocks are dev/epy_deframer.py', fh.read() == dsrc)


def g2():
    section('G2 the shipped .py is the compiled .grc')
    grcc = '/usr/bin/grcc'
    if not os.path.exists(grcc):
        check('grcc available', False)
        return
    out = tempfile.mkdtemp(prefix='bpsk_grcc_')
    res = subprocess.run([sys.executable, grcc, '-o', out,
                          os.path.join(HERE, 'bpsk_duplex_pluto.grc')],
                         capture_output=True, text=True, cwd=out)
    check('grcc compiles the .grc without errors', res.returncode == 0,
          (res.stderr or '')[-200:])
    if res.returncode:
        return
    sys.path.insert(0, os.path.join(HERE, 'dev'))
    import make_grc
    with open(os.path.join(out, 'bpsk_duplex_pluto.py')) as fh:
        gen = make_grc.to_31012(fh.read())
    with open(os.path.join(HERE, 'bpsk_duplex_pluto.py')) as fh:
        shipped = fh.read()
    def norm(text):
        # grcc writes the connections in set order (string hashing is
        # randomised per process): compare them as a set
        lines = text.splitlines(True)
        conn = sorted(l for l in lines if l.startswith(
            ('        self.connect(', '        self.msg_connect(')))
        rest = [l for l in lines if not l.startswith(
            ('        self.connect(', '        self.msg_connect('))]
        return ''.join(rest), conn
    diff = ''
    if norm(gen) == norm(shipped):
        gen = shipped
    if gen != shipped:
        import difflib
        diff = ''.join(list(difflib.unified_diff(
            shipped.splitlines(True), gen.splitlines(True), n=0))[:12])
    check('bpsk_duplex_pluto.py is exactly that output (in the 3.10.12 '
          'layout; connection order aside)', gen == shipped, diff)
    check('3.10.12 layout: version banner, flowgraph_started event, '
          'QSettings key', '# GNU Radio version: 3.10.12' in shipped
          and 'self.flowgraph_started = threading.Event()' in shipped
          and 'tb.flowgraph_started.set()' in shipped
          and 'Qt.QSettings("gnuradio/flowgraphs"' in shipped)
    for name in os.listdir(out):
        if name.startswith('bpsk_duplex_pluto_') and name.endswith('.py'):
            with open(os.path.join(out, name)) as a, \
                    open(os.path.join(HERE, name)) as b:
                if a.read() != b.read():
                    check('%s as grcc writes it' % name, False)
    import shutil
    shutil.rmtree(out, ignore_errors=True)
    for p in ('chat_files',):
        q = os.path.join(out, p)
        if os.path.isdir(q):
            shutil.rmtree(q, ignore_errors=True)


def g3():
    section('G3 filters')
    import numpy as np
    from gnuradio.filter import firdes
    import bpsk_phy as P
    a = np.array(firdes.root_raised_cosine(1.0, 4, 1, 0.35, 44))
    b = P.rrc_taps(4, 0.35, 11)
    check('bpsk_phy.rrc_taps is firdes.root_raised_cosine',
          len(a) == len(b) and np.max(np.abs(a / a.sum() - b / b.sum()))
          < 1e-6)


def run_bits(blocks_fn, bits, settle=0.5, timeout=10.0):
    """bits -> chain -> PDUs; returns (list of payload bytes, stalled)."""
    import numpy as np
    import pmt
    from gnuradio import blocks, gr
    tb = gr.top_block()
    src = blocks.vector_source_b(bits.tolist(), False)
    first, last = blocks_fn(tb, src)
    dbg = blocks.message_debug()
    tb.msg_connect((last, 'pdus'), (dbg, 'store'))
    tb.start()
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(0.1)
        if first.nitems_read(0) >= len(bits) - 4096:
            break
    time.sleep(settle)
    stalled = first.nitems_read(0) < len(bits) - 8192
    tb.stop()
    tb.wait()
    out = []
    for i in range(dbg.num_messages()):
        m = dbg.get_message(i)
        v = pmt.cdr(m)
        if pmt.is_u8vector(v):
            out.append(bytes(pmt.u8vector_elements(v)))
    return out, stalled


def g4(d):
    section('G4 F3 at block level: corrupted header')
    import numpy as np
    from gnuradio import blocks, digital, pdu, gr
    import bpsk_phy as P
    rng = np.random.default_rng(3)
    good = [bytes(rng.integers(0, 256, 60, dtype=np.uint8))
            for _ in range(30)]
    # GNU Radio 3.10.12 reads all 16 bits of the length (3.10.9 masked it to
    # 12): 13 -> 0x300D = 12301 bytes on the user's 3.10.12. To reproduce on
    # either version the two flipped bits here are inside the low 12:
    # 13 -> 0x0C0D = 3085 bytes, 24680 bits.
    bad = bytearray(P.frame_bytes(bytes(13)))
    bad[72] ^= 0x0C
    bad[74] ^= 0x0C
    fill = P.PRBS[:20000]
    bits = np.concatenate(
        [P.frame_bits(good[0]), np.unpackbits(np.frombuffer(bytes(bad),
                                                             np.uint8))]
        + [P.frame_bits(g) for g in good[1:]] + [fill] * 8).astype(np.uint8)

    def old_chain(tb, src):
        # r6.0.1, as generated (buffers as set there)
        cor = digital.correlate_access_code_bb_ts(
            digital.packet_utils.default_access_code, 2, 'packet_len')
        cor.set_min_output_buffer(1048576)
        rep = blocks.repack_bits_bb(1, 8, 'packet_len', True,
                                    gr.GR_MSB_FIRST)
        rep.set_min_output_buffer(131072)
        t2p = pdu.tagged_stream_to_pdu(gr.types.byte_t, 'packet_len')
        tb.connect(src, cor, rep, t2p)
        return cor, t2p
    got_old, _ = run_bits(old_chain, bits)
    old_ok = sum(1 for g in good if g in got_old)

    blk = [b for b in d['blocks'] if b['name'] == 'deframer_ch0'][0]
    ns = {}
    exec(compile(blk['parameters']['_source_code'], 'deframer', 'exec'), ns)

    def new_chain(tb, src):
        v = blocks.stream_to_vector(1, 4096)
        de = ns['blk'](channel=0, max_len=600, batch=4096)
        tb.connect(src, v, de)
        tb._keep = de
        return v, de
    got_new, _ = run_bits(new_chain, bits)
    new_ok = sum(1 for g in good if g in got_new)
    check('r6.0.1 blocks: the frames after a corrupted length (3085 bytes; '
          '12301 on 3.10.12) are swallowed (reproduces F3)',
          old_ok < len(good) - 5,
          '%d of %d frames' % (old_ok, len(good)))
    check('r6.1 deframer block: only the corrupted frame is lost',
          new_ok == len(good), '%d of %d frames' % (new_ok, len(good)))


def g5(d):
    section('G5 AGC and symbol sync (parameters from the .grc)')
    import numpy as np
    from gnuradio import analog, blocks, digital, filter, gr
    from gnuradio.filter import firdes
    import bpsk_phy as P
    p = {b['name']: b['parameters'] for b in d['blocks']}
    ag = p['agc_ch0']
    ss = p['symbol_sync_ch0']
    f = lambda s: float(eval(str(s), {}))                    # noqa: E731
    rng = np.random.default_rng(5)

    def burst(sec, dbfs, cfo=0.0):
        tx = P.TxFramer(max_backlog=100)
        pays = []
        n = int(sec * 250e3)
        while tx.backlog_bits() < n:
            pays.append(bytes(rng.integers(0, 256, 60, dtype=np.uint8)))
            tx.push(pays[-1])
        s = tx.next_symbols(n)
        up = np.zeros(4 * n, np.complex64)
        up[::4] = s
        x = np.convolve(up, P.rrc_taps(4, 0.35, 16))[:4 * n]
        x = x / np.max(np.abs(x)) * 10 ** (dbfs / 20.0)
        x = x * np.exp(2j * np.pi * cfo * np.arange(4 * n) / 1e6)
        return x.astype(np.complex64), pays

    def noise(n, dbfs):
        return ((rng.normal(size=n) + 1j * rng.normal(size=n)) * 10 ** (
            dbfs / 20.0) / np.sqrt(2)).astype(np.complex64)

    a, pa = burst(0.3, -1.0, 300.0)
    b, pb = burst(0.3, -55.0, -200.0)
    x = np.concatenate([np.zeros(300000, np.complex64), a,
                        np.zeros(100000, np.complex64), b])
    x += noise(len(x), -75.0)

    def chain(agc):
        tb = gr.top_block()
        src = blocks.vector_source_c(x.tolist(), False)
        rrc = filter.fft_filter_ccc(1, firdes.root_raised_cosine(
            1.0, 1e6, 250e3, 0.35, 44), 1)
        sy = digital.symbol_sync_cc(
            digital.TED_SIGNAL_TIMES_SLOPE_ML, 4, f(ss['loop_bw']),
            f(ss['damping']), f(ss['ted_gain']), f(ss['max_dev']), 1,
            digital.constellation_bpsk().base(), digital.IR_MMSE_8TAP, 32, [])
        cos = digital.costas_loop_cc(f(p['costas_ch0']['w']), 2, False)
        dec = digital.constellation_decoder_cb(
            digital.constellation_bpsk().base())
        dd = digital.diff_decoder_bb(2, digital.DIFF_DIFFERENTIAL)
        sink = blocks.vector_sink_b()
        tb.connect(src, agc, rrc, sy, cos, dec, dd, sink)
        tb.start()
        t0 = time.time()
        while time.time() - t0 < 30:
            time.sleep(0.2)
            if sy.nitems_read(0) >= len(x) - 400:
                break
        wedged = sy.nitems_read(0) < len(x) - 400
        tb.stop()
        tb.wait()
        got = P.Deframer().feed(np.array(sink.data(), np.uint8))
        return wedged, sum(1 for q in pa if q in got), \
            sum(1 for q in pb if q in got)

    agc3 = analog.agc3_cc(f(ag['attack_rate']), f(ag['decay_rate']),
                          f(ag['reference']), f(ag['gain']),
                          int(f(ag['iir_update_decim'])), f(ag['max_gain']))
    w, na, nb = chain(agc3)
    check('the flowgraph\'s AGC + symbol sync: silence, then a transmitter '
          'at -1 dBFS switching on: not wedged, its frames decode',
          not w and na >= len(pa) - 1, 'wedged %s, %d/%d' % (w, na, len(pa)))
    check('... and a node at -55 dBFS right after it decodes',
          nb >= len(pb) - 1, '%d/%d' % (nb, len(pb)))
    agc2 = analog.agc2_cc(1e-1, 1e-2, 1.0, 1.0, 1e5)
    w2, na2, nb2 = chain(agc2)
    check('(why not agc2: with it the same input wedges the symbol sync - '
          'the output spikes to gain x input before the loop reacts)', w2,
          'wedged %s, %d/%d, %d/%d' % (w2, na2, len(pa), nb2, len(pb)))


def g7(d):
    section('G7 weak neighbours and carrier offset: the r6.0.1 chain (FLL, '
            'no AGC) against the r6.1 chain (AFC, AGC), parameters as '
            'generated')
    import numpy as np
    from gnuradio import analog, blocks, digital, filter, gr
    from gnuradio.filter import firdes
    import bpsk_phy as P
    p = {b['name']: b['parameters'] for b in d['blocks']}
    ag, ss = p['agc_ch0'], p['symbol_sync_ch0']
    f = lambda v: float(eval(str(v), {}))                    # noqa: E731
    rng = np.random.default_rng(8)

    def signal(dbfs, cfo):
        tx = P.TxFramer(max_backlog=100)
        pays = []
        while tx.backlog_bits() < 250000:
            pays.append(bytes(rng.integers(0, 256, 60, dtype=np.uint8)))
            tx.push(pays[-1])
        sym = tx.next_symbols(250000)
        up = np.zeros(4 * len(sym), np.complex64)
        up[::4] = sym
        x = np.convolve(up, P.rrc_taps(4, 0.35, 16))[:len(up)]
        x = 10 ** (dbfs / 20.0) * x / np.max(np.abs(x)) * np.exp(
            2j * np.pi * cfo * np.arange(len(x)) / 1e6)
        x = x + (rng.normal(size=len(x)) + 1j * rng.normal(size=len(x))) \
            * 10 ** (-78 / 20.0) / np.sqrt(2)
        return x.astype(np.complex64), pays

    def run(x, front):
        tb = gr.top_block()
        src = blocks.vector_source_c(x.tolist(), False)
        sink = blocks.vector_sink_b()
        dec = digital.constellation_decoder_cb(
            digital.constellation_bpsk().base())
        dd = digital.diff_decoder_bb(2, digital.DIFF_DIFFERENTIAL)
        chain = front() + [dec, dd, sink]
        tb.connect(src, *chain)
        tb.run()
        return P.Deframer().feed(np.array(sink.data(), np.uint8))

    def old():
        try:                       # 3.10.12 form, as r6.0.1 shipped
            fll = digital.fll_band_edge_cc(4, 0.35, 44, 0.03, False)
        except TypeError:          # 3.10.9
            fll = digital.fll_band_edge_cc(4, 0.35, 44, 0.03)
        return [fll, filter.fft_filter_ccc(1, firdes.root_raised_cosine(
            1, 1e6, 250e3, 0.35, 44), 1),
            digital.symbol_sync_cc(
                digital.TED_SIGNAL_TIMES_SLOPE_ML, 4, 0.045, 1.0, 0.1, 1.5, 1,
                digital.constellation_bpsk().base(), digital.IR_MMSE_8TAP, 32,
                []),
            digital.costas_loop_cc(3.14 / 100, 2, False)]

    def new(cfo_est):
        def build():
            return [blocks.rotator_cc(-2 * np.pi * cfo_est / 1e6, False),
                    analog.agc3_cc(f(ag['attack_rate']), f(ag['decay_rate']),
                                   f(ag['reference']), f(ag['gain']),
                                   int(f(ag['iir_update_decim'])),
                                   f(ag['max_gain'])),
                    filter.fft_filter_ccc(1, firdes.root_raised_cosine(
                        1.0, 1e6, 250e3, 0.35, 44), 1),
                    digital.symbol_sync_cc(
                        digital.TED_SIGNAL_TIMES_SLOPE_ML, 4,
                        f(ss['loop_bw']), f(ss['damping']), f(ss['ted_gain']),
                        f(ss['max_dev']), 1,
                        digital.constellation_bpsk().base(),
                        digital.IR_MMSE_8TAP, 32, []),
                    digital.costas_loop_cc(f(p['costas_ch0']['w']), 2, False)]
        return build
    res = {}
    for dbfs in (-10.0, -40.0, -55.0):
        x, pays = signal(dbfs, 18e3)
        half = pays[len(pays) // 2:]
        got_o = run(x, old)
        est, _ = P.estimate_cfo(x[:4096], 1e6)        # what the AFC does
        got_n = run(x, new(est or 0.0))
        res[dbfs] = (sum(1 for q in half if q in got_o),
                     sum(1 for q in half if q in got_n), len(half))
    detail = ', '.join('%d dBFS: old %d/%d new %d/%d' % (
        k, v[0], v[2], v[1], v[2]) for k, v in res.items())
    check('r6.0.1 chain: a neighbour 30 dB or more below full scale '
          '(+18 kHz) is not decoded at all - its loops scale with the '
          'signal amplitude and there is no AGC',
          res[-10.0][0] >= res[-10.0][2] - 1
          and res[-40.0][0] == 0 and res[-55.0][0] == 0, detail)
    check('r6.1 chain: the same three signals decode',
          all(v[1] >= v[2] - 1 for v in res.values()), detail)


def g6():
    section('G6 the generated flowgraph end to end (radio replaced by a '
            'file)')
    code = os.path.join(HERE, 'dev', 'grc_rx_check.py')
    res = subprocess.run([sys.executable, code], capture_output=True,
                         text=True, timeout=900, cwd=HERE)
    lines = [l for l in res.stdout.splitlines() if l.startswith('RESULT ')]
    if not lines:
        check('the flowgraph runs on recorded samples', False,
              (res.stdout + res.stderr)[-600:])
        return
    import json
    r = json.loads(lines[-1][7:])
    sent, got, afc = r['sent'], r['got'], r['afc']
    check('the flowgraph runs on 3.6 s of recorded samples without a stall '
          'or a fault', not r['fault'] and r['done'],
          r['fault'] or 'ran in %.1f s' % r['run_s'])
    sent = {int(k): v for k, v in sent.items()}
    got = {int(k): v for k, v in got.items()}
    for k, cfo, lvl in ((1, 18e3, -38), (2, -21e3, -53)):
        frac = got[k] / float(sent[k]) if sent[k] else 0.0
        check('channel %d (%+.0f kHz offset, %d dBFS, own TX 35+ dB above '
              'on channel 0): >= 95 %% of frames after the AFC settles'
              % (k, cfo / 1e3, lvl), frac >= 0.95,
              '%d of %d, AFC estimate %+.0f Hz' % (got[k], sent[k], afc[k]))
    check('channel 3 (empty; the own TX image lands at -450 kHz, outside '
          'every channel): no frames', got[3] == 0, '%d' % got[3])
    check('the corrupted header and the 40 ms gap cost at most the frames '
          'they hit', r['lost_after_faults'] <= 3,
          '%d frames lost around them' % r['lost_after_faults'])


def main():
    try:
        from gnuradio import gr
    except ImportError:
        print('GNU Radio not available - test_grc skipped')
        return 0
    print('GNU Radio %s' % gr.version())
    d = grc()
    g1(d)
    g2()
    g3()
    g4(d)
    g5(d)
    g7(d)
    if os.environ.get('BPSK_GRC_QUICK') != '1':
        g6()
    print('\n%s' % ('all GNU Radio checks passed' if not FAILURES
                    else 'FAILED: ' + ', '.join(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == '__main__':
    sys.exit(main())
