#!/usr/bin/env python3
"""
qpsk_chat.py -- full-duplex chat / file transfer over the differential-QPSK
Pluto link defined in qpsk_phy.py.

Link-layer protocol (inside every PHY frame payload):

    byte 0   : type   1=META 2=DATA 3=EOM 4=ACK 5=NAK
    byte 1-2 : msg_id (uint16, per-sender)
    byte 3-4 : seq    (uint16, chunk index; 0 for META/EOM/ACK)
    byte 5.. : body

    META  body: kind(1) total_bytes(4) nchunks(2) name(utf-8, optional)
                kind: 0=text 1=file 2=ping
    DATA  body: chunk bytes
    EOM   body: crc32 of the whole message (4) nchunks(2)
    ACK   body: -
    NAK   body: list of uint16 missing chunk indices;
                0xFFFF is a sentinel meaning "resend META"

A message is sent as META, every DATA chunk, then EOM.  The receiver answers
EOM with ACK (complete and CRC-good) or NAK (list of what it is missing).  The
sender retransmits only what was NAKed.  Because the link is full duplex the
ACK/NAK path runs while the next message is still going out; ACK/NAK frames are
queued ahead of bulk data so they are never stuck behind a large file.

Usage:
    python3 qpsk_chat.py --role a          # node A: TX 915.0, RX 917.0
    python3 qpsk_chat.py --role b          # node B: TX 917.0, RX 915.0

Commands at the prompt:
    <text>            send a text message
    /send <path>      send a file
    /ping             measure round-trip time
    /stats            PHY + link counters
    /config           show the RF configuration
    /quit
"""

import argparse
import os
import queue
import struct
import sys
import threading
import time
import zlib
from collections import deque

from qpsk_phy import PhyConfig, QpskPhy

# ---- protocol constants ---------------------------------------------------

T_META, T_DATA, T_EOM, T_ACK, T_NAK = 1, 2, 3, 4, 5
K_TEXT, K_FILE, K_PING = 0, 1, 2

HDR = struct.Struct("!BHH")          # type, msg_id, seq
META = struct.Struct("!BIH")         # kind, total_bytes, nchunks
EOM = struct.Struct("!IH")           # crc32, nchunks

CHUNK = 200                          # payload bytes per DATA frame
MAX_ROUNDS = 16
ACK_TIMEOUT = 1.2                    # seconds; covers the modem pipeline delay
NAK_SENTINEL_META = 0xFFFF
MAX_CHUNKS = 0xFFFE
TX_BACKLOG_LIMIT = 64                # frames queued in the PHY before throttling


def _pack(typ, msg_id, seq, body=b""):
    return HDR.pack(typ, msg_id, seq) + body


# ---- sender-side state ----------------------------------------------------

class PendingTx:
    def __init__(self, msg_id, nchunks):
        self.msg_id = msg_id
        self.nchunks = nchunks
        self.event = threading.Event()
        self.acked = False
        self.missing = None      # list[int] or None
        self.need_meta = False


class RxMessage:
    def __init__(self, msg_id, kind, total, nchunks, name):
        self.msg_id = msg_id
        self.kind = kind
        self.total = total
        self.nchunks = nchunks
        self.name = name
        self.chunks = {}
        self.t0 = time.time()

    def missing(self):
        return [i for i in range(self.nchunks) if i not in self.chunks]

    def assemble(self):
        return b"".join(self.chunks[i] for i in range(self.nchunks))


# ---- link layer -----------------------------------------------------------

class ChatLink:
    def __init__(self, phy, out_dir="rx_files", log=print):
        self.phy = phy
        self.out_dir = out_dir
        self.log = log
        os.makedirs(out_dir, exist_ok=True)

        self._next_id = 1
        self._id_lock = threading.Lock()
        self._tx_pending = {}
        self._rx_msgs = {}
        self._done_ids = deque(maxlen=64)
        self._done_set = set()
        self._rx_q = queue.Queue()
        self._stop = threading.Event()

        self.stat_tx_msgs = 0
        self.stat_rx_msgs = 0
        self.stat_retx = 0
        self.stat_naks_rx = 0
        self.stat_naks_tx = 0

        self._rx_thread = threading.Thread(target=self._rx_worker, daemon=True)
        self._rx_thread.start()

    # -- called from the GNU Radio thread; keep it cheap ------------------
    def on_frame(self, payload):
        self._rx_q.put(payload)

    def stop(self):
        self._stop.set()

    # -- transmit ---------------------------------------------------------
    def _alloc_id(self):
        with self._id_lock:
            mid = self._next_id
            self._next_id = (self._next_id % 0xFFFE) + 1
            return mid

    def _tx(self, frame, urgent=False):
        if not urgent:
            while (self.phy.tx_backlog() > TX_BACKLOG_LIMIT
                   and not self._stop.is_set()):
                time.sleep(0.01)
        self.phy.send_frame(frame, urgent=urgent)

    def send_message(self, kind, data, name=""):
        """Blocking; returns (ok, elapsed_seconds)."""
        if len(data) > CHUNK * MAX_CHUNKS:
            raise ValueError("message too large for a uint16 chunk index")
        chunks = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)] or [b""]
        nchunks = len(chunks)
        msg_id = self._alloc_id()
        st = PendingTx(msg_id, nchunks)
        self._tx_pending[msg_id] = st

        name_b = name.encode("utf-8")[:120]
        meta = _pack(T_META, msg_id, 0,
                     META.pack(kind, len(data), nchunks) + name_b)
        eom = _pack(T_EOM, msg_id, 0,
                    EOM.pack(zlib.crc32(data) & 0xFFFFFFFF, nchunks))

        t0 = time.time()
        timeouts = 0
        try:
            for rnd in range(MAX_ROUNDS):
                if rnd == 0:
                    todo = list(range(nchunks))
                elif st.missing is not None:
                    todo = st.missing
                elif timeouts < 2:
                    todo = []            # only re-poke with an EOM
                else:
                    todo = list(range(nchunks))
                # arm before transmitting, otherwise an ACK that arrives while
                # the EOM is still being queued would be cleared away
                st.missing = None
                st.event.clear()
                if rnd == 0 or st.need_meta:
                    self._tx(meta)
                    st.need_meta = False
                for seq in todo:
                    self._tx(_pack(T_DATA, msg_id, seq, chunks[seq]))
                    if rnd > 0:
                        self.stat_retx += 1
                self._tx(eom)

                wait = ACK_TIMEOUT + 0.01 * max(len(todo), 1)
                if st.event.wait(wait):
                    timeouts = 0
                    if st.acked:
                        self.stat_tx_msgs += 1
                        return True, time.time() - t0
                else:
                    timeouts += 1
            return False, time.time() - t0
        finally:
            self._tx_pending.pop(msg_id, None)

    def send_text(self, text):
        return self.send_message(K_TEXT, text.encode("utf-8"))

    def send_file(self, path):
        with open(path, "rb") as f:
            data = f.read()
        return self.send_message(K_FILE, data, os.path.basename(path))

    def ping(self):
        return self.send_message(K_PING, b"ping")

    # -- receive ----------------------------------------------------------
    def _rx_worker(self):
        while not self._stop.is_set():
            try:
                payload = self._rx_q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._handle(payload)
            except Exception as exc:                      # never kill the thread
                self.log("[link] malformed frame: %r" % (exc,))

    def _handle(self, payload):
        if len(payload) < HDR.size:
            return
        typ, msg_id, seq = HDR.unpack(payload[:HDR.size])
        body = payload[HDR.size:]

        if typ == T_ACK:
            st = self._tx_pending.get(msg_id)
            if st:
                st.acked = True
                st.event.set()
            return

        if typ == T_NAK:
            st = self._tx_pending.get(msg_id)
            if st:
                seqs = [struct.unpack_from("!H", body, i)[0]
                        for i in range(0, len(body) - 1, 2)]
                if NAK_SENTINEL_META in seqs:
                    st.need_meta = True
                    seqs = [s for s in seqs if s != NAK_SENTINEL_META]
                st.missing = [s for s in seqs if s < st.nchunks]
                self.stat_naks_rx += 1
                st.event.set()
            return

        if typ == T_META:
            if msg_id in self._done_set:
                return
            if len(body) < META.size:
                return
            kind, total, nchunks = META.unpack(body[:META.size])
            name = body[META.size:].decode("utf-8", "replace")
            old = self._rx_msgs.get(msg_id)
            msg = RxMessage(msg_id, kind, total, nchunks, name)
            if old is not None:                # keep chunks already received
                msg.chunks = {k: v for k, v in old.chunks.items() if k < nchunks}
            self._rx_msgs[msg_id] = msg
            return

        if typ == T_DATA:
            msg = self._rx_msgs.get(msg_id)
            if msg is None:
                # META lost: buffer the chunk under a placeholder
                msg = RxMessage(msg_id, K_TEXT, 0, 0, "")
                self._rx_msgs[msg_id] = msg
            msg.chunks[seq] = body
            return

        if typ == T_EOM:
            if msg_id in self._done_set:
                self._tx(_pack(T_ACK, msg_id, 0), urgent=True)
                return
            if len(body) < EOM.size:
                return
            crc, nchunks = EOM.unpack(body[:EOM.size])
            msg = self._rx_msgs.get(msg_id)
            if msg is None or msg.nchunks == 0:
                # never saw META
                self._tx(_pack(T_NAK, msg_id, 0,
                               struct.pack("!H", NAK_SENTINEL_META)),
                         urgent=True)
                return
            msg.nchunks = nchunks
            missing = msg.missing()
            if missing:
                self._send_nak(msg_id, missing)
                return
            data = msg.assemble()
            if (zlib.crc32(data) & 0xFFFFFFFF) != crc:
                # whole-message CRC failed although every chunk passed its own
                # frame CRC -- ask for everything again
                self._send_nak(msg_id, list(range(nchunks)))
                return
            self._complete(msg, data)

    def _send_nak(self, msg_id, missing, extra_meta=False):
        self.stat_naks_tx += 1
        per_frame = (CHUNK - 2) // 2
        head = [NAK_SENTINEL_META] if extra_meta else []
        for i in range(0, len(missing), per_frame):
            seqs = head + list(missing[i:i + per_frame])
            head = []
            body = b"".join(struct.pack("!H", s) for s in seqs)
            self._tx(_pack(T_NAK, msg_id, 0, body), urgent=True)

    def _complete(self, msg, data):
        self._tx(_pack(T_ACK, msg.msg_id, 0), urgent=True)
        self._rx_msgs.pop(msg.msg_id, None)
        self._done_ids.append(msg.msg_id)      # bounded deque
        self._done_set = set(self._done_ids)
        self.stat_rx_msgs += 1

        dt = max(time.time() - msg.t0, 1e-6)
        if msg.kind == K_PING:
            return
        if msg.kind == K_TEXT:
            self.log("\n[peer] %s" % data.decode("utf-8", "replace"))
        else:
            path = self._unique_path(msg.name or "received.bin")
            with open(path, "wb") as f:
                f.write(data)
            self.log("\n[peer] file %s  %d B  (%.1f s, %.1f kB/s) -> %s"
                     % (msg.name, len(data), dt, len(data) / dt / 1e3, path))

    def _unique_path(self, name):
        name = os.path.basename(name).replace("/", "_") or "received.bin"
        path = os.path.join(self.out_dir, name)
        stem, ext = os.path.splitext(path)
        n = 1
        while os.path.exists(path):
            path = "%s_%d%s" % (stem, n, ext)
            n += 1
        return path

    def stats(self):
        s = self.phy.phy_stats()
        s.update({
            "link_tx_msgs": self.stat_tx_msgs,
            "link_rx_msgs": self.stat_rx_msgs,
            "retransmitted_chunks": self.stat_retx,
            "naks_received": self.stat_naks_rx,
            "naks_sent": self.stat_naks_tx,
            "tx_backlog": self.phy.tx_backlog(),
        })
        return s


# ---- CLI ------------------------------------------------------------------

ROLES = {
    # role: (tx_freq, rx_freq)
    "a": (915.0e6, 917.0e6),
    "b": (917.0e6, 915.0e6),
}


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--role", choices=sorted(ROLES), default="a",
                   help="FDD role: 'a' TX 915/RX 917, 'b' TX 917/RX 915")
    p.add_argument("--uri", default="ip:192.168.1.10")
    p.add_argument("--tx-freq", type=float, help="override TX centre frequency (Hz)")
    p.add_argument("--rx-freq", type=float, help="override RX centre frequency (Hz)")
    p.add_argument("--samp-rate", type=float, default=2e6)
    p.add_argument("--sps", type=int, default=4)
    p.add_argument("--bandwidth", type=float, default=1.5e6,
                   help="AD9361 analog filter bandwidth (Hz)")
    p.add_argument("--tx-atten", type=float, default=10.0,
                   help="TX attenuation in dB (0 = full power)")
    p.add_argument("--tx-amplitude", type=float, default=0.6)
    p.add_argument("--rx-gain-mode", default="slow_attack",
                   choices=["slow_attack", "fast_attack", "manual", "hybrid"])
    p.add_argument("--rx-gain", type=float, default=40.0)
    p.add_argument("--buffer-size", type=int, default=16384)
    p.add_argument("--out-dir", default="rx_files")
    return p.parse_args(argv)


def build_config(args):
    tx, rx = ROLES[args.role]
    return PhyConfig(
        uri=args.uri,
        tx_freq=args.tx_freq if args.tx_freq else tx,
        rx_freq=args.rx_freq if args.rx_freq else rx,
        samp_rate=args.samp_rate,
        sps=args.sps,
        bandwidth=args.bandwidth,
        tx_atten=args.tx_atten,
        tx_amplitude=args.tx_amplitude,
        rx_gain_mode=args.rx_gain_mode,
        rx_gain=args.rx_gain,
        buffer_size=args.buffer_size,
    )


def main(argv=None):
    args = parse_args(argv)
    cfg = build_config(args)

    print("node %s\n%s" % (args.role.upper(), cfg.describe()))

    link_holder = {}

    def on_frame(payload):
        link_holder["link"].on_frame(payload)

    phy = QpskPhy(cfg, on_frame)
    link = ChatLink(phy, out_dir=args.out_dir)
    link_holder["link"] = link

    phy.start()
    print("radio running.  type a message, /send <path>, /ping, /stats, /quit")

    try:
        while True:
            try:
                line = input("> ")
            except EOFError:
                break
            line = line.strip()
            if not line:
                continue
            if line in ("/quit", "/exit"):
                break
            if line == "/stats":
                for k, v in sorted(link.stats().items()):
                    print("  %-22s %s" % (k, v))
                continue
            if line == "/config":
                print(cfg.describe())
                continue
            if line == "/ping":
                ok, dt = link.ping()
                print("  %s  rtt=%.0f ms" % ("pong" if ok else "no reply",
                                             dt * 1e3))
                continue
            if line.startswith("/send "):
                path = os.path.expanduser(line[6:].strip())
                if not os.path.isfile(path):
                    print("  no such file: %s" % path)
                    continue
                size = os.path.getsize(path)
                print("  sending %s (%d B, %d chunks)..."
                      % (path, size, max(1, -(-size // CHUNK))))
                ok, dt = link.send_file(path)
                print("  %s in %.1f s (%.1f kB/s)"
                      % ("delivered" if ok else "FAILED after retries",
                         dt, size / max(dt, 1e-6) / 1e3))
                continue
            if line.startswith("/"):
                print("  unknown command")
                continue
            ok, dt = link.send_text(line)
            if not ok:
                print("  [not acknowledged after %d rounds]" % MAX_ROUNDS)
    finally:
        link.stop()
        phy.stop()
        phy.wait()
        print("stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
