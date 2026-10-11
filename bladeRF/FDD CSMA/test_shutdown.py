"""Shutdown-path regression test for the GRC block.

Covers the two defects that made the flowgraph crash intermittently in r5:

  1. The watchdog thread ran `while True:` and stop() never signalled it, so it
     kept calling message_port_pub while the runtime destroyed the block. Since
     stop() also freezes _last_strobe, the watchdog ENGAGED 250 ms into
     teardown and then published at 100 Hz - the steady state, not a race.
  2. stop() set _server = None, which _ensure_running() could not tell apart
     from "never started", so any in-flight message re-bound the TCP port and
     restarted the watchdog during teardown.

Run:  python3 test_shutdown.py
"""

import importlib.util
import os
import socket
import sys
import time
import types

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
            self.torn_down = False

        def message_port_register_in(self, port):
            pass

        def message_port_register_out(self, port):
            pass

        def set_msg_handler(self, port, fn):
            pass

        def message_port_pub(self, port, msg):
            # Stands in for the C++ call. After teardown the real one is a
            # use-after-free, which is a segfault, not an exception.
            if self.torn_down:
                raise AssertionError('message_port_pub after teardown: %s' % port)
            self.published.append((str(port), msg))

    gr.sync_block = sync_block
    pkg = types.ModuleType('gnuradio')
    pkg.gr = gr
    sys.modules['gnuradio'] = pkg
    sys.modules['gnuradio.gr'] = gr
    return pmt


def free_port():
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def main():
    pmt = install_stubs()
    spec = importlib.util.spec_from_file_location(
        'shim', os.path.join(HERE, 'bpsk_duplex_pluto_link_layer.py'))
    shim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shim)

    import tempfile
    store = tempfile.mkdtemp(prefix='bpsk_shutdown_')
    port = free_port()
    blk = shim.blk(my_addr=1, peer_addr=2, nickname='Bench', http_port=port,
                   open_ui=False, rx_dir=store)

    print('\n--- start, then let the watchdog engage ---')
    blk._on_tick(None)
    time.sleep(0.45)                       # past the 250 ms engage threshold
    check('watchdog is running before stop', blk._watchdog is not None
          and blk._watchdog.is_alive())
    check('UI server is bound before stop', bool(blk._server))
    watchdog = blk._watchdog

    print('\n--- stop() ---')
    t0 = time.time()
    blk.stop()
    stop_ms = (time.time() - t0) * 1000.0
    check('stop() returns promptly', stop_ms < 1500, '%.0f ms' % stop_ms)
    check('watchdog thread has exited', not watchdog.is_alive())
    check('server handle released', blk._server is None)

    n_before = len(blk.published)
    time.sleep(0.6)                        # 60 watchdog periods
    after = len(blk.published) - n_before
    check('nothing is published after stop()', after == 0,
          '%d messages in 600 ms' % after)

    print('\n--- the runtime now destroys the block ---')
    blk.torn_down = True
    time.sleep(0.3)
    check('no port access once the block is torn down', True,
          'no AssertionError raised')

    print('\n--- in-flight messages arriving during teardown ---')
    blk._on_chat(pmt.intern('hello'))
    blk._on_tick(None)
    check('a late console message does not re-open the server',
          blk._server is None, repr(blk._server))
    check('a late tick does not restart the watchdog',
          blk._watchdog is None, repr(blk._watchdog))

    print('\n--- stop() is idempotent ---')
    blk.stop()
    check('second stop() is harmless', True)

    import shutil
    shutil.rmtree(store, ignore_errors=True)
    print('\n%s' % ('all shutdown checks passed' if not FAILURES
                    else 'FAILED: ' + ', '.join(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == '__main__':
    sys.exit(main())
