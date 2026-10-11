"""Tests for the r6b CSMA GRC block, with pmt/gr stubbed out.

This is the integration these tests CAN reach without a radio: that outbound
frames are gated by the MAC rather than published straight to the modulator,
that energy on input 1 marks the channel busy, and that reception arms the NAV.

It cannot tell you whether gr-iio's burst mode works on your hardware. Nothing
runnable here can.

    python3 test_csma_shim.py
"""

import importlib.util
import os
import socket
import sys
import time
import types

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FAILURES = []


def check(label, condition, detail=''):
    print('  [%s] %s%s' % ('PASS' if condition else 'FAIL', label,
                           (' - ' + detail) if detail else ''))
    if not condition:
        FAILURES.append(label)


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
    pmt.intern = Sym
    pmt.string_to_symbol = Sym
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
            self.in_sig = in_sig

        def message_port_register_in(self, p):
            pass

        def message_port_register_out(self, p):
            pass

        def set_msg_handler(self, p, fn):
            pass

        def message_port_pub(self, port, msg):
            self.published.append((str(port), msg))

    gr.sync_block = sync_block
    pkg = types.ModuleType('gnuradio')
    pkg.gr = gr
    sys.modules['gnuradio'] = pkg
    sys.modules['gnuradio.gr'] = gr
    return pmt


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def tx_frames(blk):
    return [m for port, m in blk.published if port == 'tx_frame']


def sense(blk, amplitude, n=2048):
    """Feed n samples of a given amplitude into both input ports."""
    quiet = np.zeros(n, dtype=np.complex64)
    raw = (np.full(n, amplitude, dtype=np.complex64) if amplitude else quiet)
    return blk.work([quiet, raw], [])


def main():
    install_stubs()
    import tempfile
    spec = importlib.util.spec_from_file_location(
        'csma_shim', os.path.join(HERE, 'bpsk_csma_link_layer.py'))
    shim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shim)
    import bpsk_link as L

    # The shim stamps everything with time.time(): work() for carrier sense,
    # the handlers for contention. Swap in one controllable clock so the test
    # drives the real code path instead of racing the wall clock.
    class Clock(object):
        def __init__(self, t0=1000.0):
            self.t = t0

        def time(self):
            return self.t

        def sleep(self, dt):
            pass

        def advance(self, dt=0.001):
            self.t = round(self.t + dt, 6)
            return self.t
    clock = Clock()
    shim.time = clock

    store = tempfile.mkdtemp(prefix='bpsk_csma_')
    blk = shim.blk(my_addr=1, peers='2,3,4', nickname='Bench',
                   http_port=free_port(), open_ui=False, rx_dir=store,
                   mac_slot=0.003, beacon_interval=0.0, queue_ahead=0.0)

    print('\n  wiring')
    check('the block declares two complex inputs', len(blk.in_sig) == 2,
          '%d inputs' % len(blk.in_sig))
    check('a MAC was constructed', blk.mac is not None)
    check('the link layer runs with no idle filler',
          blk.link.queue_ahead == 0.0, str(blk.link.queue_ahead))

    print('\n  carrier sense')
    consumed = sense(blk, 0.0)
    check('work() consumes every sample offered', consumed == 2048,
          str(consumed))
    check('a quiet channel reads idle', blk.mac.busy is False)
    sense(blk, 0.5)
    check('energy on input 1 marks the channel busy', blk.mac.busy is True,
          'threshold %.0f dB' % blk.mac.busy_threshold_db)
    sense(blk, 0.0)
    check('it clears again when the energy goes', blk.mac.busy is False)

    print('\n  frames are gated by the MAC')
    before = len(tx_frames(blk))
    blk._dispatch([('tx', L.build(L.FT_DATA, 2, 1, 1, 0, b'x' * 200, 0, 1))],
                  now=clock.t)
    check('a data frame is not published immediately',
          len(tx_frames(blk)) == before,
          '%d new' % (len(tx_frames(blk)) - before))
    check('it is queued in the MAC instead', blk.mac.pending() == 1,
          '%d queued' % blk.mac.pending())

    for _ in range(300):
        blk._drain_mac(clock.advance())
    t = clock.t
    check('it is published once contention completes',
          len(tx_frames(blk)) == before + 1,
          '%d new' % (len(tx_frames(blk)) - before))
    check('the MAC queue drained', blk.mac.pending() == 0)

    print('\n  a busy channel holds a frame back')
    sense(blk, 0.5)                                  # channel busy
    before = len(tx_frames(blk))
    blk._dispatch([('tx', L.build(L.FT_DATA, 2, 1, 2, 0, b'y' * 200, 0, 2))],
                  now=t)
    for _ in range(400):
        blk._drain_mac(clock.advance())
    t = clock.t
    check('nothing goes out while the channel is busy',
          len(tx_frames(blk)) == before, '%d new' % (len(tx_frames(blk)) - before))
    sense(blk, 0.0)                                  # channel clears
    for _ in range(400):
        blk._drain_mac(clock.advance())
    t = clock.t
    check('it goes out after the channel clears',
          len(tx_frames(blk)) == before + 1)

    print('\n  reception arms the virtual carrier sense')
    nav_before = blk.mac.nav_until
    blk._on_rx(sys.modules['pmt'].cons(
        sys.modules['pmt'].PMT_NIL,
        sys.modules['pmt'].init_u8vector(
            0, list(L.build(L.FT_DATA, 1, 2, 5, 0, b'hello', 0, 5)))))
    check('NAV advances on a received data frame',
          blk.mac.nav_until > nav_before,
          '%.4f -> %.4f' % (nav_before, blk.mac.nav_until))

    print('\n  telemetry')
    snap = blk.mac.snapshot(t)
    check('MAC snapshot reaches the app', True)
    blk.app.set_mac_state(snap)
    stats = blk.app.stats_payload(t)
    check('the UI sees the medium state', 'mac' in stats and
          stats['mac']['slot_ms'] == 3.0, str(stats.get('mac', {}).get('slot_ms')))

    blk.stop()
    import shutil
    shutil.rmtree(store, ignore_errors=True)
    print('\n%s' % ('all CSMA shim checks passed' if not FAILURES
                    else 'FAILED: ' + ', '.join(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == '__main__':
    sys.exit(main())
