import numpy as np
from gnuradio import gr
import pmt
import sys

class address_ack(gr.basic_block):
    def __init__(self, my_id=2):
        gr.basic_block.__init__(self, name="Address Filter & ACK", in_sig=None, out_sig=None)
        self.my_id = my_id
        self.message_port_register_in(pmt.mp("in"))
        self.message_port_register_out(pmt.mp("payload_out"))
        self.message_port_register_out(pmt.mp("ack_out"))
        self.set_msg_handler(pmt.mp("in"), self.handle_msg)

    def handle_msg(self, msg):
        meta = pmt.car(msg)
        vec = list(pmt.u8vector_elements(pmt.cdr(msg)))
        
        if len(vec) < 3:
            return
        
        dest_id = vec[0]
        src_id = vec[1]
        payload_len = vec[2]
        
        if len(vec) < 3 + payload_len:
            return

        payload = vec[3 : 3 + payload_len]

        # Accept frames for My ID (2), Broadcast (255), or Sender ID (1)
        if dest_id == self.my_id or dest_id == 255 or dest_id == 1:
            is_ack = (payload == [0x41, 0x43, 0x4B]) # "ACK"
            
            if not is_ack:
                msg_text = bytes(payload).decode('utf-8', errors='ignore').strip()
                
                # Use sys.__stdout__ to bypass GNU Radio's GUI logging capture
                sys.__stdout__.write("\n=================================\n")
                sys.__stdout__.write(f"[DECODED PAYLOAD]: {msg_text}\n")
                sys.__stdout__.write("=================================\n\n")
                sys.__stdout__.flush()

                # Send ACK frame back to transmitter
                ack_data = [src_id, self.my_id, 3, 0x41, 0x43, 0x4B] 
                ack_pdu = pmt.init_u8vector(len(ack_data), ack_data)
                self.message_port_pub(pmt.mp("ack_out"), pmt.cons(meta, ack_pdu))
            else:
                sys.__stdout__.write("\n=================================\n")
                sys.__stdout__.write(f"[ACK RECEIVED] From Node {src_id}\n")
                sys.__stdout__.write("=================================\n\n")
                sys.__stdout__.flush()
            
            pdu_out = pmt.init_u8vector(len(payload), payload)
            self.message_port_pub(pmt.mp("payload_out"), pmt.cons(meta, pdu_out))
