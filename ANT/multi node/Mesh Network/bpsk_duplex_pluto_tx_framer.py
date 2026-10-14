"""BPSK transmit framer (r6.1, embedded block).

Message port 'frames' in: link frames (PDUs) from the node block. Port 'ctl'
in: {'enable': bool}. Stream out: BPSK symbols, one per symbol period, to the
interpolating RRC filter. Port 'status' out, every 50 ms: the transmit
backlog, so the node's airtime model tracks the real queue.

The radio pulls symbols at exactly its own rate, so this block's queue is
the whole transmit backlog. While enabled, gaps between frames are filled
with a PRBS-15 pattern (no access code can appear in it) so the carrier is
continuous without the link layer making ~200 IDLE frames a second; while
disabled (the MAC listening or checking its channel) it sends zeros, which
keeps the DAC fed - no 'U' - and the channel silent. Switching off is
immediate: the frame on air and any still queued are discarded (the link
layer's ARQ resends what mattered). The framing and modulation
are bpsk_phy.TxFramer, identical on air to r6.0's tagged_stream_mux +
generic_mod(differential BPSK).
"""

import os
import sys
import time
import traceback

import numpy as np
import pmt
from gnuradio import gr


def _load(name):
    """Load a sibling module by path (see the deframer block)."""
    import importlib.util
    if name in sys.modules:
        return sys.modules[name]
    roots = []
    try:
        roots.append(os.path.dirname(os.path.abspath(__file__)))
    except NameError:
        pass
    roots.append(os.getcwd())
    roots.extend([p for p in sys.path if p])
    for root in roots:
        path = os.path.join(root, name + '.py')
        if os.path.isfile(path):
            spec = importlib.util.spec_from_file_location(name, path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            try:
                spec.loader.exec_module(mod)
            except Exception:
                sys.modules.pop(name, None)
                raise
            return mod
    return None


try:
    _phy = _load('bpsk_phy')
    _err = '' if _phy is not None else 'bpsk_phy.py not found'
except Exception as _e:          # noqa: BLE001 - reported at run time
    _phy, _err = None, repr(_e)


class blk(gr.sync_block):
    """BPSK TX framer: link frames in, continuous symbols out."""

    def __init__(self, preamble_size=64, postamble_size=64,
                 sym_rate=250000.0, max_backlog=0.5, batch=2560):
        gr.sync_block.__init__(self, name='BPSK TX Framer', in_sig=None,
                               out_sig=[np.complex64])
        self.set_output_multiple(max(64, int(batch)))
        self._in = pmt.intern('frames')
        self._ctl = pmt.intern('ctl')
        self._out = pmt.intern('status')
        self.message_port_register_in(self._in)
        self.message_port_register_in(self._ctl)
        self.message_port_register_out(self._out)
        # GNU Radio finds a Python handler again BY NAME: real methods only.
        self.set_msg_handler(self._in, self._on_frame)
        self.set_msg_handler(self._ctl, self._on_ctl)
        self._core = (_phy.TxFramer(preamble_size, postamble_size, sym_rate,
                                    max_backlog) if _phy is not None else None)
        self._enabled = False
        self._flushed = 0
        self._next = 0.0
        self._faults = 0
        self._sent_syms = 0

    def _on_frame(self, msg):
        try:
            if self._core is None or not pmt.is_pair(msg):
                return
            vec = pmt.cdr(msg)
            if pmt.is_u8vector(vec):
                self._core.push(bytes(pmt.u8vector_elements(vec)))
        except Exception:                         # noqa: BLE001
            self._fault()

    def _on_ctl(self, msg):
        try:
            if pmt.is_dict(msg):
                val = pmt.dict_ref(msg, pmt.intern('enable'), pmt.PMT_NIL)
                if pmt.is_bool(val):
                    on = pmt.to_bool(val)
                    if not on and self._core is not None:
                        c = self._core
                        self._flushed += len(c.queue) + (c.cur is not None)
                        c.queue.clear()
                        c.queued_bits = 0
                        c.cur, c.pos = None, 0
                    self._enabled = on
        except Exception:                         # noqa: BLE001
            self._fault()

    def _fault(self):
        self._faults += 1
        if self._faults <= 3:
            traceback.print_exc()

    def work(self, input_items, output_items):
        out = output_items[0]
        n = len(out)
        core = self._core
        if core is None or not self._enabled:
            out[:] = 0
        else:
            try:
                out[:] = core.next_symbols(n)
            except Exception:                     # noqa: BLE001
                self._fault()
                out[:] = 0
        self._sent_syms += n
        now = time.time()
        if now >= self._next:
            self._next = now + 0.05
            st = {'t': now, 'enabled': self._enabled,
                  'symbols': self._sent_syms, 'flushed': self._flushed,
                  'faults': self._faults}
            if core is not None:
                st.update(core.stats)
                st['backlog'] = core.backlog()
            else:
                st['error'] = _err
            self.message_port_pub(self._out, pmt.to_pmt(st))
        return n
