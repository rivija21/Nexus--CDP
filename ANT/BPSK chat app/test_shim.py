"""Exercise the GNU Radio block without GNU Radio.

bpsk_duplex_pluto_link_layer.py is the one file that cannot be tested by
test_bpsk_link.py, because it imports pmt and gnuradio.gr. Both are stubbed
here, which is enough to check the parts that actually carry risk: that the
protocol modules load, that a typed line turns into a correctly addressed
frame on the tx_frame port, that the EVM estimator agrees with a channel of
known SNR, and that the UI server starts on the first tick and gives the port
back on stop() so the flowgraph can be restarted.

    python3 test_shim.py
"""

import importlib.util
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

    pmt.PMT_NIL = object()
    pmt.intern = lambda s: Sym(s)
    pmt.string_to_symbol = lambda s: Sym(s)
    pmt.is_symbol = lambda o: isinstance(o, Sym)
    pmt.symbol_to_string = str
    pmt.is_pair = lambda o: isinstance(o, Pair)
    pmt.cdr = lambda o: o.cdr
    pmt.cons = Pair
    pmt.is_u8vector = lambda o: isinstance(o, U8)
    pmt.u8vector_elements = lambda o: list(o.data)
    pmt.init_u8vector = lambda n, lst: U8(bytes(lst))
    sys.modules['pmt'] = pmt

    gr = types.ModuleType('gnuradio.gr')

    class sync_block(object):
        def __init__(self, name='', in_sig=None, out_sig=None):
            self.published = []

        def message_port_register_in(self, port):
            pass

        def message_port_register_out(self, port):
            pass

        def set_msg_handler(self, port, fn):
            pass

        def message_port_pub(self, port, msg):
            self.published.append((str(port), msg))

    gr.sync_block = sync_block
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

    port = 8231
    blk = shim.blk(my_addr=1, peer_addr=2, rx_dir=os.path.join(HERE, 'chat_files'),
                   nickname='Bench A', http_port=port, open_ui=False,
                   tx_freq=905.2e6, rx_freq=910.2e6)
    check('protocol modules load next to the flowgraph', blk.app is not None,
          shim._load_error)
    check('the banner reaches the log port',
          any(p[0] == 'log' for p in blk.published))

    # ------------------------------------------------ EVM against known AWGN
    rng = np.random.default_rng(3)
    for target in (6.0, 12.0, 20.0):
        n = 4096
        sym = np.sign(rng.normal(size=n)).astype(np.complex64)
        sigma = 10 ** (-target / 20.0)
        noise = (rng.normal(size=n) + 1j * rng.normal(size=n)) / np.sqrt(2)
        blk._meas_t = 0.0
        consumed = blk.work([sym + (noise * sigma).astype(np.complex64)], [])
        est = blk.app.radio['snr_db']
        check('SNR estimate tracks a %.0f dB channel' % target,
              abs(est - target) < 1.5, 'estimated %.1f dB' % est)
        check('work() consumes every sample it is given', consumed == n)

    blk._meas_t = 0.0
    blk.work([np.zeros(1024, dtype=np.complex64)], [])
    check('a silent receiver reports no reading rather than a stale one',
          blk.app.radio['snr_db'] is None and not blk.app.radio['locked'])

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

    blk.stop()
    time.sleep(0.4)
    try:
        urllib.request.urlopen('http://127.0.0.1:%d/api/state' % port, timeout=1)
        freed = False
    except Exception:                                       # noqa: BLE001
        freed = True
    check('stop() releases the port so the flowgraph can restart', freed)

    print('\n%s' % ('all shim checks passed' if not FAILURES
                    else 'FAILED: ' + ', '.join(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == '__main__':
    sys.exit(main())
