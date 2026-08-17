import numpy as np
from gnuradio import gr
import pmt

class add_header(gr.basic_block):
    def __init__(self, dest_id=2, src_id=1):
        gr.basic_block.__init__(self, name="Add Header", in_sig=None, out_sig=None)
        self.dest_id = dest_id
        self.src_id = src_id
        self.message_port_register_in(pmt.mp("in"))
        self.message_port_register_out(pmt.mp("out"))
        self.set_msg_handler(pmt.mp("in"), self.handle_msg)

    def handle_msg(self, msg):
        meta = pmt.car(msg)
        payload = list(pmt.u8vector_elements(pmt.cdr(msg)))
        
        # 3-Byte Header: [DEST_ID, SRC_ID, PAYLOAD_LEN]
        header = [self.dest_id, self.src_id, len(payload)]
        packet = header + payload
        
        pdu_out = pmt.init_u8vector(len(packet), packet)
        self.message_port_pub(pmt.mp("out"), pmt.cons(meta, pdu_out))
