import numpy as np
from gnuradio import gr
import pmt

class header_frame_trim(gr.basic_block):
    def __init__(self):
        gr.basic_block.__init__(self, name="Header Frame Trim", in_sig=None, out_sig=None)
        self.buffer = []
        self.message_port_register_in(pmt.mp("in"))
        self.message_port_register_out(pmt.mp("out"))
        self.set_msg_handler(pmt.mp("in"), self.handle_msg)

    def handle_msg(self, msg):
        meta = pmt.car(msg)
        new_bytes = list(pmt.u8vector_elements(pmt.cdr(msg)))
        self.buffer.extend(new_bytes)

        # Valid node addresses in our network
        VALID_DESTS = [1, 2, 255]
        VALID_SRCS = [1, 2]

        while len(self.buffer) >= 3:
            dest_id = self.buffer[0]
            src_id = self.buffer[1]
            payload_len = self.buffer[2]

            # Aggressively drop inter-packet noise / preamble bytes
            if dest_id not in VALID_DESTS or src_id not in VALID_SRCS or payload_len < 1 or payload_len > 128:
                self.buffer.pop(0)
                continue

            # Total frame length = Header(3) + Payload + CRC(4)
            exact_frame_len = 3 + payload_len + 4

            # Wait until the full packet arrives
            if len(self.buffer) < exact_frame_len:
                break

            # Extract clean packet and publish
            clean_packet = self.buffer[:exact_frame_len]
            pdu_out = pmt.init_u8vector(len(clean_packet), clean_packet)
            self.message_port_pub(pmt.mp("out"), pmt.cons(meta, pdu_out))

            # Advance buffer past the processed packet
            self.buffer = self.buffer[exact_frame_len:]
