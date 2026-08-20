"""
link_layer.py -- MAC-layer glue between the GNU Radio PHY (Node1_TRX.grc /
Node2_TRX.grc, reached over UDP) and an application such as flet_chat.py.

Wire format (see SYSTEM_DESIGN.md section 4) -- 7-byte header, big-endian,
unsigned bytes:

    dest_id | src_id | msg_id | frag_idx | frag_tot | type | len | payload...

GNU Radio's "Frame Trim" -> "CRC32 (check)" -> "Address Filter + Auto-ACK"
chain already: (a) discards anything that fails CRC, (b) discards anything
not addressed to this node, and (c) auto-generates + transmits an ACK for
any non-ACK frame it accepts. This module's job is everything above that:
splitting an outgoing message into <=MAX_FRAG_PAYLOAD-byte fragments,
retrying each one until its ACK arrives (or giving up), and reassembling
incoming fragments back into whole messages for the application.
"""
import socket
import struct
import threading
import time

HDR_FMT = "!BBBBBBB"  # dest, src, msg_id, frag_idx, frag_tot, type, len
HDR_LEN = struct.calcsize(HDR_FMT)
MAX_FRAG_PAYLOAD = 180  # must match MAX_FRAG_PAYLOAD in the GRC "Frame Trim" block

TYPE_TEXT, TYPE_IMAGE, TYPE_AUDIO, TYPE_ACK = 0, 1, 2, 3
TYPE_NAMES = {TYPE_TEXT: "text", TYPE_IMAGE: "image", TYPE_AUDIO: "audio", TYPE_ACK: "ack"}

BROADCAST = 255
STALE_REASSEMBLY_S = 30.0


class LinkLayer:
    """One instance per node. Owns the UDP sockets that bridge to GNU Radio
    and runs a background thread that receives, ACK-matches, and
    reassembles incoming frames."""

    def __init__(self, my_id, grc_tx_port=52001, grc_rx_port=52002, host="127.0.0.1",
                 ack_timeout=1.5, max_attempts=5):
        self.my_id = my_id
        self.ack_timeout = ack_timeout
        self.max_attempts = max_attempts

        self._tx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._tx_addr = (host, grc_tx_port)

        self._rx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._rx_sock.bind((host, grc_rx_port))

        self._msg_id_counter = 0
        self._msg_id_lock = threading.Lock()

        self._ack_events = {}   # (msg_id, frag_idx) -> threading.Event
        self._ack_lock = threading.Lock()

        self._reassembly = {}   # (src_id, msg_id) -> {"parts": {idx: bytes}, "total": n, "type": t, "ts": float}
        self._reassembly_lock = threading.Lock()

        # Callbacks the application should set:
        self.on_message = None   # fn(src_id: int, msg_type: int, payload: bytes)
        self.on_progress = None  # fn(direction: str, msg_id: int, frag_idx: int, frag_tot: int)

        self._running = True
        self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
        self._rx_thread.start()

    # ------------------------------------------------------------------ #
    # Outbound
    # ------------------------------------------------------------------ #
    def _next_msg_id(self):
        with self._msg_id_lock:
            self._msg_id_counter = (self._msg_id_counter + 1) % 256
            return self._msg_id_counter

    def send(self, dest_id, msg_type, data: bytes, progress_cb=None):
        """Blocking call -- run this on a worker thread, not the UI thread.

        Fragments `data` into <=MAX_FRAG_PAYLOAD-byte chunks and sends each
        one with stop-and-wait ARQ. Returns True if every fragment was
        ACKed, False if the message was abandoned (some fragment exhausted
        `max_attempts`). progress_cb(frag_idx_done, frag_tot), if given, is
        called after each fragment succeeds.
        """
        msg_id = self._next_msg_id()
        frags = [data[i:i + MAX_FRAG_PAYLOAD] for i in range(0, len(data), MAX_FRAG_PAYLOAD)]
        if not frags:
            frags = [b""]
        frag_tot = len(frags)

        for idx, chunk in enumerate(frags):
            ok = self._send_fragment_with_arq(dest_id, msg_id, idx, frag_tot, msg_type, chunk)
            if self.on_progress:
                self.on_progress("tx", msg_id, idx + 1, frag_tot)
            if not ok:
                return False
            if progress_cb:
                progress_cb(idx + 1, frag_tot)
        return True

    def _send_fragment_with_arq(self, dest_id, msg_id, frag_idx, frag_tot, msg_type, chunk):
        key = (msg_id, frag_idx)
        ev = threading.Event()
        with self._ack_lock:
            self._ack_events[key] = ev

        packet = struct.pack(HDR_FMT, dest_id, self.my_id, msg_id, frag_idx,
                              frag_tot, msg_type, len(chunk)) + chunk

        try:
            for _attempt in range(1, self.max_attempts + 1):
                ev.clear()
                self._tx_sock.sendto(packet, self._tx_addr)
                if ev.wait(self.ack_timeout):
                    return True
            return False
        finally:
            with self._ack_lock:
                self._ack_events.pop(key, None)

    # ------------------------------------------------------------------ #
    # Inbound
    # ------------------------------------------------------------------ #
    def _rx_loop(self):
        while self._running:
            try:
                data, _addr = self._rx_sock.recvfrom(4096)
            except OSError:
                break  # socket closed in close()

            if len(data) < HDR_LEN:
                continue

            dest_id, src_id, msg_id, frag_idx, frag_tot, msg_type, length = \
                struct.unpack(HDR_FMT, data[:HDR_LEN])
            payload = data[HDR_LEN:HDR_LEN + length]

            if dest_id not in (self.my_id, BROADCAST):
                continue  # defensive; GRC already filtered this

            if msg_type == TYPE_ACK:
                with self._ack_lock:
                    ev = self._ack_events.get((msg_id, frag_idx))
                if ev is not None:
                    ev.set()
                continue

            self._reassemble(src_id, msg_id, frag_idx, frag_tot, msg_type, payload)

    def _reassemble(self, src_id, msg_id, frag_idx, frag_tot, msg_type, payload):
        key = (src_id, msg_id)
        complete = False
        ordered = None

        with self._reassembly_lock:
            now = time.time()
            for stale_key in [k for k, v in self._reassembly.items()
                               if now - v["ts"] > STALE_REASSEMBLY_S]:
                del self._reassembly[stale_key]

            entry = self._reassembly.setdefault(
                key, {"parts": {}, "total": frag_tot, "type": msg_type, "ts": now})
            entry["parts"][frag_idx] = payload
            entry["ts"] = now
            done = len(entry["parts"]) >= entry["total"]
            n_parts, n_total = len(entry["parts"]), entry["total"]

            if done:
                ordered = b"".join(entry["parts"][i] for i in range(entry["total"]))
                del self._reassembly[key]
                complete = True

        if self.on_progress:
            self.on_progress("rx", msg_id, n_parts, n_total)

        if complete and self.on_message:
            self.on_message(src_id, msg_type, ordered)

    def close(self):
        self._running = False
        for sock in (self._tx_sock, self._rx_sock):
            try:
                sock.close()
            except OSError:
                pass
