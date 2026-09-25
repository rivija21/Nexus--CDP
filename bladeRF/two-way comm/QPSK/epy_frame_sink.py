"""QPSK chat -- frame sink (GRC embedded block).

Consumes the tagged bit stream from Correlate Access Code - Tag and emits one
PDU on 'rx' per header- and CRC-valid frame.
Requires qpsk_framing.py in the same directory as this flowgraph.
"""
import os
import sys

import numpy as np
import pmt
from gnuradio import gr

_here = os.path.dirname(os.path.abspath(globals().get('__file__', '.')))
for _p in (os.getcwd(), _here):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)
from qpsk_framing import FrameAssembler


class blk(gr.sync_block):
    """QPSK chat frame sink: tagged bits in, one PDU out per good frame."""

    def __init__(self, tag_key='pkt_start'):
        gr.sync_block.__init__(self, name='frame_sink', in_sig=[np.uint8],
                               out_sig=None)
        self.tag_key = tag_key
        self._tag = pmt.string_to_symbol(tag_key)
        self.asm = FrameAssembler(self._deliver)
        self.message_port_register_out(pmt.intern('rx'))

    def _deliver(self, payload):
        vec = pmt.init_u8vector(len(payload), list(payload))
        self.message_port_pub(pmt.intern('rx'), pmt.cons(pmt.PMT_NIL, vec))

    def work(self, input_items, output_items):
        in0 = input_items[0]
        n = len(in0)
        tags = self.get_tags_in_window(0, 0, n, self._tag)
        if tags:
            self.asm.add_tags([t.offset for t in tags])
        self.asm.add_bits(self.nitems_read(0), in0)
        self.asm.process()
        return n
