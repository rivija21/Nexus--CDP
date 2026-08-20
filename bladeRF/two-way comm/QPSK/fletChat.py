"""
Standalone correctness test for link_layer.py -- no GNU Radio required.

Replaces the GRC flowgraph with a tiny "FakeRadio" relay thread that does
exactly what Node*_TRX.grc's Frame Trim -> CRC32(check) -> Address Filter +
Auto-ACK chain does: forward a frame from A's TX port to B's RX port, and if
it isn't itself an ACK, synthesize + send back an ACK. This validates
link_layer.py's fragmentation, stop-and-wait ARQ, and reassembly logic in
isolation. Also drops a fraction of frames to prove retry/backoff works.
"""
import random
import socket
import struct
import threading
import time

from link_layer import LinkLayer, HDR_FMT, HDR_LEN, TYPE_ACK, TYPE_TEXT, TYPE_IMAGE

random.seed(0)


class FakeRadioLink:
    """Cross-wires two (tx_port, rx_port) pairs and auto-ACKs, simulating
    what the two GRC flowgraphs would do over real RF."""

    def __init__(self, a_tx, a_rx, b_tx, b_rx, drop_rate=0.0):
        self.drop_rate = drop_rate
        self._running = True
        self._threads = [
            threading.Thread(target=self._relay, args=(a_tx, b_rx, a_rx), daemon=True),
            threading.Thread(target=self._relay, args=(b_tx, a_rx, b_rx), daemon=True),
        ]
        for t in self._threads:
            t.start()

    def _relay(self, listen_port, forward_port, ack_return_port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", listen_port))
        fwd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        while self._running:
            try:
                data, _ = sock.recvfrom(4096)
            except OSError:
                return
            if random.random() < self.drop_rate:
                continue  # simulate a lost frame
            fwd.sendto(data, ("127.0.0.1", forward_port))

            dest, src, msg_id, frag_idx, frag_tot, msg_type, length = struct.unpack(
                HDR_FMT, data[:HDR_LEN])
            if msg_type != TYPE_ACK:
                ack = struct.pack(HDR_FMT, src, dest, msg_id, frag_idx, 1, TYPE_ACK, 0)
                fwd.sendto(ack, ("127.0.0.1", ack_return_port))


_port_base = [60100]


def run_test(drop_rate, payload_size, label):
    base = _port_base[0]
    _port_base[0] += 10
    A_TX, A_RX, B_TX, B_RX = base + 1, base + 2, base + 3, base + 4
    radio = FakeRadioLink(A_TX, A_RX, B_TX, B_RX, drop_rate=drop_rate)

    node_a = LinkLayer(my_id=1, grc_tx_port=A_TX, grc_rx_port=A_RX, ack_timeout=0.3, max_attempts=8)
    node_b = LinkLayer(my_id=2, grc_tx_port=B_TX, grc_rx_port=B_RX, ack_timeout=0.3, max_attempts=8)

    received = {}
    done = threading.Event()

    def on_msg(src, msg_type, payload):
        received["src"] = src
        received["type"] = msg_type
        received["payload"] = payload
        done.set()

    node_b.on_message = on_msg

    payload = bytes(random.randrange(256) for _ in range(payload_size))
    t0 = time.time()
    ok = node_a.send(dest_id=2, msg_type=TYPE_IMAGE if payload_size > 200 else TYPE_TEXT, data=payload)
    dt = time.time() - t0

    got = done.wait(10.0)
    assert ok, f"[{label}] send() reported failure"
    assert got, f"[{label}] receiver never completed reassembly"
    assert received["payload"] == payload, f"[{label}] payload mismatch after reassembly"
    assert received["src"] == 1

    n_frags = max(1, (payload_size + 179) // 180)
    print(f"[{label}] OK -- {payload_size}B in {n_frags} fragment(s), drop_rate={drop_rate}, "
          f"{dt:.2f}s")

    node_a.close()
    node_b.close()
    radio._running = False


run_test(drop_rate=0.0, payload_size=40, label="short text, no loss")
run_test(drop_rate=0.0, payload_size=2000, label="multi-fragment image, no loss")
run_test(drop_rate=0.3, payload_size=2000, label="multi-fragment image, 30% frame loss")

print("ALL TESTS PASSED")
