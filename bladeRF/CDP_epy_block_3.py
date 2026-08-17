import numpy as np
from gnuradio import gr
import pmt

class add_preamble_pad(gr.basic_block):
    def __init__(self):
        gr.basic_block.__init__(self, name="Add Preamble & Pad", in_sig=None, out_sig=None)
        self.message_port_register_in(pmt.mp("in"))
        self.message_port_register_out(pmt.mp("out"))
        self.set_msg_handler(pmt.mp("in"), self.handle_msg)

    def handle_msg(self, msg):
        meta = pmt.car(msg)
        vec = list(pmt.u8vector_elements(pmt.cdr(msg)))
        
        # Pad short messages to at least 16 bytes
        min_bytes = 16
        if len(vec) < min_bytes:
            vec += [0x00] * (min_bytes - len(vec))
            
        # FIX: Increased dummy preamble from 48 to 128 bytes for cold-start AGC lock
        dummy = [0x55] * 128
        preamble = [0xAC, 0x62, 0x0D, 0xCD]
        
        full_packet = dummy + preamble + vec
        pdu_out = pmt.init_u8vector(len(full_packet), full_packet)
        self.message_port_pub(pmt.mp("out"), pmt.cons(meta, pdu_out))
