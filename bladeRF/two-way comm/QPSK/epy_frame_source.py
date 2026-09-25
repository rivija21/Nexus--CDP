"""QPSK chat -- frame source (GRC embedded block).

Frames every PDU arriving on 'send' and emits it as bytes; transmits PN
filler while idle so the peer's timing/carrier loops never lose lock.
Requires qpsk_framing.py in the same directory as this flowgraph.
"""
import os
import sys
import threading
from collections import deque

import numpy as np
import pmt
from gnuradio import gr

_here = os.path.dirname(os.path.abspath(globals().get('__file__', '.')))
for _p in (os.getcwd(), _here):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)
from qpsk_framing import build_frame, pn_bytes


class blk(gr.sync_block):
    """QPSK chat frame source: PDU in ('send'), framed bytes out."""

    def __init__(self, idle_chunk=1024):
        gr.sync_block.__init__(self, name='frame_source', in_sig=None,
                               out_sig=[np.uint8])
        self.idle_chunk = int(idle_chunk)
        self._lock = threading.Lock()
        self._q = deque()
        self._cur = None
        self._pos = 0
        self._filler = pn_bytes(4096)
        self._fpos = 0
        self.message_port_register_in(pmt.intern('send'))
        self.set_msg_handler(pmt.intern('send'), self._on_msg)

    def _on_msg(self, msg):
        if pmt.is_pair(msg):
            msg = pmt.cdr(msg)
        if pmt.is_u8vector(msg):
            payload = bytes(pmt.u8vector_elements(msg))
        elif pmt.is_symbol(msg):
            payload = pmt.symbol_to_string(msg).encode('utf-8')
        else:
            return
        frame = np.frombuffer(build_frame(payload), dtype=np.uint8)
        with self._lock:
            self._q.append(frame)

    def _idle(self, n):
        out = np.empty(n, dtype=np.uint8)
        got = 0
        while got < n:
            take = min(n - got, len(self._filler) - self._fpos)
            out[got:got + take] = self._filler[self._fpos:self._fpos + take]
            got += take
            self._fpos = (self._fpos + take) % len(self._filler)
        return out

    def work(self, input_items, output_items):
        out = output_items[0]
        n = len(out)
        produced = 0
        while produced < n:
            if self._cur is None or self._pos >= len(self._cur):
                with self._lock:
                    self._cur = self._q.popleft() if self._q else None
                self._pos = 0
                if self._cur is None:
                    take = min(n - produced, self.idle_chunk)
                    out[produced:produced + take] = self._idle(take)
                    produced += take
                    break
            take = min(n - produced, len(self._cur) - self._pos)
            out[produced:produced + take] = self._cur[self._pos:self._pos + take]
            self._pos += take
            produced += take
        return produced
