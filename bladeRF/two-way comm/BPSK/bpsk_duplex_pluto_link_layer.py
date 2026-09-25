"""GNU Radio shim for the BPSK duplex link layer.

The protocol lives in bpsk_link.py, which must sit in the same directory as
this flowgraph.
"""

import os
import sys
import threading
import time

import pmt
from gnuradio import gr


def _load_protocol():
    """Load bpsk_link.py by absolute path, never by module name.

    GRC validates this block by running exec() on this source in an empty
    namespace, so there is no __file__ and the working directory is wherever
    GRC was started. Loading the file directly makes both irrelevant, and
    swallowing every failure keeps a missing module from painting the block
    red in the editor - __init__ reports it on the console instead.
    """
    import importlib.util

    roots = []
    try:
        roots.append(os.path.dirname(os.path.abspath(__file__)))
    except NameError:
        pass
    roots.append(os.getcwd())
    roots.extend([entry for entry in sys.path if entry])

    for root in roots:
        path = os.path.join(root, 'bpsk_link.py')
        if not os.path.isfile(path):
            continue
        spec = importlib.util.spec_from_file_location('bpsk_link', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules['bpsk_link'] = module
        return module
    return None


try:
    bpsk_link = _load_protocol()
except Exception as _err:
    bpsk_link = None
    _load_error = repr(_err)
else:
    _load_error = 'bpsk_link.py not found next to the flowgraph'


def _to_text(msg):
    """Accept whatever the QT edit box emits and return a str."""
    try:
        if pmt.is_symbol(msg):
            return pmt.symbol_to_string(msg)
        if pmt.is_pair(msg):
            val = pmt.cdr(msg)
            if pmt.is_u8vector(val):
                return bytes(bytearray(pmt.u8vector_elements(val))).decode(
                    'utf-8', 'replace')
            if pmt.is_symbol(val):
                return pmt.symbol_to_string(val)
    except Exception:
        pass
    return None


def _to_bytes(msg):
    """Extract the u8vector of a PDU as bytes."""
    try:
        if pmt.is_pair(msg):
            val = pmt.cdr(msg)
            if pmt.is_u8vector(val):
                return bytes(bytearray(pmt.u8vector_elements(val)))
    except Exception:
        pass
    return None


class blk(gr.sync_block):
    """Addressed stop-and-wait ARQ link layer for the BPSK duplex radio."""

    def __init__(self, my_addr=1, peer_addr=2, ack_timeout=0.5, max_retries=5,
                 frag_size=256, queue_ahead=0.03, sym_rate=250000.0,
                 overhead=460, rx_dir='.'):
        gr.sync_block.__init__(self, name='BPSK Link Layer',
                               in_sig=[], out_sig=[])
        if bpsk_link is None:
            self.link = None
            print('[link] ERROR: could not load bpsk_link.py: %s'
                  % _load_error, flush=True)
        else:
            self.link = bpsk_link.LinkState(
                my_addr, peer_addr, ack_timeout, max_retries, frag_size,
                queue_ahead, sym_rate, overhead, rx_dir)
        for port in ('chat_in', 'rx_frame', 'tick'):
            self.message_port_register_in(pmt.intern(port))
        for port in ('tx_frame', 'log'):
            self.message_port_register_out(pmt.intern(port))
        self.set_msg_handler(pmt.intern('chat_in'), self._on_chat)
        self.set_msg_handler(pmt.intern('rx_frame'), self._on_rx)
        self.set_msg_handler(pmt.intern('tick'), self._on_tick)
        self._lock = threading.RLock()
        self._last_strobe = 0.0
        self._watchdog = None
        if self.link is not None:
            self._say(self.link.banner())

    def _say(self, text):
        for line in str(text).splitlines():
            print('[link] %s' % line, flush=True)
        self.message_port_pub(pmt.intern('log'),
                              pmt.string_to_symbol(str(text)))

    def _dispatch(self, actions):
        for kind, value in actions:
            if kind == 'tx':
                vec = pmt.init_u8vector(len(value), list(bytearray(value)))
                self.message_port_pub(pmt.intern('tx_frame'),
                                      pmt.cons(pmt.PMT_NIL, vec))
            else:
                self._say(value)

    def _ensure_watchdog(self):
        """Start the fallback timer thread on first traffic.

        Started lazily rather than in __init__ so that GRC, which instantiates
        this block every time it validates the flowgraph, never spawns one.
        """
        if self._watchdog is None and self.link is not None:
            self._watchdog = threading.Thread(target=self._watchdog_loop,
                                              daemon=True)
            self._watchdog.start()

    def _watchdog_loop(self):
        """Drive on_tick if the Message Strobe stops delivering.

        Every ARQ timer and the transmit filler hang off the tick port. If that
        port goes quiet the link silently wedges: nothing retransmits, the
        queue never drains, and the Pluto TX buffer starves. This takes over
        after 250 ms of silence and stands down if the strobe returns.
        """
        engaged = False
        while True:
            time.sleep(0.01)
            try:
                with self._lock:
                    if self.link is None:
                        continue
                    if (time.time() - self._last_strobe) < 0.25:
                        if engaged:
                            engaged = False
                            self._say('message strobe recovered, watchdog '
                                      'standing down')
                        continue
                    if not engaged:
                        engaged = True
                        self._say('WARNING: no tick from the Message Strobe '
                                  'block - driving ARQ timers and TX filler '
                                  'from the fallback thread instead')
                    self._dispatch(self.link.on_tick(time.time()))
            except Exception as exc:
                print('[link] watchdog error: %r' % (exc,), flush=True)

    def _on_chat(self, msg):
        if self.link is None:
            return
        text = _to_text(msg)
        if text is None:
            return
        with self._lock:
            self._ensure_watchdog()
            self._dispatch(self.link.on_user(text, time.time()))

    def _on_rx(self, msg):
        if self.link is None:
            return
        raw = _to_bytes(msg)
        if raw is None:
            return
        with self._lock:
            self._ensure_watchdog()
            self._dispatch(self.link.on_rx(raw, time.time()))

    def _on_tick(self, msg):
        if self.link is None:
            return
        with self._lock:
            self._last_strobe = time.time()
            self._ensure_watchdog()
            self._dispatch(self.link.on_tick(time.time()))

    def work(self, input_items, output_items):
        return 0
