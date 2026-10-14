"""BPSK deframer for one receive channel (r6.1, embedded block).

Input: hard-decision bits (0/1, one per byte) from the differential decoder,
grouped by a Stream to Vector block into vectors of `batch` bits.
Output, message port 'pdus': one PDU per link frame, metadata {'ch': k}; and
once a second a PDU with an empty payload and metadata {'ch': k, 'stats':
{...}}, which the node block uses as this channel's liveness check.

The search is bpsk_phy.Deframer (next to the flowgraph). It replaces
correlate_access_code_bb_ts -> repack_bits_bb -> tagged_stream_to_pdu, which
on GNU Radio 3.10.12 could wait for 65535 bytes after one corrupted header
and so stall the whole receiver ('repack_bits_bb is requesting more input
data than we can provide'). This block never waits for data it does not
have: it takes whatever whole vectors are there (Python is entered at most
~60 times a second per channel instead of once per few symbols, and the
scheduler never calls into Python just to ask - a basic_block's forecast()
would be a Python call on every scheduler pass), drops an implausible
length at once, and keeps looking for access codes inside a frame it is
still collecting, so one bad header costs at most that one frame.
"""

import os
import sys
import time
import traceback

import numpy as np
import pmt
from gnuradio import gr


def _load(name):
    """Load a sibling module by path (GRC validates with exec() and no
    __file__, from whatever directory it was started in)."""
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
    """BPSK deframer: bits in, link frames out as PDUs."""

    def __init__(self, channel=0, max_len=600, batch=8192):
        gr.sync_block.__init__(self, name='BPSK Deframer',
                               in_sig=[(np.uint8, int(batch))], out_sig=None)
        self.channel = int(channel)
        self.batch = int(batch)
        self._port = pmt.intern('pdus')
        self.message_port_register_out(self._port)
        self._core = (_phy.Deframer(max_len=int(max_len))
                      if _phy is not None else None)
        self._ch = pmt.from_long(self.channel)
        self._k_ch = pmt.intern('ch')
        self._k_stats = pmt.intern('stats')
        self._empty = pmt.init_u8vector(0, [])
        self._next = 0.0
        self._faults = 0

    def work(self, input_items, output_items):
        x = input_items[0]
        n = len(x)
        frames = ()
        if n and self._core is not None:
            try:
                frames = self._core.feed(x.reshape(-1))
            except Exception:                     # noqa: BLE001
                self._faults += 1
                if self._faults <= 3:
                    traceback.print_exc()
        for f in frames:
            meta = pmt.dict_add(pmt.make_dict(), self._k_ch, self._ch)
            self.message_port_pub(self._port, pmt.cons(
                meta, pmt.init_u8vector(len(f), list(f))))
        now = time.time()
        if now >= self._next:
            self._next = now + 1.0
            st = dict(self._core.stats) if self._core is not None else {}
            st['faults'] = self._faults
            if self._core is None:
                st['error'] = _err
            meta = pmt.dict_add(pmt.make_dict(), self._k_ch, self._ch)
            meta = pmt.dict_add(meta, self._k_stats, pmt.to_pmt(st))
            self.message_port_pub(self._port, pmt.cons(meta, self._empty))
        return n
