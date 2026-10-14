"""Exercise the GNU Radio node block without GNU Radio.

bpsk_duplex_pluto_link_layer.py is the one file that cannot be tested by
test_bpsk_link.py, because it imports pmt and gnuradio.gr. Both are stubbed
here, which is enough to check the parts that carry risk: that the protocol
modules load, that the block has no stream ports (r6.1: it can never hold the
receiver back), that every message handler is a named method, that a typed
line turns into a correctly addressed frame, that the measurement snapshots
(symbols, channel IQ, raw ADC) reach the application, that the transmitter is
placed on the one-sided channel plan, that gain and transmit-enable commands
leave as IIO attribute writes in range, that the transmit framer's report
corrects the airtime model, that a dead receive chain is reported, and that
the UI server starts on the first tick and gives the port back on stop().

    python3 test_shim.py
"""

import importlib.util
import math
import os
import sys
import time
import types
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


# --------------------------------------------------------------- gr / pmt stub
def install_stubs():
    pmt = types.ModuleType('pmt')

    class Sym(str):
        pass

    class Pair(object):
        def __init__(self, car, cdr):
            self.car, self.cdr = car, cdr

    class U8(object):
        def __init__(self, data):
            self.data = bytes(data)

    class C32(object):
        def __init__(self, data):
            self.data = list(data)

    pmt.PMT_NIL = None
    pmt.intern = lambda s: Sym(s)
    pmt.string_to_symbol = lambda s: Sym(s)
    pmt.is_symbol = lambda o: isinstance(o, Sym)
    pmt.symbol_to_string = str
    pmt.is_pair = lambda o: isinstance(o, Pair)
    pmt.car = lambda o: o.car
    pmt.cdr = lambda o: o.cdr
    pmt.cons = Pair
    pmt.is_u8vector = lambda o: isinstance(o, U8)
    pmt.u8vector_elements = lambda o: list(o.data)
    pmt.init_u8vector = lambda n, lst: U8(bytes(lst))
    pmt.is_c32vector = lambda o: isinstance(o, C32)
    pmt.c32vector_elements = lambda o: list(o.data)
    pmt.init_c32vector = lambda n, lst: C32(lst)
    pmt.is_dict = lambda o: isinstance(o, dict)
    pmt.make_dict = lambda: {}
    pmt.dict_add = lambda d, k, v: dict(d, **{str(k): v})
    pmt.to_python = lambda o: o
    pmt.from_double = float
    pmt.from_bool = bool
    sys.modules['pmt'] = pmt

    gr = types.ModuleType('gnuradio.gr')

    class basic_block(object):
        def __init__(self, name='', in_sig=None, out_sig=None):
            self.published = []
            self.sigs = (in_sig, out_sig)
            self.ports_in, self.ports_out = [], []

        def message_port_register_in(self, port):
            self.ports_in.append(str(port))

        def message_port_register_out(self, port):
            self.ports_out.append(str(port))

        def set_msg_handler(self, port, fn):
            # As GNU Radio's gateway: the handler is found again BY NAME.
            if getattr(self, getattr(fn, '__name__', '<none>'), None) is None:
                raise AttributeError('handler %r is not a method of the '
                                     'block' % fn)
            self.handlers = getattr(self, 'handlers', {})
            self.handlers[str(port)] = fn

        def message_port_pub(self, port, msg):
            self.published.append((str(port), msg))

    gr.basic_block = basic_block
    gr.sync_block = basic_block
    pkg = types.ModuleType('gnuradio')
    pkg.gr = gr
    sys.modules['gnuradio'] = pkg
    sys.modules['gnuradio.gr'] = gr
    return pmt


FAILURES = []


def check(label, cond, detail=''):
    print('  [%s] %s%s' % ('PASS' if cond else 'FAIL', label,
                           (' - ' + detail) if detail else ''))
    if not cond:
        FAILURES.append(label)


def main():
    pmt = install_stubs()
    import numpy as np

    spec = importlib.util.spec_from_file_location(
        'shim', os.path.join(HERE, 'bpsk_duplex_pluto_link_layer.py'))
    shim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shim)

    import bpsk_app as A
    import bpsk_link as L
    import bpsk_phy as P

    def pdu(x, meta=None):
        x = np.asarray(x, dtype=np.complex64)
        return pmt.cons(meta, pmt.init_c32vector(len(x), list(x)))

    import shutil
    import tempfile
    scratch = tempfile.mkdtemp(prefix='bpsk_shim_')
    port = 8231
    blk = shim.blk(my_addr=1, peer_addr=2, rx_dir=scratch,
                   nickname='Bench A', http_port=port, open_ui=False,
                   center_freq=910.2e6, security=False, count_ou=False)
    check('protocol modules load next to the flowgraph', blk.app is not None,
          shim._load_error)
    check('the banner reaches the log port',
          any(p[0] == 'log' for p in blk.published))
    check('the node block has no stream ports (it can never hold the '
          'receiver or the transmitter back)', blk.sigs == (None, None),
          repr(blk.sigs))
    want_in = {'chat_in', 'rx_frame', 'tick', 'adc', 'txstat'} | {
        '%s%d' % (p, k) for p in ('rx', 'meas', 'iq') for k in range(4)}
    check('every input port has a named handler method',
          set(blk.handlers) == want_in and set(blk.ports_in) == want_in,
          repr(sorted(set(blk.ports_in) ^ want_in)))

    # ------------------------------------------------ EVM against known AWGN
    rng = np.random.default_rng(3)
    n = 512
    for target in (6.0, 12.0, 20.0):
        sym = np.sign(rng.normal(size=n)).astype(np.complex64)
        sigma = 10 ** (-target / 20.0)
        noise = (rng.normal(size=n) + 1j * rng.normal(size=n)) / np.sqrt(2)
        blk.handlers['meas1'](pdu(sym + noise * sigma))
        est = blk.app.radio_ch[1]['snr_db']
        check('SNR estimate tracks a %.0f dB channel' % target,
              est is not None and abs(est - target) < 1.5,
              'estimated %s dB' % est)
    blk.handlers['meas1'](pdu(np.zeros(n)))
    check('a silent receiver reports no reading rather than a stale one',
          blk.app.radio_ch[1]['snr_db'] is None
          and not blk.app.radio_ch[1]['locked'])

    # ------------------------------------------------ channel levels (iq taps)
    sps = 4
    taps = P.rrc_taps(sps, 0.35, 16)
    bits = rng.integers(0, 2, 1200) * 2.0 - 1.0
    up = np.zeros(len(bits) * sps, np.complex64)
    up[::sps] = bits
    sig = np.convolve(up, taps)[:4096].astype(np.complex64)
    sig *= 0.3 / np.max(np.abs(sig))
    quiet = ((rng.normal(size=4096) + 1j * rng.normal(size=4096))
             * 1e-4).astype(np.complex64)
    for k in range(4):
        blk.handlers['iq%d' % k](pdu(sig + quiet if k == 1 else quiet))
    blk._push_levels(time.time() + 1.0)
    lv = [blk._lvl[k][1] for k in range(4)]
    check('per-channel in-band levels are measured (occupied channel '
          '~60 dB above the quiet ones)', lv[1] - lv[0] > 50, repr(lv))
    check('levels are plain floats (readable log lines)',
          all(type(x) is float for x in lv))

    # ------------------------------------------------------- ADC snapshots
    adc = (0.1 * (rng.normal(size=8192) + 1j * rng.normal(size=8192))
           ).astype(np.complex64)
    hot = adc * 30
    hot = (np.clip(hot.real, -1, 1) + 1j * np.clip(hot.imag, -1, 1)).astype(
        np.complex64)
    blk.handlers['adc'](pdu(hot))
    a = blk.app.adc
    check('the ADC meter reports peak and clipping',
          a['peak_dbfs'] is not None and a['peak_dbfs'] > -0.5
          and a['clip'] > 0.1, repr({k: a.get(k) for k in ('peak_dbfs',
                                                          'clip')}))
    t0 = time.time()
    blk.app.adc['ts'] = t0 + 1.2
    out = []
    blk.app._check_adc(out, t0)              # not the link: no side effects
    blk.app._check_adc(out, t0 + 1.2)
    check('an overloaded receiver is reported in the chat',
          any('receiver overloaded' in m.get('text', '')
              for m in blk.app.messages))

    # ------------------------------------------- transmit framer feedback
    blk.link.radio.busy_until = 0.0
    now = time.time()
    blk.handlers['txstat']({'t': now, 'enabled': True, 'backlog': 0.25,
                            'accepted': 0, 'dropped': 0, 'rejected': 0})
    check('the transmit framer\'s backlog report sets the airtime model '
          '(no open-loop drift)',
          abs(blk.link.radio.busy_until - (now + 0.25)) < 0.05,
          '%.3f s ahead' % (blk.link.radio.busy_until - now))

    # ------------------------------------------------------- server and ports
    blk._on_tick(None)
    time.sleep(0.5)
    try:
        body = urllib.request.urlopen(
            'http://127.0.0.1:%d/api/state' % port, timeout=3).read()
        served = b'"Bench A"' in body
    except Exception as exc:                                # noqa: BLE001
        served, body = False, repr(exc).encode()
    check('the UI server comes up on the first tick', served,
          body[:80].decode('utf-8', 'replace'))
    rxa = [m for name, m in blk.published if name == 'rx_attr']
    check('the receiver RF bandwidth is set through the attribute sink '
          '(the Pluto source block ignores its own bandwidth field)',
          any('rf_bandwidth' in m for m in rxa), repr(rxa[:2]))

    blk._on_chat(pmt.intern('hello over the air'))
    time.sleep(0.1)
    frames = []
    for name, msg in blk.published:
        if name != 'tx_frame':
            continue
        try:
            frames.append(L.parse(msg.cdr.data))
        except L.BadFrame:
            pass
    data = [f for f in frames
            if f['type'] == L.FT_DATA and f['dst'] == 2 and f['src'] == 1]
    check('typed text leaves as an addressed DATA frame', bool(data),
          '%d data frames among %d transmitted' % (len(data), len(frames)))
    env = A.unpack_envelope(data[-1]['payload']) if data else None
    check('the frame carries the application envelope',
          env is not None and env['nick'] == 'Bench A'
          and env['body'] == b'hello over the air', repr(env)[:80])
    check('no frame is longer than the deframer accepts (600 B)',
          all(len(m.cdr.data) <= P.MAX_FRAME for name, m in blk.published
              if name == 'tx_frame'))

    ctl = [m for name, m in blk.published if name == 'tx_ctl']
    want = 2 * math.pi * 450e3 / 6e6
    check('the transmitter is placed on the one-sided plan (static node 1: '
          'channel 0 at +450 kHz; r6.0.1 used -900 kHz, whose image fell '
          'on channel 3)', ctl and abs(ctl[0].get('inc', 0) - want) < 1e-9,
          repr(ctl[:1]))
    frame = L.build(L.FT_DATA, 1, 2, 0, 0, b'x', 5 << L.EPOCH_SHIFT, 0)
    before = blk.app.ch_frames[3]
    blk.handlers['rx3'](pmt.cons(None, pmt.init_u8vector(
        len(frame), list(frame))))
    check('a frame on port rx3 is attributed to channel 3',
          blk.app.ch_frames[3] == before + 1)

    # ------------------------------------------------------- gain commands
    blk.published[:] = []
    blk._dispatch([('gain', {'what': 'rx', 'value': 24, 'reason': 't'}),
                   ('gain', {'what': 'tx', 'value': 55, 'reason': 't'}),
                   ('txen', False)])
    rxa = [m for name, m in blk.published if name == 'rx_attr']
    txa = [m for name, m in blk.published if name == 'tx_attr']
    en = [m for name, m in blk.published if name == 'tx_en']
    check('RX gain leaves as hardwaregain on the receiver attribute sink',
          rxa == [{'hardwaregain': '24'}], repr(rxa))
    check('TX attenuation leaves as a negative hardwaregain on the '
          'transmitter attribute sink', txa == [{'hardwaregain': '-55'}],
          repr(txa))
    check('transmit enable reaches the framer', en == [{'enable': False}],
          repr(en))

    # ------------------------------------------------------- liveness
    blk._started = time.time() - 60.0
    stale = time.time() - 30.0
    blk._seen = {'meas': [time.time()] * 4, 'iq': [time.time()] * 4,
                 'dfr': [time.time(), time.time(), stale, time.time()],
                 'adc': time.time()}
    blk._housekeeping(time.time())
    live = blk.app.phy.get('live') or {}
    check('a stalled receive chain is named, the others reported running',
          live.get('channels') == [True, True, False, True]
          and 'channel 2 stalled' in (live.get('fault') or ''),
          repr(live.get('fault')))
    blk._seen['adc'] = stale
    blk._housekeeping(time.time())
    live = blk.app.phy.get('live') or {}
    check('no samples at all is reported as a receiver stall',
          'no samples from the radio' in (live.get('fault') or ''),
          repr(live.get('fault')))

    blk.stop()
    time.sleep(0.4)
    freed_port = port
    try:
        urllib.request.urlopen('http://127.0.0.1:%d/api/state' % port, timeout=1)
        freed = False
    except Exception:                                       # noqa: BLE001
        freed = True
    check('stop() releases the port so the flowgraph can restart', freed)

    # ------------------------------------------------------- stderr tap
    tap = shim.StderrTap()
    tap.start()
    os.write(2, b'O')
    os.write(2, b'\n[link] hello OK\n')
    os.write(2, b'U')
    time.sleep(0.3)
    tap.stop()
    check('the stderr tap counts gr-iio\'s O and U marks (and not the O in '
          'ordinary words)', (tap.overflows, tap.underflows) == (1, 1),
          '%r %r' % (tap.overflows, tap.underflows))
    os.write(2, b'')
    check('after stop() stderr is the real stderr again', not tap.active)

    # ------------------------------------------------- with encryption on
    import bpsk_keys
    import bpsk_secure as S
    keys = os.path.join(scratch, 'sec')
    bpsk_keys.provision({1: os.path.join(keys, 'keys')})   # peer NOT in roster
    sblk = shim.blk(my_addr=1, peer_addr=2, rx_dir=keys, nickname='Bench A',
                    http_port=freed_port, open_ui=False, security=True,
                    keys_dir='keys', count_ou=False)
    check('security is on by default and loads the keys',
          sblk.app.sec is not None, sblk.app.sec_error or '')
    sblk._ensure_running = lambda: None
    sblk._on_tick(None)
    sblk._on_chat(pmt.intern('must not leave in clear'))
    sblk._on_tick(None)
    sent = [L.parse(m.cdr.data) for n, m in sblk.published if n == 'tx_frame']
    check('with no key for the peer, typed text is held, never sent in clear',
          not any(b'must not leave' in f['payload'] for f in sent)
          and all(f['type'] == L.FT_IDLE for f in sent),
          '%d frames, types %s' % (len(sent), sorted({f['type'] for f in sent})))
    check('the operator is told the peer key is missing',
          any('not in' in m.get('text', '') for m in sblk.app.messages))
    bpsk_keys.provision({1: os.path.join(keys, 'keys'),
                         2: os.path.join(keys, 'peer')})
    sblk.app.sec.keys.reload_roster()
    sblk.app.sec.peers[2].next_try = 0.0
    sblk._on_tick(None)
    sent = [L.parse(m.cdr.data) for n, m in sblk.published if n == 'tx_frame']
    check('once the roster has the peer, a Noise handshake goes out',
          any(f['type'] == S.FT_HS_INIT for f in sent))
    sblk.stop()

    # ------------------------------------------------------------ mesh mode
    mkeys = os.path.join(scratch, 'mesh')
    bpsk_keys.provision_mesh({'m': os.path.join(mkeys, 'keys')})
    mblk = shim.blk(rx_dir=mkeys, http_port=freed_port, open_ui=False,
                    count_ou=False)
    mblk._ensure_running = lambda: None
    check('defaults (my_addr 0, peer_addr 0) start a mesh node',
          mblk.app.mesh and mblk.app.addrm is not None
          and mblk.app.sec is not None, mblk.app.sec_error or '')
    t0 = time.time()
    for _ in range(3):
        mblk._on_tick(None)
    early = [m for n, m in mblk.published if n == 'tx_frame']
    check('it listens before transmitting anything', not early)
    real = time.time
    time.time = lambda: real() + 5.0
    try:
        mblk._on_tick(None)
    finally:
        time.time = real
    sent = [L.parse(m.cdr.data) for n, m in mblk.published if n == 'tx_frame']
    ctl = [m for n, m in mblk.published if n == 'tx_ctl']
    ch = mblk.app.chan.channel
    want = (2 * math.pi * mblk.app.offset(ch) / 6e6) if ch is not None else 0
    check('after listening it claims a channel, retunes and beacons',
          ch is not None and ctl and abs(ctl[-1]['inc'] - want) < 1e-9
          and mblk.app.offset(ch) > 0
          and any(f['type'] == L.FT_BEACON for f in sent),
          'channel %s after %.1f s' % (ch, time.time() - t0))
    mblk.stop()

    shutil.rmtree(scratch, ignore_errors=True)
    print('\n%s' % ('all shim checks passed' if not FAILURES
                    else 'FAILED: ' + ', '.join(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == '__main__':
    sys.exit(main())
