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

# ---------- inlined from qpsk_framing.py ----------
import zlib
from collections import deque

import numpy as np

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

# gnuradio.digital.packet_utils.default_access_code (0xACDDA4E2F28C20FC)
ACCESS_CODE = "1010110011011101101001001110001011110010100011000010000011111100"
ACCESS_CODE_BYTES = bytes(
    int(ACCESS_CODE[i:i + 8], 2) for i in range(0, len(ACCESS_CODE), 8)
)

HDR_BYTES = 4          # len(2) + ~len(2)
HDR_BITS = HDR_BYTES * 8
CRC_BYTES = 4
MAX_PAYLOAD = 1024     # link-layer payload ceiling; keep in sync with qpsk_chat
MAX_BODY = MAX_PAYLOAD + CRC_BYTES
MIN_BODY = CRC_BYTES + 1

# Candidate bit offsets tried at a correlator tag before giving up.  Different
# GNU Radio releases place the tag on the last access-code bit or on the first
# bit after it; probing removes the ambiguity instead of hard-coding it.
PROBE_OFFSETS = (0, 1, -1)


# --------------------------------------------------------------------------
# PN9 whitening
# --------------------------------------------------------------------------

def _make_pn(nbytes):
    """PN9 (x^9 + x^5 + 1) byte sequence, MSB-first, seed all-ones."""
    state = 0x1FF
    out = bytearray()
    for _ in range(nbytes):
        b = 0
        for _ in range(8):
            bit = ((state >> 8) ^ (state >> 4)) & 1
            state = ((state << 1) | bit) & 0x1FF
            b = (b << 1) | bit
        out.append(b)
    return bytes(out)


_PN_LEN = 511
PN = _make_pn(_PN_LEN)
_PN_ARR = np.frombuffer(PN, dtype=np.uint8)


def pn_bytes(n, phase=0):
    """n bytes of the PN sequence starting at `phase` (cyclic)."""
    idx = (np.arange(n, dtype=np.int64) + phase) % _PN_LEN
    return _PN_ARR[idx]


def whiten(data, phase=0):
    """XOR `data` (bytes) with the PN sequence.  Self-inverse."""
    a = np.frombuffer(data, dtype=np.uint8)
    return (a ^ pn_bytes(len(a), phase)).tobytes()


# --------------------------------------------------------------------------
# transmit side
# --------------------------------------------------------------------------

def build_frame(payload):
    """bytes -> packed frame bytes ready to hand to the modulator."""
    if not payload:
        raise ValueError("empty payload")
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload %d > MAX_PAYLOAD %d" % (len(payload), MAX_PAYLOAD))
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    body = bytes(payload) + crc.to_bytes(4, "big")
    ln = len(body)
    hdr = ln.to_bytes(2, "big") + (ln ^ 0xFFFF).to_bytes(2, "big")
    return ACCESS_CODE_BYTES + whiten(hdr + body)


def frame_bits(payload):
    """Convenience: frame as an unpacked 0/1 bit array (MSB-first)."""
    return np.unpackbits(np.frombuffer(build_frame(payload), dtype=np.uint8))


# --------------------------------------------------------------------------
# receive side
# --------------------------------------------------------------------------

class FrameAssembler:
    """Turns a tagged bit stream into validated payloads.

    Feed it with add_bits(abs_index, bits) and add_tags(absolute_offsets),
    where the offsets come from a correlate_access_code_tag_bb tag.  Complete,
    CRC-good frames are handed to the `on_frame(payload_bytes)` callback.

    Everything is vectorised (np.packbits over slices); there is no per-bit
    Python loop, so it comfortably keeps up with a 1 Mbit/s bit stream.
    """

    MAX_PENDING_TAGS = 64

    def __init__(self, on_frame, max_body=MAX_BODY):
        self.on_frame = on_frame
        self.max_body = max_body
        self._buf = np.zeros(0, dtype=np.uint8)
        self._buf_start = 0          # absolute bit index of _buf[0]
        self._next_abs = 0           # absolute index of the next expected bit
        self._pending = deque()      # absolute tag offsets, ascending
        self._cur = None             # (abs_header_start, body_len)
        # counters
        self.n_tags = 0
        self.n_bad_header = 0
        self.n_bad_crc = 0
        self.n_frames = 0
        self.n_payload_bytes = 0

    # -- input ------------------------------------------------------------
    def add_tags(self, offsets):
        for off in offsets:
            self._pending.append(int(off))
            self.n_tags += 1
        while len(self._pending) > self.MAX_PENDING_TAGS:
            self._pending.popleft()

    def add_bits(self, abs_index, bits):
        if abs_index != self._next_abs and self._next_abs != 0:
            # stream discontinuity (flowgraph restart): resynchronise
            self._reset_buffer(abs_index)
        elif self._next_abs == 0 and len(self._buf) == 0:
            self._buf_start = abs_index
        self._buf = np.concatenate((self._buf, np.asarray(bits, dtype=np.uint8)))
        self._next_abs = abs_index + len(bits)

    def _reset_buffer(self, abs_index):
        self._buf = np.zeros(0, dtype=np.uint8)
        self._buf_start = abs_index
        self._pending.clear()
        self._cur = None

    # -- processing -------------------------------------------------------
    def process(self):
        end_abs = self._buf_start + len(self._buf)
        while True:
            if self._cur is None:
                if not self._pending:
                    break
                tag = self._pending[0]
                # need HDR_BITS from the latest candidate before we can decide
                if tag + max(PROBE_OFFSETS) + HDR_BITS > end_abs:
                    break
                self._pending.popleft()
                cand = self._try_header(tag, end_abs)
                if cand is None:
                    self.n_bad_header += 1
                    continue
                self._cur = cand
            start, body_len = self._cur
            total_bits = HDR_BITS + body_len * 8
            if start + total_bits > end_abs:
                break
            self._emit(start, body_len, total_bits)
            self._cur = None
            frame_end = start + total_bits
            while self._pending and self._pending[0] < frame_end:
                self._pending.popleft()
        self._trim()

    def _try_header(self, tag, end_abs):
        for off in PROBE_OFFSETS:
            start = tag + off
            if start < self._buf_start or start + HDR_BITS > end_abs:
                continue
            i = start - self._buf_start
            hdr = whiten(np.packbits(self._buf[i:i + HDR_BITS]).tobytes())
            ln = (hdr[0] << 8) | hdr[1]
            comp = (hdr[2] << 8) | hdr[3]
            if (ln ^ 0xFFFF) == comp and MIN_BODY <= ln <= self.max_body:
                return (start, ln)
        return None

    def _emit(self, start, body_len, total_bits):
        i = start - self._buf_start
        raw = np.packbits(self._buf[i:i + total_bits]).tobytes()
        frame = whiten(raw)
        body = frame[HDR_BYTES:]
        payload, crc = body[:-CRC_BYTES], body[-CRC_BYTES:]
        if zlib.crc32(payload) & 0xFFFFFFFF == int.from_bytes(crc, "big"):
            self.n_frames += 1
            self.n_payload_bytes += len(payload)
            self.on_frame(payload)
        else:
            self.n_bad_crc += 1

    def _trim(self):
        end_abs = self._buf_start + len(self._buf)
        if self._cur is not None:
            keep_from = self._cur[0]
        elif self._pending:
            keep_from = self._pending[0] + min(PROBE_OFFSETS)
        else:
            # keep a small tail so a tag landing on the chunk boundary can
            # still be probed backwards
            keep_from = end_abs - 8
        keep_from = max(self._buf_start, min(keep_from, end_abs))
        drop = keep_from - self._buf_start
        if drop > 0:
            self._buf = self._buf[drop:]
            self._buf_start = keep_from

    # -- reporting --------------------------------------------------------
    def stats(self):
        return {
            "tags": self.n_tags,
            "frames": self.n_frames,
            "bad_header": self.n_bad_header,
            "bad_crc": self.n_bad_crc,
            "payload_bytes": self.n_payload_bytes,
        }
# ---------- end of inlined qpsk_framing.py --------


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
