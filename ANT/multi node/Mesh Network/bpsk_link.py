"""
Link layer for the BPSK full-duplex PlutoSDR radio (GNU Radio 3.10).

Pure Python - deliberately free of any GNU Radio / PMT import so that the
protocol logic can be exercised from a REPL or a unit test without a
flowgraph.  The GRC Embedded Python Block is a thin shim that converts
PMT <-> bytes and drives LinkState.

Wire format of one link frame (the PHY carries this as its payload, i.e.
access code + 16-bit length header are prepended by protocol_formatter_bb):

    byte  0        : scrambler offset, sent in the clear
    byte  1        : version (4b) | frame type (4b)
    byte  2        : destination address
    byte  3        : source address
    byte  4        : sequence number of this frame
    byte  5        : sequence number being acknowledged (ACK frames)
    byte  6        : flags
    bytes 7-8      : payload length, uint16 big-endian
    bytes 9..      : payload
    last 4 bytes   : CRC-32 (zlib) over the header and payload, big-endian

Everything after byte 0 is XORed with a fixed LFSR keystream rotated by the
offset in byte 0, so the transmitted waveform does not depend on the payload.

Reliability is stop-and-wait ARQ: one outstanding data frame at a time,
retransmitted on timeout up to max_retries, with 8-bit sequence numbers and
duplicate suppression at the receiver.

---------------------------------------------------------------------------
r5 additions, for the application layer (bpsk_app.py)
---------------------------------------------------------------------------
The link layer stays exactly as reliable as before; what is new is that it
now *reports* what it is doing instead of only printing it:

  * Every entry point may return ('evt', dict) actions alongside the existing
    ('tx', bytes) and ('log', str).  The dicts are structured facts - frame
    sent, frame acknowledged, fragment k of n arrived - which is what a UI
    needs.  Nothing above has to scrape log strings.
  * Outbound work can be tagged with an opaque message id (mid).  The id is
    never transmitted; it is carried locally so that an ACK can be attributed
    to the chat bubble that caused it.
  * FILE_START carries an opaque application metadata blob (MIME type, sender
    nickname, message id, timestamp).  The link layer never looks inside it.
  * send_data() / send_file() accept bytes directly, so the application layer
    does not have to go through the operator command parser.

Frames from an r4 node still decode: the FILE_START parser falls back to the
old "everything after byte 6 is the filename" layout when the v2 marker is
absent, and a text frame with no application envelope is delivered as plain
text by bpsk_app.

---------------------------------------------------------------------------
r6.1
---------------------------------------------------------------------------
  * No link frame is longer than MAX_FRAME (600) bytes; build() refuses one.
    The receiver's deframer (bpsk_phy) rejects any header announcing more,
    so a corrupted length can no longer stall a receive channel.
  * The carrier is kept up by the flowgraph's transmit framer (PRBS filler),
    not by IDLE frames: LinkMux(filler='keepalive') sends one IDLE frame only
    after KEEPALIVE seconds with nothing else on air - static mode's presence
    signal - instead of ~200 a second that every receiver had to decode.
    filler='frames' keeps the r6.0 behaviour (the simulator's r6.0 mode).
  * Radio.observe(): the framer reports its real backlog and the airtime
    model is corrected from it, so the model can no longer run ahead of the
    radio (r6.0.1: frames piled up in front of the modulator until GNU Radio
    discarded them).
"""

import os
import random
import struct
import zlib
from collections import OrderedDict, deque

VERSION = 1
HDR_LEN = 8
CRC_LEN = 4
SCRAM_LEN = 1        # clear-text scrambler offset byte
BROADCAST = 0xFF
MAX_PAYLOAD = 512          # r6.1: was 1024; every frame must fit MAX_FRAME
MAX_FRAME = 600            # bytes on air per link frame, all included (r6.1)

FT_IDLE = 0x0
FT_DATA = 0x1
FT_ACK = 0x2
FT_FILE_START = 0x3
FT_FILE_DATA = 0x4
FT_FILE_END = 0x5
FT_FILE_NACK = 0x6         # receiver -> sender: these fragments never arrived
FT_FILE_DONE = 0x7         # receiver -> sender: file complete, here is the verdict
# 0x8-0xA belong to bpsk_secure and never reach LinkState: they are unwrapped
# (or consumed, for the handshake) before on_frame() is called.
FT_SEALED = 0x8            # any link frame, encrypted and authenticated
FT_HS_INIT = 0x9           # Noise KK handshake, message 1
FT_HS_RESP = 0xA           # Noise KK handshake, message 2
FT_BEACON = 0xB            # r6: broadcast presence (bpsk_mac), never sealed

FT_NAME = {FT_IDLE: 'IDLE', FT_DATA: 'DATA', FT_ACK: 'ACK',
           FT_FILE_START: 'FILE_START', FT_FILE_DATA: 'FILE_DATA',
           FT_FILE_END: 'FILE_END', FT_FILE_NACK: 'FILE_NACK',
           FT_FILE_DONE: 'FILE_DONE', FT_SEALED: 'SEALED',
           FT_HS_INIT: 'HS_INIT', FT_HS_RESP: 'HS_RESP', FT_BEACON: 'BEACON'}

# Bumped whenever the wire format or the file protocol changes. Printed in the
# banner so both consoles show which build they are running - a mismatched pair
# is otherwise indistinguishable from a bad radio link.
REVISION = 'r6.1-mesh'
# r6.1: same link frames, but a different channel plan (6 MS/s, one-sided)
# and continuous PRBS filler, so r6.1 and r6.0.x nodes do not hear each other.
WIRE_COMPAT = 'r6.1'    # bump only when the on-air format changes
# With security on (the default) every frame but the IDLE filler is sealed and
# only another r5.3 node holding a roster entry for us can talk to us. With
# security off, r5.3 is on-air identical to r5 / r5.1 / r5.2.
WIRE_COMPAT_SECURE = 'r6.1-sec'

MAX_NACK_INDICES = 120     # fragment indices that fit in one NACK frame

FLAG_RETRY = 0x01
FLAG_SYNC = 0x02           # unicast IDLE carrying the sender's window base
EPOCH_SHIFT = 2            # flags bits 2-7: sender epoch, random per start-up

# --- Selective Repeat (r5.4) ------------------------------------------------
# Sequence numbers stay 8-bit. A receiver treats (seq - base) mod 256 < HALF
# as new and the rest as old, which is correct for any send window up to
# HALF; MAX_WINDOW leaves the other half for ACK lag and base resync.
SEQ_MOD = 256
HALF = 128
MAX_WINDOW = 64
SACK_BITS = 127            # SACK bitmap: bit i = seq base+1+i received

# Marks a FILE_START payload as carrying the r5 layout (length-prefixed name
# followed by an application metadata blob) rather than the r4 layout, where
# everything past the fixed header was the filename. 0xA5 is not a valid first
# byte of a UTF-8 filename, so an r4 receiver could never produce it either.
FILE_META_MARK = 0xA5
MAX_FILE_META = 512

_HDR = struct.Struct('!BBBBBBH')

# The transmit filler is an open-loop model of how much airtime has been handed
# to the modulator. Any underestimate accumulates: the link hands out more
# airtime per second than the radio can emit, the PDU queue ahead of the Pluto
# sink grows without bound, and eventually every frame times out before it is
# even transmitted. Bias the model to under-produce - a small duty-cycle gap is
# harmless, an unbounded queue is not.
AIRTIME_MARGIN = 1.05
FILLER_BURST = 8        # most idle frames emitted in one tick
KEEPALIVE = 0.5         # filler='keepalive': an IDLE after this long silent
MAX_RX_LATENCY = 10.0   # r6.1: cap on the measured latency ACK timers allow for
PEER_TIMEOUT = 3.0      # a peer silent this long (+ 2 x latency) is unreachable

# Longest a frame in flight is held, without spending retries, while there is
# no secure session to send it on (see on_tick).
HOLD_LIMIT = 15.0

# Largest link payload build() accepts. MAX_PAYLOAD is what the link layer
# puts in a frame; sealing (bpsk_secure) adds 35 bytes around it.
MAX_FRAME_PAYLOAD = MAX_PAYLOAD + 64


def _build_keystream(length=1024, seed=0xACE1):
    """Fixed pseudo-random byte sequence from a 16-bit Galois LFSR.

    Identical on both nodes because it is computed from this code, so no
    keystream has to be exchanged.
    """
    out = bytearray(length)
    reg = seed
    for i in range(length):
        byte = 0
        for _ in range(8):
            bit = reg & 1
            reg >>= 1
            if bit:
                reg ^= 0xB400
            byte = (byte << 1) | bit
        out[i] = byte
    return bytes(out)


KEYSTREAM = _build_keystream()
KLEN = len(KEYSTREAM)
_KS2 = KEYSTREAM * 2


def _scramble(data, offset):
    """XOR with the keystream. Its own inverse, so TX and RX share it.

    Without this the transmitted waveform is a direct function of the payload.
    Differential BPSK turns a run of zero bits into a run of symbols with no
    phase transition: no timing information for the TED, and a pure tone for
    the band-edge FLL and the AD9363 DC-offset tracking to fight over. Real
    files - JPEG especially - contain such runs, and because a retransmission
    resends the identical bit pattern, ARQ cannot recover from one.
    """
    n = len(data)
    if not n:
        return b''
    offset %= KLEN
    if n <= KLEN:
        ks = _KS2[offset:offset + n]
    else:
        ks = (KEYSTREAM * (n // KLEN + 2))[offset:offset + n]
    # One big-integer XOR instead of a Python loop per byte (r6.1: every
    # received frame passes through here on the GNU Radio thread).
    return (int.from_bytes(data, 'big') ^ int.from_bytes(ks, 'big')).to_bytes(
        n, 'big')

HELP = (
    "commands:\n"
    "  <text>              send a text message to the peer\n"
    "  /sendfile <path>    segment and send a file\n"
    "  /stats              print link counters\n"
    "  /ping               send a one-word probe\n"
    "  /secure             encryption status, key fingerprints\n"
    "  /help               this list"
)


class BadFrame(Exception):
    """Raised by parse() when a received buffer is not a valid link frame."""


def build(ftype, dst, src, seq, ack, payload=b'', flags=0, offset=0):
    """Serialise one link frame: clear offset byte, then scrambled body+CRC."""
    if len(payload) > MAX_FRAME_PAYLOAD:
        raise ValueError('payload too long: %d' % len(payload))
    body = _HDR.pack(((VERSION & 0x0F) << 4) | (ftype & 0x0F),
                     dst & 0xFF, src & 0xFF, seq & 0xFF, ack & 0xFF,
                     flags & 0xFF, len(payload)) + payload
    body += struct.pack('!I', zlib.crc32(body) & 0xFFFFFFFF)
    if 1 + len(body) > MAX_FRAME:
        raise ValueError('frame too long: %d bytes (max %d)'
                         % (1 + len(body), MAX_FRAME))
    offset &= 0xFF
    return bytes([offset]) + _scramble(body, offset)


def parse(raw):
    """Validate and decode one link frame. Raises BadFrame on any defect."""
    raw = bytes(raw)
    if len(raw) < 1 + HDR_LEN + CRC_LEN:
        raise BadFrame('runt frame, %d bytes' % len(raw))
    raw = _scramble(raw[1:], raw[0])
    body, crc = raw[:-CRC_LEN], raw[-CRC_LEN:]
    if struct.unpack('!I', crc)[0] != (zlib.crc32(body) & 0xFFFFFFFF):
        raise BadFrame('crc mismatch')
    v_t, dst, src, seq, ack, flags, plen = _HDR.unpack(body[:HDR_LEN])
    if (v_t >> 4) != VERSION:
        raise BadFrame('unknown version %d' % (v_t >> 4))
    payload = body[HDR_LEN:]
    if len(payload) != plen:
        raise BadFrame('length field %d, got %d' % (plen, len(payload)))
    return {'type': v_t & 0x0F, 'dst': dst, 'src': src, 'seq': seq,
            'ack': ack, 'flags': flags, 'payload': payload}


def segment(data, frag_size):
    """Split a blob into fragments and compute its CRC.

    Exposed separately from send_file() so the caller can do this work on its
    own thread. Done inline on the GNU Radio thread it blocks every message
    handler, which stops the transmit filler and starves the Pluto TX buffer:
    measured at 43 ms for an 8 MiB file against a 30 ms queue_ahead cushion.
    """
    step = max(1, int(frag_size) - 2)          # 2 bytes of fragment index
    frags = [data[i:i + step] for i in range(0, len(data), step)] or [b'']
    return frags, zlib.crc32(data) & 0xFFFFFFFF


def build_file_items(size, total, name, meta, crc, frags, mid):
    """Build the whole transmit-queue item list for one file transfer.

    Module-level and pure, so the caller can do it on its own thread. Building
    33 000 of these inline on the GNU Radio thread costs ~15 ms, which eats
    half the queue_ahead cushion on its own.
    """
    items = [{'ftype': FT_FILE_START,
              'payload': pack_file_start(size, total, name, meta),
              'label': 'file-start', 'mid': mid, 'frag': None}]
    items.extend({'ftype': FT_FILE_DATA,
                  'payload': struct.pack('!H', i) + frag,
                  'label': 'file-frag %d' % i, 'mid': mid, 'frag': i}
                 for i, frag in enumerate(frags))
    items.append({'ftype': FT_FILE_END, 'payload': struct.pack('!I', crc),
                  'label': 'file-end', 'mid': mid, 'frag': None})
    return items


def pack_file_start(size, total, name, meta=b''):
    """Build an r5 FILE_START payload: fixed header, name, metadata blob."""
    name = name.encode('utf-8', 'replace') if isinstance(name, str) else name
    name = name[:200]
    meta = meta or b''
    if len(meta) > MAX_FILE_META:
        raise ValueError('file metadata too long: %d' % len(meta))
    return (struct.pack('!IH', size, total)
            + bytes([FILE_META_MARK, len(name)]) + name
            + struct.pack('!H', len(meta)) + meta)


_BAD_NAME_CHARS = set('<>:"|?*\\/\x7f')
MAX_NAME_BYTES = 180


def clean_name(name):
    """Make a peer-supplied filename safe to use as a path component.

    The name arrives off the air, so it is untrusted: a NUL makes open() raise
    ValueError (which, escaping a GNU Radio message handler with
    catch_exceptions=False, aborts the process), a CR/LF would end up in an
    HTTP header, and a separator would escape rx_dir. Letters of any script are
    kept - only separators, control characters and the characters Windows
    rejects are removed. The extension survives truncation.
    """
    if isinstance(name, bytes):
        name = name.decode('utf-8', 'replace')
    name = str(name).replace('\\', '/').rsplit('/', 1)[-1]
    name = ''.join(c for c in name
                   if c not in _BAD_NAME_CHARS and ord(c) >= 0x20
                   and not 0x80 <= ord(c) < 0xA0)
    name = name.strip().lstrip('.').strip()
    stem, dot, ext = name.rpartition('.')
    if not dot or len(ext.encode('utf-8')) > 16:
        stem, ext = name, ''
    while stem and len((stem + dot + ext).encode('utf-8')) > MAX_NAME_BYTES:
        stem = stem[:-1]
    name = (stem + dot + ext) if stem else ext
    return name or 'unnamed'


def unique_path(directory, filename):
    """directory/filename, or directory/stem-2.ext, -3 ... if that exists.

    Received files used to be written to rx_<name> unconditionally, so two
    attachments with the same name - every pasted screenshot is 'image.png' -
    overwrote each other and the first chat entry silently showed the second
    picture.
    """
    path = os.path.join(directory, filename)
    if not os.path.exists(path):
        return path
    stem, dot, ext = filename.rpartition('.')
    if not dot:
        stem, ext = filename, ''
    for n in range(2, 100000):
        path = os.path.join(directory, '%s-%d%s%s' % (stem, n, dot, ext))
        if not os.path.exists(path):
            return path
    return os.path.join(directory, '%s-%s%s%s' % (stem, os.urandom(4).hex(),
                                                   dot, ext))


def unpack_file_start(payload):
    """Decode a FILE_START payload, r5 layout or the r4 one.

    Returns (size, total, name, meta) or None if the payload is malformed.
    """
    if len(payload) < 6:
        return None
    size, total = struct.unpack('!IH', payload[:6])
    rest = payload[6:]
    if not rest or rest[0] != FILE_META_MARK:
        # r4 node: the remainder is the filename and there is no metadata.
        return size, total, clean_name(rest), b''
    if len(rest) < 2:
        return None
    nlen = rest[1]
    name = clean_name(rest[2:2 + nlen])
    tail = rest[2 + nlen:]
    meta = b''
    if len(tail) >= 2:
        mlen = struct.unpack('!H', tail[:2])[0]
        meta = tail[2:2 + mlen]
    return size, total, name, meta


class Radio(object):
    """The node's single transmit channel: one airtime model for all peers.

    r6 runs one LinkState (ARQ engine) per peer, but they all share this
    node's one carrier, so busy_until - the modelled instant the transmit
    queue drains - must be shared too. `enabled` is the channel manager's
    switch (bpsk_mac): while listening or during a silence check nothing at
    all may be transmitted, not even an ACK.
    """

    def __init__(self, sym_rate=250000.0, overhead=452):
        self.sym_rate = float(sym_rate)
        self.overhead = int(overhead)
        self.busy_until = 0.0
        self.seal_overhead = 0
        self.enabled = True
        self.suppressed = 0
        self.last_tx = -1e9         # last frame handed out (keepalive)
        self.observed = 0           # framer reports applied (r6.1)
        self.model_error = 0.0      # last (model - real) busy_until, seconds

    def airtime(self, frame_len):
        """Seconds of RF this frame occupies, preamble/postamble included."""
        return AIRTIME_MARGIN * (self.overhead + frame_len) * 8.0 / self.sym_rate

    def send(self, out, frame, now, sealed=True):
        if not self.enabled:
            self.suppressed += 1
            return 0
        n = len(frame) + (self.seal_overhead if sealed else 0)
        self.busy_until = max(self.busy_until, now) + self.airtime(n)
        self.last_tx = now
        out.append(('tx', frame))
        return n

    def observe(self, now, real_busy):
        """The transmit framer's report, turned into the instant the frames
        handed out so far will have left (computed by the flowgraph shim from
        the framer's backlog). The model then follows the radio instead of
        its own estimate: an underestimate can no longer pile frames up in
        front of the modulator, an overestimate no longer wastes airtime."""
        self.model_error = self.busy_until - real_busy
        self.busy_until = max(now, real_busy)
        self.observed += 1


class LinkState(object):
    """Addressing + Selective Repeat ARQ + file segmentation, for ONE peer.

    Every entry point takes the current time and returns a list of actions:
        ('tx', bytes)  -- hand this frame to the modulator
        ('log', str)   -- show this line to the operator
        ('evt', dict)  -- a structured protocol event, for the application layer
    """

    def __init__(self, my_addr=1, peer_addr=2, ack_timeout=0.5, max_retries=5,
                 frag_size=256, queue_ahead=0.03, sym_rate=250000.0,
                 overhead=452, rx_dir='.', window=32, ack_gap=0.03,
                 radio=None, filler=True, epoch=None):
        self.my_addr = int(my_addr) & 0xFF
        self.peer_addr = int(peer_addr) & 0xFF
        self.ack_timeout = float(ack_timeout)
        self.max_retries = int(max_retries)
        self.frag_size = max(16, min(int(frag_size), MAX_PAYLOAD - 8))
        self.queue_ahead = float(queue_ahead)
        self.sym_rate = float(sym_rate)
        self.overhead = int(overhead)
        self.rx_dir = rx_dir
        # r6: the carrier may be shared with other peers' engines (LinkMux);
        # only a standalone engine sends the idle filler itself.
        self.radio = radio if radio is not None else Radio(sym_rate, overhead)
        self.filler = bool(filler)

        # Two transmit queues. txq holds the bulk of our own file transfer;
        # txq_hi holds everything short and time-critical - FILE_NACK and
        # FILE_DONE replies to the PEER's transfer (at the very front) and chat
        # frames. With one FIFO, a reply to the peer waited behind every
        # fragment of our own file, so on a full-duplex link carrying a file in
        # each direction the peer gave up after 8 x 3 s and reported a file it
        # had delivered as failed; a chat line typed during an upload waited
        # for the whole upload (measured: 114 s behind a 200 kB file).
        self.txq = deque()
        self.txq_hi = deque()

        # Selective Repeat state. outq: frames sent and not yet acknowledged,
        # oldest first, seq -> entry. Its first key is the window base, which
        # every data frame carries in its header 'ack' byte so the receiver can
        # skip a frame the sender has given up on.
        self.window = max(1, min(int(window), MAX_WINDOW))
        self.ack_gap = float(ack_gap)
        # r6.1: measured receive-pipeline latency (the node's own echo). An
        # ACK crosses two pipelines (the peer's and ours), so it is added
        # twice to every ACK deadline - with a 4.6 s echo (a slow host,
        # r6.0.1) a fixed 0.5 s timeout retransmitted every frame 5 times
        # and then dropped it.
        self.rx_latency = 0.0
        self.outq = OrderedDict()
        self.tx_seq = 0
        self._txno = 0              # transmission counter, for fast retransmit
        self.epoch = int(epoch) if epoch else random.randint(1, 63)
        self._sync_left = 0         # base-sync frames still to send
        self._sync_next = 0.0
        # Receiver state per source: base (next seq to deliver), buf (frames
        # held until the gap before them fills), epoch (sender's start-up id).
        self.rx = {}
        self._ack_due = {}
        self._ack_last = {}
        self.peer_seen = 0.0
        self.peer_up = False
        self.rx_file = None
        self.rx_last = None      # verdict of the last completed inbound file
        # Set by the application when bpsk_secure is in use: tx_gate() says
        # whether a session exists (nothing is pumped until it does) and
        # seal_overhead is the bytes sealing adds to every non-filler frame.
        self.tx_gate = None
        self.tx_file = None
        self.max_file_rounds = 8
        self._idle_off = 0

        self.stats = dict(tx_frames=0, tx_bytes=0, retx=0, dropped=0,
                          rx_valid=0, rx_bad=0, rx_notme=0, rx_dup=0,
                          ack_tx=0, ack_rx=0, idle_tx=0, idle_rx=0,
                          frag_lost=0, file_rounds=0,
                          rx_payload_bytes=0, tx_payload_bytes=0,
                          fast_retx=0, rx_skipped=0, rx_resync=0, rx_legacy=0)

    # ------------------------------------------------------------------ util
    def banner(self):
        return ('link up [%s]: my_addr=%d peer_addr=%d  frag=%dB  '
                'ack_timeout=%.2fs retries=%d  ARQ=%s\n%s'
                % (REVISION, self.my_addr, self.peer_addr, self.frag_size,
                   self.ack_timeout, self.max_retries,
                   'selective-repeat window %d' % self.window
                   if self.window > 1 else 'stop-and-wait (window 1)', HELP))

    @property
    def busy_until(self):
        return self.radio.busy_until

    @busy_until.setter
    def busy_until(self, value):
        self.radio.busy_until = value

    @property
    def seal_overhead(self):
        return self.radio.seal_overhead

    @seal_overhead.setter
    def seal_overhead(self, value):
        self.radio.seal_overhead = value

    def _airtime(self, frame_len):
        """Seconds of RF this frame occupies, preamble/postamble included."""
        return self.radio.airtime(frame_len)

    def note_tx(self, frame_len, now):
        """Account for a frame produced outside this state machine.

        Handshake frames come from bpsk_secure. They occupy airtime like any
        other; leaving them out of busy_until is exactly the kind of airtime
        underestimate AIRTIME_MARGIN exists to prevent.
        """
        self.busy_until = max(self.busy_until, now) + self._airtime(frame_len)
        self.stats['tx_frames'] += 1
        self.stats['tx_bytes'] += frame_len

    def reset_peer(self, src):
        """The peer restarted: forget per-peer receive state from its old life.

        Its sequence numbers begin again at 0. If our last-seen sequence from
        it happened to be 0, its first new frame would be ACKed and discarded
        as a duplicate (latent since r4).
        """
        out = []
        self.rx.pop(src, None)
        self._ack_due.pop(src, None)
        rf = self.rx_file
        if rf is not None and rf['src'] == src:
            self.rx_file = None
            self._evt(out, e='rx_file_abandoned', src=src, name=rf['name'],
                      got=len(rf['frags']), total=rf['total'])
        return out

    def _ready(self, now):
        return self.radio.enabled and (self.tx_gate is None
                                       or self.tx_gate(now))

    def kick(self, now):
        """Pump the queue now - e.g. the moment a secure session comes up."""
        out = []
        self._pump(out, now)
        return out

    @staticmethod
    def _evt(out, **kw):
        out.append(('evt', kw))

    def _tx(self, out, frame, now, sealed=True):
        n = self.radio.send(out, frame, now, sealed)
        if n:
            self.stats['tx_frames'] += 1
            self.stats['tx_bytes'] += n

    def _enqueue(self, ftype, payload, label, mid=None, frag=None,
                 prio='bulk'):
        """prio: 'ctl' = front of txq_hi, 'msg' = back of txq_hi, 'bulk' = txq."""
        item = {'ftype': ftype, 'payload': payload, 'label': label,
                'mid': mid, 'frag': frag}
        if prio == 'ctl':
            self.txq_hi.appendleft(item)
        elif prio == 'msg':
            self.txq_hi.append(item)
        else:
            self.txq.append(item)

    def queued(self):
        return len(self.txq_hi) + len(self.txq)

    def send_base(self):
        """Oldest unacknowledged sequence number (next_seq if none)."""
        return next(iter(self.outq)) if self.outq else self.tx_seq

    def _pump_ahead(self):
        """How much modelled airtime may sit queued in front of the radio.

        Two data frames: enough to keep the channel full between 10 ms ticks,
        little enough that an ACK we owe the peer is never stuck behind a
        whole window of our own data.
        """
        frame = self._airtime(1 + HDR_LEN + self.frag_size + CRC_LEN
                              + self.seal_overhead)
        return max(self.queue_ahead, 2.0 * frame)

    def _pump(self, out, now):
        """Send queued frames while the window and the radio have room."""
        if not self._ready(now):
            return
        ahead = self._pump_ahead()
        while (self.busy_until - now) < ahead:
            if ((self.tx_seq - self.send_base()) % SEQ_MOD) >= self.window:
                return                                  # window full
            if self.txq_hi:
                item, bulk = self.txq_hi.popleft(), False
            elif self.txq:
                item, bulk = self.txq.popleft(), True
            else:
                return
            entry = dict(item, seq=self.tx_seq, tries=1, bulk=bulk)
            self.tx_seq = (self.tx_seq + 1) % SEQ_MOD
            self.outq[entry['seq']] = entry
            self._send_entry(out, entry, now)
            self.stats['tx_payload_bytes'] += len(item['payload'])
            if item['mid'] is not None:
                self._evt(out, e='tx_state', mid=item['mid'], state='sent',
                          tries=1, frag=item['frag'], queued=self.queued())

    def _flags(self, extra=0):
        return extra | (self.epoch << EPOCH_SHIFT)

    def _send_entry(self, out, entry, now):
        """(Re)transmit one window entry. Every copy carries the window base."""
        retry = entry['tries'] > 1
        self._tx(out, build(entry['ftype'], self.peer_addr, self.my_addr,
                            entry['seq'], self.send_base(), entry['payload'],
                            self._flags(FLAG_RETRY if retry else 0),
                            entry['seq'] + 37 * entry['tries']), now)
        entry['txno'] = self._txno
        self._txno += 1
        # busy_until is now the modelled instant this copy leaves the antenna.
        # Timing the ACK from there makes the timers immune to however deep the
        # transmit queue is; ack_gap allows for the receiver batching its SACK.
        entry['deadline'] = (self.busy_until + self.ack_timeout + self.ack_gap
                             + 2.0 * self.rx_latency)

    def _retransmit(self, out, entry, now, fast=False):
        entry['tries'] += 1
        self.stats['retx'] += 1
        if fast:
            self.stats['fast_retx'] += 1
        self._send_entry(out, entry, now)
        if entry['mid'] is not None:
            self._evt(out, e='tx_state', mid=entry['mid'], state='retry',
                      tries=entry['tries'], frag=entry['frag'],
                      queued=self.queued())

    def _abandon(self, out, entry, now):
        """Give up on one frame after max_retries: the old drop path."""
        self.outq.pop(entry['seq'], None)
        self.stats['dropped'] += 1
        # The receiver may be holding later frames behind this gap; tell it
        # the window moved (also carried by every later data frame).
        self._sync_left, self._sync_next = 3, now
        label = entry['label']
        if entry['mid'] is not None:
            self._evt(out, e='tx_state', mid=entry['mid'], state='failed',
                      tries=entry['tries'], frag=entry['frag'],
                      queued=self.queued())
        if label.startswith('file-frag'):
            # Recoverable: the receiver will name it in its NACK. Counted
            # rather than logged, so one bad patch of channel does not bury
            # the console.
            self.stats['frag_lost'] += 1
        elif label == 'file-start' and self.tx_file is not None:
            # The very first frame is the one that gives the receiver its file
            # context, so losing it makes every fragment behind it
            # undeliverable. Restart the transfer rather than abandon it.
            self._restart_file(out)
        else:
            out.append(('log', 'dropped after %d attempts: %s'
                        % (entry['tries'], label)))

    def stats_line(self, now=None):
        s = self.stats
        good = s['rx_valid']
        total = good + s['rx_bad']
        per = (100.0 * s['rx_bad'] / total) if total else 0.0
        backlog = max(0.0, self.busy_until - now) if now is not None else 0.0
        return ('tx frames=%d bytes=%d retx=%d dropped=%d | '
                'rx ok=%d bad=%d (%.1f%%) dup=%d not-mine=%d | '
                'ack tx=%d rx=%d | idle tx=%d rx=%d | queued=%d | '
                'tx backlog=%.2fs' %
                (s['tx_frames'], s['tx_bytes'], s['retx'], s['dropped'],
                 good, s['rx_bad'], per, s['rx_dup'], s['rx_notme'],
                 s['ack_tx'], s['ack_rx'], s['idle_tx'], s['idle_rx'],
                 self.queued(), backlog) +
                ' | frags lost=%d file rounds=%d'
                % (s['frag_lost'], s['file_rounds']))

    def snapshot(self, now=None):
        """Machine-readable counterpart of stats_line(), for the UI."""
        s = dict(self.stats)
        total = s['rx_valid'] + s['rx_bad']
        s['per'] = (100.0 * s['rx_bad'] / total) if total else 0.0
        s['queued'] = self.queued()
        s['backlog'] = max(0.0, self.busy_until - now) if now is not None else 0.0
        s['peer_up'] = self.peer_up
        s['peer_addr'] = self.peer_addr
        s['my_addr'] = self.my_addr
        if self.outq:
            first = next(iter(self.outq.values()))
            s['pending'] = first['label'] + (' +%d' % (len(self.outq) - 1)
                                             if len(self.outq) > 1 else '')
        else:
            s['pending'] = None
        s['window'] = self.window
        s['inflight'] = len(self.outq)
        s['revision'] = REVISION
        s['sym_rate'] = self.sym_rate
        s['frag_size'] = self.frag_size
        s['ack_timeout'] = self.ack_timeout
        s['max_retries'] = self.max_retries
        return s

    # -------------------------------------------------------------- outbound
    def send_data(self, payload, now, mid=None, label='chat'):
        """Queue one opaque application payload as a DATA frame."""
        out = []
        payload = bytes(payload)[:MAX_PAYLOAD]
        self._enqueue(FT_DATA, payload, label, mid=mid, prio='msg')
        if mid is not None:
            self._evt(out, e='tx_state', mid=mid, state='queued',
                      queued=self.queued())
        self._pump(out, now)
        return out

    def send_file(self, name, data, now, meta=b'', mid=None, prepared=None,
                  items=None):
        """Segment an in-memory blob and queue the whole transfer.

        Same protocol as /sendfile, but the bytes come from the caller rather
        than from disk, and an opaque metadata blob rides along in FILE_START.

        `prepared` is an optional (fragments, crc32) tuple from segment(). Pass
        it when the caller has already fragmented the blob on another thread;
        this entry point then does no bulk work at all. `items` goes further:
        pass the list from build_file_items() and the transmit queue is filled
        with a single extend().
        """
        out = []
        if self.tx_file is not None:
            out.append(('log', 'a file transfer is already in progress'))
            self._evt(out, e='tx_file_done', mid=mid, ok=False,
                      reason='busy')
            return out
        name = os.path.basename(name) or 'unnamed'
        if prepared is not None:
            frags, crc = prepared
        else:
            frags, crc = segment(data, self.frag_size)
        if len(frags) > 0xFFFF:
            out.append(('log', 'file needs %d fragments, max 65535; '
                               'raise frag_size' % len(frags)))
            self._evt(out, e='tx_file_done', mid=mid, ok=False,
                      reason='too many fragments')
            return out
        self.tx_file = {'name': name, 'frags': frags, 'total': len(frags),
                        'size': len(data), 'crc': crc,
                        'round': 1, 'deadline': None, 'mid': mid,
                        'meta': meta, 'acked': set()}
        self._queue_whole_file(out, items=items)
        out.append(('log', 'sending %s: %d bytes in %d fragments'
                    % (name, len(data), len(frags))))
        self._evt(out, e='tx_file_start', mid=mid, name=name, size=len(data),
                  total=len(frags))
        self._pump(out, now)
        return out

    def _queue_whole_file(self, out, items=None):
        tf = self.tx_file
        if items is None:                  # repair path: rebuild them here
            items = build_file_items(tf['size'], tf['total'], tf['name'],
                                     tf['meta'], tf['crc'], tf['frags'],
                                     tf['mid'])
        self.txq.extend(items)

    def on_user(self, text, now):
        """Operator console entry point: raw text or a slash command."""
        out = []
        text = (text or '').strip()
        if not text:
            return out
        if text.startswith('/'):
            self._command(out, text, now)
        else:
            data = text.encode('utf-8', 'replace')[:self.frag_size]
            self._enqueue(FT_DATA, data, 'chat', prio='msg')
            out.append(('log', 'TX -> %d: %s' % (self.peer_addr, text)))
        self._pump(out, now)
        return out

    def _command(self, out, text, now):
        parts = text.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ''
        if cmd == '/help':
            out.append(('log', HELP))
        elif cmd == '/stats':
            out.append(('log', self.stats_line(now)))
        elif cmd == '/ping':
            self._enqueue(FT_DATA, b'ping', 'ping', prio='msg')
            out.append(('log', 'TX -> %d: ping' % self.peer_addr))
        elif cmd == '/sendfile':
            self._sendfile(out, arg, now)
        else:
            out.append(('log', 'unknown command %s' % cmd))

    def _sendfile(self, out, path, now=0.0):
        path = os.path.expanduser(path)
        if not path or not os.path.isfile(path):
            out.append(('log', 'no such file: %s' % path))
            return
        with open(path, 'rb') as fh:
            data = fh.read()
        out.extend(self.send_file(os.path.basename(path), data, now))

    # --------------------------------------------------------------- inbound
    def on_rx(self, raw, now):
        try:
            frm = parse(raw)
        except BadFrame:
            self.stats['rx_bad'] += 1
            return []
        self.stats['rx_valid'] += 1
        return self.on_frame(frm, now)

    def on_frame(self, frm, now):
        """Process one frame that has already been parsed and counted.

        The entry point for bpsk_secure: it opens a sealed frame and hands
        the original link frame here, so this state machine is identical with
        and without encryption.
        """
        out = []
        if frm['dst'] not in (self.my_addr, BROADCAST):
            self.stats['rx_notme'] += 1
            return out
        self.peer_seen = now
        if not self.peer_up:
            self.peer_up = True
            out.append(('log', 'peer %d reachable' % frm['src']))
            self._evt(out, e='peer', up=True, addr=frm['src'])

        ftype = frm['type']
        if ftype == FT_IDLE:
            self.stats['idle_rx'] += 1
            # Only a UNICAST idle may move the window: broadcast filler is
            # unauthenticated even with encryption on, and a forged base would
            # make us skip frames and then acknowledge them as old.
            if (frm['dst'] == self.my_addr and frm['flags'] & FLAG_SYNC
                    and frm['flags'] >> EPOCH_SHIFT):
                st = self.rx.get(frm['src'])
                if st is not None:
                    self._check_epoch(out, frm, st)
                    self._advance(out, frm['src'], st, frm['ack'], now)
            return out

        if ftype == FT_ACK:
            self.stats['ack_rx'] += 1
            self._on_sack(out, frm, now)
            return out

        # A data-bearing frame: Selective Repeat receive.
        src = frm['src']
        if not frm['flags'] >> EPOCH_SHIFT:
            # Epoch 0 is never generated by r5.4: this is an r5.3-or-older
            # node (stop-and-wait, no window base). Ignore it rather than let
            # it reset a live receive window.
            self.stats['rx_legacy'] += 1
            if self.stats['rx_legacy'] == 1:
                out.append(('log', 'ignoring frames from %d: it runs r5.3 or '
                                   'older (no Selective Repeat)' % src))
            return out
        st = self.rx.get(src)
        if st is None:
            # First frame from this sender: start at its window base.
            st = self.rx[src] = {'base': frm['ack'], 'buf': {},
                                 'epoch': frm['flags'] >> EPOCH_SHIFT}
        self._check_epoch(out, frm, st)
        self._advance(out, src, st, frm['ack'], now)
        seq = frm['seq']
        if (seq - st['base']) % SEQ_MOD < HALF and seq not in st['buf']:
            st['buf'][seq] = frm
            self._release(out, st, now)
        else:
            # Old, or already buffered: our SACK for it was lost.
            self.stats['rx_dup'] += 1
        self._ack_due[src] = True
        self._flush_acks(out, now)
        self._pump(out, now)
        return out

    # ----------------------------------------------------- selective repeat
    def _check_epoch(self, out, frm, st):
        """A new sender epoch means the peer restarted: its sequence
        numbering began again, so our receive window is meaningless."""
        ep = frm['flags'] >> EPOCH_SHIFT
        if ep != st['epoch']:
            st['epoch'] = ep
            st['base'] = frm['ack']
            st['buf'].clear()
            self.stats['rx_resync'] += 1
            out.append(('log', 'peer %d restarted its link (new epoch), '
                               'receive window reset' % frm['src']))

    def _release(self, out, st, now):
        """Deliver buffered frames in order from the window base."""
        buf = st['buf']
        while st['base'] in buf:
            frm = buf.pop(st['base'])
            st['base'] = (st['base'] + 1) % SEQ_MOD
            # Counted here, once per delivered frame: a duplicate is not
            # goodput.
            self.stats['rx_payload_bytes'] += len(frm['payload'])
            self._deliver(out, frm, now)

    def _advance(self, out, src, st, base, now):
        """Apply the sender's window base carried in its header."""
        diff = (base - st['base']) % SEQ_MOD
        if diff == 0 or diff >= SEQ_MOD - MAX_WINDOW:
            return                  # same, or the sender lags (our SACK lost)
        if diff < HALF:
            # The sender gave up on everything below base: deliver what we
            # hold in that range, in order, and skip the gaps.
            buf = st['buf']
            while st['base'] != base:
                frm = buf.pop(st['base'], None)
                st['base'] = (st['base'] + 1) % SEQ_MOD
                if frm is None:
                    self.stats['rx_skipped'] += 1
                else:
                    self.stats['rx_payload_bytes'] += len(frm['payload'])
                    self._deliver(out, frm, now)
            self._release(out, st, now)
            return
        # Neither ahead nor a plausible lag: resynchronise.
        st['base'] = base
        st['buf'].clear()
        self.stats['rx_resync'] += 1

    def _sack(self, src):
        st = self.rx[src]
        base, bits = st['base'], 0
        for seq in st['buf']:
            d = (seq - base) % SEQ_MOD
            if 1 <= d <= SACK_BITS:
                bits |= 1 << (d - 1)
        return bytes([base]) + bits.to_bytes(16, 'little')

    def _flush_acks(self, out, now, force=False):
        """Send owed SACKs, at most one per ack_gap per peer.

        One SACK covers everything received so far (cumulative base plus a
        bitmap of what is held beyond it), so batching them loses nothing and
        halves the reverse-channel airtime spent on ACKs at full rate.
        """
        if not self.radio.enabled:
            return                  # owed ACKs go out when we may transmit
        for src, due in list(self._ack_due.items()):
            if not due or src not in self.rx:
                continue
            if not force and now - self._ack_last.get(src, -1e9) < self.ack_gap:
                continue
            payload = self._sack(src)
            self._tx(out, build(FT_ACK, src, self.my_addr, 0, payload[0],
                                payload, 0, int(now * 1000)), now)
            self.stats['ack_tx'] += 1
            self._ack_due[src] = False
            self._ack_last[src] = now

    def _on_sack(self, out, frm, now):
        pl = frm['payload']
        if len(pl) >= 17:
            base = pl[0]
            bits = int.from_bytes(pl[1:17], 'little')
        else:                       # bare ACK: that one sequence number
            base, bits = None, 0
        acked = []
        for seq, entry in list(self.outq.items()):
            if base is None:
                hit = seq == frm['ack']
            else:
                d = (seq - base) % SEQ_MOD
                hit = d >= HALF or (1 <= d <= SACK_BITS
                                    and (bits >> (d - 1)) & 1)
            if hit:
                acked.append(self.outq.pop(seq))
        if not acked:
            return
        for entry in acked:
            self._on_acked(out, entry)
        # Fast retransmit: the channel is FIFO, so a frame still unacked while
        # a frame transmitted AFTER it has been acknowledged was lost. Resend
        # it now instead of waiting ack_timeout (which would stall the window).
        newest = max(e['txno'] for e in acked)
        if self._ready(now):
            for entry in list(self.outq.values()):
                if entry['txno'] < newest:
                    if entry['tries'] > self.max_retries:
                        self._abandon(out, entry, now)
                    else:
                        self._retransmit(out, entry, now, fast=True)
        self._pump(out, now)

    def _on_acked(self, out, done):
        """One outstanding frame was acknowledged by the peer."""
        if done['mid'] is None:
            return
        self._evt(out, e='tx_state', mid=done['mid'], state='acked',
                  tries=done['tries'], frag=done['frag'],
                  queued=self.queued())
        tf = self.tx_file
        if tf is not None and done['mid'] == tf['mid'] and done['frag'] is not None:
            tf['acked'].add(done['frag'])
            self._evt(out, e='tx_file_progress', mid=tf['mid'],
                      acked=len(tf['acked']), total=tf['total'])

    def _deliver(self, out, frm, now):
        ftype, payload, src = frm['type'], frm['payload'], frm['src']
        if ftype == FT_DATA:
            self._evt(out, e='rx_data', src=src, data=payload)
        elif ftype == FT_FILE_START:
            info = unpack_file_start(payload)
            if info is None:
                return
            size, total, name, meta = info
            rf = self.rx_file
            if (rf is not None and rf['src'] == src and rf['name'] == name
                    and rf['total'] == total and rf['meta'] == meta):
                return          # repair round for a transfer already in progress
            last = self.rx_last
            if (meta and last is not None and last['src'] == src
                    and last['name'] == name and last['total'] == total
                    and last['meta'] == meta):
                return          # header of a transfer that already completed
            if rf is not None:
                # A different transfer is starting while an older one is still
                # incomplete: the sender gave up on it. Its fragments must not
                # be reused - r5.1 matched only (name, fragment count), so a
                # second 'image.png' of similar size inherited the first one's
                # fragments and a single lost fragment became a CRC failure
                # instead of a repair round. The metadata blob carries the
                # sender's message id and timestamp, so it tells them apart.
                out.append(('log', 'incoming file %s from %d abandoned by the '
                                   'sender (%d of %d fragments)'
                            % (rf['name'], rf['src'], len(rf['frags']),
                               rf['total'])))
                self._evt(out, e='rx_file_abandoned', src=rf['src'],
                          name=rf['name'], got=len(rf['frags']),
                          total=rf['total'])
            self.rx_file = {'src': src, 'size': size, 'total': total,
                            'name': name, 'frags': {}, 'meta': meta,
                            'started': now}
            out.append(('log', 'incoming file %s from %d: %d bytes, %d fragments'
                        % (name, src, size, total)))
            self._evt(out, e='rx_file_start', src=src, name=name, size=size,
                      total=total, meta=meta)
        elif ftype == FT_FILE_DATA:
            if (self.rx_file is None or len(payload) < 2
                    or src != self.rx_file['src']):
                return
            idx = struct.unpack('!H', payload[:2])[0]
            if idx >= self.rx_file['total']:
                return
            self.rx_file['frags'][idx] = payload[2:]
            got, total = len(self.rx_file['frags']), self.rx_file['total']
            self._evt(out, e='rx_file_progress', name=self.rx_file['name'],
                      src=self.rx_file['src'], got=got, total=total)
            if total and got % max(1, total // 10) == 0:
                out.append(('log', '  file %d/%d fragments' % (got, total)))
        elif ftype == FT_FILE_END:
            self._finish_file(out, payload)
        elif ftype == FT_FILE_NACK:
            self._on_nack(out, payload)
        elif ftype == FT_FILE_DONE:
            self._on_done(out, payload)

    def _finish_file(self, out, payload):
        want = struct.unpack('!I', payload[:4])[0] if len(payload) >= 4 else None
        meta = self.rx_file
        if meta is None:
            # FILE_END with no transfer open: our FILE_DONE for the file we
            # just completed was lost and the sender is asking again. r5.1
            # ignored this, so the sender exhausted its rounds and reported a
            # file that had arrived intact as failed. Repeat the verdict.
            last = self.rx_last
            if last is not None and want is not None and want == last['want']:
                self._enqueue(FT_FILE_DONE, last['reply'], 'file-done',
                              prio='ctl')
                out.append(('log', 'file %s: the sender asked again, repeating '
                                   'the verdict' % last['name']))
            return
        missing = [i for i in range(meta['total']) if i not in meta['frags']]
        if missing:
            # Tell the sender exactly what is missing and keep what we have.
            # Replies go to the front of the queue (see txq_hi), and an older
            # NACK still waiting there is superseded by this one.
            chunk = missing[:MAX_NACK_INDICES]
            self.txq_hi = deque(i for i in self.txq_hi
                                if i['label'] != 'file-nack')
            self._enqueue(FT_FILE_NACK,
                          b''.join(struct.pack('!H', i) for i in chunk),
                          'file-nack', prio='ctl')
            out.append(('log', 'file %s: %d of %d fragments missing, asking '
                               'the sender to resend %d of them'
                        % (meta['name'], len(missing), meta['total'],
                           len(chunk))))
            self._evt(out, e='rx_file_repair', name=meta['name'],
                      missing=len(missing), asked=len(chunk))
            return
        self.rx_file = None
        data = b''.join(meta['frags'][i] for i in range(meta['total']))
        got = zlib.crc32(data) & 0xFFFFFFFF
        dest = unique_path(self.rx_dir, 'rx_' + meta['name'])
        try:
            with open(dest, 'wb') as fh:
                fh.write(data)
        except (OSError, ValueError) as exc:
            out.append(('log', 'could not write %s: %s' % (dest, exc)))
            dest = None
        ok = (want is None or want == got)
        reply = struct.pack('!BI', 0 if ok else 1, got)
        self.rx_last = {'src': meta['src'], 'name': meta['name'],
                        'total': meta['total'], 'meta': meta['meta'],
                        'want': want, 'reply': reply}
        self._enqueue(FT_FILE_DONE, reply, 'file-done', prio='ctl')
        if ok:
            out.append(('log', 'file %s written, %d bytes, CRC ok'
                        % (dest, len(data))))
        else:
            out.append(('log', 'file %s written (%d bytes) but CRC MISMATCH '
                               '(%08x != %08x)' % (dest, len(data), got, want)))
        self._evt(out, e='rx_file_done', name=meta['name'], path=dest,
                  ok=ok, size=len(data), crc=got, data=data,
                  meta=meta.get('meta', b''), src=meta.get('src'))

    def _on_nack(self, out, payload):
        """Receiver reported gaps: requeue exactly those fragments."""
        tf = self.tx_file
        if tf is None:
            return
        idxs = [struct.unpack('!H', payload[i:i + 2])[0]
                for i in range(0, len(payload) - 1, 2)]
        idxs = [i for i in idxs if i < tf['total']]
        if not idxs:
            return
        if tf['round'] >= self.max_file_rounds:
            out.append(('log', 'file %s: still %d fragments short after %d '
                               'rounds, giving up'
                        % (tf['name'], len(idxs), tf['round'])))
            self._purge_file_queue()
            self._evt(out, e='tx_file_done', mid=tf['mid'], ok=False,
                      reason='%d fragments never got through' % len(idxs))
            self.tx_file = None
            return
        tf['round'] += 1
        tf['deadline'] = None
        self.stats['file_rounds'] += 1
        tf['acked'].difference_update(idxs)
        for i in idxs:
            self._enqueue(FT_FILE_DATA,
                          struct.pack('!H', i) + tf['frags'][i],
                          'file-frag %d' % i, mid=tf['mid'], frag=i)
        self._enqueue(FT_FILE_END, struct.pack('!I', tf['crc']), 'file-end',
                      mid=tf['mid'])
        out.append(('log', 'peer is missing %d fragments, resending them '
                           '(round %d)' % (len(idxs), tf['round'])))
        self._evt(out, e='tx_file_round', mid=tf['mid'], round=tf['round'],
                  missing=len(idxs))

    def _on_done(self, out, payload):
        """Receiver confirmed the file. This is the sender's end-to-end result.

        FILE_DONE names no transfer, so a late one could land on the NEXT
        file and mark it delivered while its fragments were still queued. An
        OK verdict carries the receiver's CRC of what it reassembled; one that
        does not match this transfer is a leftover and is ignored.
        """
        tf = self.tx_file
        status = payload[0] if payload else 1
        crc = struct.unpack('!I', payload[1:5])[0] if len(payload) >= 5 else None
        if tf is None:
            out.append(('log', 'late file report ignored - no transfer in '
                               'progress'))
            return
        if status == 0 and crc is not None and crc != tf['crc']:
            out.append(('log', 'ignored a report for an earlier file (CRC %08x, '
                               '%s has %08x)' % (crc, tf['name'], tf['crc'])))
            return
        name, mid = tf['name'], tf['mid']
        self.tx_file = None
        self._purge_file_queue()        # repeated FILE_ENDs still waiting
        if status == 0:
            out.append(('log', 'peer confirmed %s received complete and '
                               'CRC-correct' % name))
            self._evt(out, e='tx_file_done', mid=mid, ok=True)
        else:
            out.append(('log', 'peer reassembled %s but the CRC did not match; '
                               'the file is corrupt at the far end' % name))
            self._evt(out, e='tx_file_done', mid=mid, ok=False,
                      reason='CRC mismatch at the receiver')

    def _restart_file(self, out):
        """Requeue the whole transfer after FILE_START was lost."""
        tf = self.tx_file
        if tf is None:
            return
        if tf['round'] >= self.max_file_rounds:
            out.append(('log', 'file %s: the peer never acknowledged '
                               'FILE_START after %d attempts, giving up'
                        % (tf['name'], tf['round'])))
            self._purge_file_queue()
            self._evt(out, e='tx_file_done', mid=tf['mid'], ok=False,
                      reason='the peer never acknowledged the file header')
            self.tx_file = None
            return
        tf['round'] += 1
        tf['deadline'] = None
        tf['acked'].clear()
        self.stats['file_rounds'] += 1
        self._purge_file_queue()
        self._queue_whole_file(out)
        out.append(('log', 'file %s: FILE_START lost, restarting the transfer '
                           '(round %d)' % (tf['name'], tf['round'])))
        self._evt(out, e='tx_file_round', mid=tf['mid'], round=tf['round'],
                  missing=tf['total'])

    def _purge_file_queue(self):
        """Drop what is left of OUR transfer.

        Only the bulk queue. r5.1 filtered every label starting with 'file',
        which also removed the FILE_NACK / FILE_DONE replies this node owed the
        peer for the peer's own transfer - a restart or give-up on one
        direction silently stalled the other.
        """
        self.txq = deque(item for item in self.txq
                         if not item['label'].startswith('file'))
        # Frames of our transfer already in the window are abandoned too; the
        # window base moves past them (every data frame carries it).
        stale = [e for e in self.outq.values()
                 if e['bulk'] and e['label'].startswith('file')]
        for entry in stale:
            self.outq.pop(entry['seq'], None)
        if stale:
            self._sync_left, self._sync_next = 3, 0.0

    # ------------------------------------------------------------------ tick
    def on_tick(self, now):
        """Called periodically. Drives the retransmit timer and TX filler."""
        out = []
        ready = self._ready(now)
        for entry in list(self.outq.values()):
            if entry['seq'] not in self.outq or now < entry['deadline']:
                continue
            if not ready:
                # No secure session at the moment (the peer restarted, or a
                # session expired). A retransmission would be dropped unsealed,
                # so hold the frame without spending a try - the handshake
                # normally takes well under a second - but not for ever.
                held = entry.setdefault('held_since', now)
                if now - held < HOLD_LIMIT:
                    entry['deadline'] = now + self.ack_timeout
                    continue
                entry['tries'] = self.max_retries + 1
            entry.pop('held_since', None)
            if entry['tries'] > self.max_retries:
                self._abandon(out, entry, now)
            else:
                self._retransmit(out, entry, now)
        self._flush_acks(out, now)
        if self._sync_left and now >= self._sync_next and ready:
            self._sync_left -= 1
            self._sync_next = now + 0.2
            self._tx(out, build(FT_IDLE, self.peer_addr, self.my_addr, 0,
                                self.send_base(), b'',
                                self._flags(FLAG_SYNC), 0), now)
        self._pump(out, now)

        # Keep the Pluto TX buffer fed so the carrier is continuous and the
        # far end keeps timing/carrier lock. Emitting only when the modelled
        # backlog runs low bounds the queue instead of flooding it. Top it up
        # to queue_ahead rather than one frame per tick: with a 64-byte
        # preamble a filler frame lasts ~5 ms, less than the 10 ms tick, and
        # one per tick left the carrier off half the time.
        for _ in range(FILLER_BURST):
            if not (self.filler and self.radio.enabled and self.queue_ahead > 0
                    and (self.busy_until - now) < self.queue_ahead):
                break
            self.stats['idle_tx'] += 1
            self._idle_off = (self._idle_off + 1) & 0xFF
            self._tx(out, build(FT_IDLE, BROADCAST, self.my_addr, 0, 0,
                                b'', 0, self._idle_off), now, sealed=False)

        # Everything queued has gone out; the transfer is now waiting on the
        # peer's verdict. Without this the sender simply falls silent and never
        # learns whether the file arrived.
        tf = self.tx_file
        own_pending = any(e['bulk'] for e in self.outq.values())
        if tf is not None and not self._ready(now):
            tf['deadline'] = None           # the wait restarts with the session
        elif tf is not None and not self.txq and not own_pending:
            if tf['deadline'] is None:
                tf['deadline'] = now + 3.0
            elif now >= tf['deadline']:
                if tf['round'] >= self.max_file_rounds:
                    out.append(('log', 'file %s: no reply from the peer after '
                                       '%d rounds, giving up'
                                % (tf['name'], tf['round'])))
                    self._evt(out, e='tx_file_done', mid=tf['mid'], ok=False,
                              reason='the peer never reported the result')
                    self.tx_file = None
                else:
                    tf['round'] += 1
                    tf['deadline'] = None
                    self.stats['file_rounds'] += 1
                    self._enqueue(FT_FILE_END,
                                  struct.pack('!I', tf['crc']), 'file-end',
                                  mid=tf['mid'])
                    out.append(('log', 'file %s: no report from the peer yet, '
                                       'asking again (round %d)'
                                % (tf['name'], tf['round'])))
                    self._pump(out, now)

        quiet = PEER_TIMEOUT + 2.0 * self.rx_latency
        if self.peer_up and self.peer_seen and (now - self.peer_seen) > quiet:
            self.peer_up = False
            out.append(('log', 'peer unreachable (no valid frame for %.0f s)'
                        % quiet))
            self._evt(out, e='peer', up=False, addr=self.peer_addr)
        return out


# --------------------------------------------------------------------------
# r6: several peers over one carrier
# --------------------------------------------------------------------------
_SUMMED = ('tx_frames', 'tx_bytes', 'retx', 'dropped', 'rx_dup', 'ack_tx',
           'ack_rx', 'frag_lost', 'file_rounds', 'rx_payload_bytes',
           'tx_payload_bytes', 'fast_retx', 'rx_skipped', 'rx_resync',
           'rx_legacy')


class LinkMux(object):
    """One LinkState engine per peer, all sharing this node's transmit channel.

    Each engine is the complete r5.4 link - Selective Repeat window, SACKs,
    file transfer in both directions, receive window per source - for exactly
    one peer, so per-peer state needs no new protocol logic. The mux adds only
    what is shared: the Radio (airtime model and the channel manager's
    on/off switch), the idle filler, beacons, and routing each received frame
    to the engine of its source.

    For code written against the two-node LinkState, `peer_addr` names a
    default peer, and the attributes that used to describe the one link
    (outq, tx_file, rx, stats, ...) describe it, or the sum over all peers.
    """

    def __init__(self, my_addr=1, peer_addr=None, ack_timeout=0.5,
                 max_retries=5, frag_size=256, queue_ahead=0.03,
                 sym_rate=250000.0, overhead=452, rx_dir='.', window=32,
                 ack_gap=0.03, filler='frames'):
        self.my_addr = int(my_addr) & 0xFF
        # 'frames': top the queue up with IDLE frames (r6.0: the carrier was
        # made of frames). 'keepalive': the flowgraph keeps the carrier up
        # itself (r6.1 transmit framer); send an IDLE only after KEEPALIVE
        # seconds of silence, as the presence signal of static mode.
        if filler not in ('frames', 'keepalive'):
            raise ValueError('filler must be frames or keepalive')
        self.filler = filler
        self.peer_addr = None if peer_addr is None else int(peer_addr) & 0xFF
        self.radio = Radio(sym_rate, overhead)
        self.queue_ahead = float(queue_ahead)
        self.sym_rate = float(sym_rate)
        self.overhead = int(overhead)
        self.rx_dir = rx_dir
        self.window = max(1, min(int(window), MAX_WINDOW))
        self.ack_timeout = float(ack_timeout)
        self._cfg = dict(ack_timeout=ack_timeout, frag_size=frag_size,
                         queue_ahead=queue_ahead, sym_rate=sym_rate,
                         overhead=overhead, rx_dir=rx_dir, window=window,
                         ack_gap=ack_gap)
        self._max_retries = int(max_retries)
        self.rx_latency = 0.0
        self._max_file_rounds = 8
        self.frag_size = max(16, min(int(frag_size), MAX_PAYLOAD - 8))
        self.epoch = random.randint(1, 63)
        self.engines = OrderedDict()
        self.tx_gate = None             # callable(now, peer) -> bool
        self._rr = 0
        self._idle_off = 0
        self.mstats = dict(rx_valid=0, rx_bad=0, rx_notme=0, rx_self=0,
                           idle_tx=0, idle_rx=0, beacon_tx=0, beacon_rx=0,
                           tx_frames=0, tx_bytes=0)
        if self.peer_addr is not None:
            self.engine(self.peer_addr)

    def set_rx_latency(self, seconds):
        """The measured receive-pipeline latency (own echo), for every
        peer engine's ACK deadlines. Capped at MAX_RX_LATENCY."""
        self.rx_latency = max(0.0, min(float(seconds), MAX_RX_LATENCY))
        for eng in self.engines.values():
            eng.rx_latency = self.rx_latency

    # ------------------------------------------------------------ engines
    def engine(self, addr, create=True):
        addr = int(addr) & 0xFF
        eng = self.engines.get(addr)
        if eng is None and create:
            eng = LinkState(self.my_addr, addr, max_retries=self._max_retries,
                            radio=self.radio, filler=False, epoch=self.epoch,
                            **self._cfg)
            eng.max_file_rounds = self._max_file_rounds
            eng.rx_latency = self.rx_latency
            eng.tx_gate = (lambda now, a=addr: self._gate(now, a))
            self.engines[addr] = eng
        return eng

    def _gate(self, now, addr):
        return self.tx_gate is None or self.tx_gate(now, addr)

    @staticmethod
    def _tag(addr, actions):
        """Engine events name the peer they concern ('peer' key)."""
        for kind, val in actions:
            if kind == 'evt':
                val.setdefault('peer', addr)
        return actions

    def count_rx(self, valid):
        self.mstats['rx_valid' if valid else 'rx_bad'] += 1

    def drop_peer(self, addr):
        """Forget a peer entirely (it changed address or left)."""
        return self.engines.pop(int(addr) & 0xFF, None)

    def set_address(self, addr):
        self.my_addr = int(addr) & 0xFF
        for eng in self.engines.values():
            eng.my_addr = self.my_addr

    @property
    def default(self):
        return (self.engines.get(self.peer_addr)
                if self.peer_addr is not None else None)

    # ---------------------------------------------- two-node compatibility
    def _dflt(self):
        eng = self.default
        if eng is None:
            raise AttributeError('no default peer')
        return eng

    busy_until = property(lambda self: self.radio.busy_until,
                          lambda self, v: setattr(self.radio, 'busy_until', v))
    seal_overhead = property(lambda self: self.radio.seal_overhead,
                             lambda self, v: setattr(self.radio,
                                                     'seal_overhead', v))
    outq = property(lambda self: self._dflt().outq)
    txq = property(lambda self: self._dflt().txq)
    txq_hi = property(lambda self: self._dflt().txq_hi)
    tx_seq = property(lambda self: self._dflt().tx_seq)
    tx_file = property(lambda self: self._dflt().tx_file)
    peer_up = property(lambda self: any(e.peer_up
                                        for e in self.engines.values()))
    _txno = property(lambda self: sum(e._txno for e in self.engines.values()))

    @property
    def max_retries(self):
        return self._max_retries

    @max_retries.setter
    def max_retries(self, value):
        self._max_retries = int(value)
        for eng in self.engines.values():
            eng.max_retries = self._max_retries

    @property
    def max_file_rounds(self):
        return self._max_file_rounds

    @max_file_rounds.setter
    def max_file_rounds(self, value):
        self._max_file_rounds = int(value)
        for eng in self.engines.values():
            eng.max_file_rounds = self._max_file_rounds

    @property
    def rx(self):
        """Receive windows of every peer, by source (live objects)."""
        merged = {}
        for eng in self.engines.values():
            merged.update(eng.rx)
        return merged

    @property
    def stats(self):
        s = dict((k, 0) for k in _SUMMED)
        for eng in self.engines.values():
            for k in _SUMMED:
                s[k] += eng.stats.get(k, 0)
        m = self.mstats
        s['tx_frames'] += m['tx_frames']
        s['tx_bytes'] += m['tx_bytes']
        for k in ('rx_valid', 'rx_bad', 'rx_notme', 'rx_self', 'idle_tx',
                  'idle_rx', 'beacon_tx', 'beacon_rx'):
            s[k] = m[k]
        s['tx_suppressed'] = self.radio.suppressed
        return s

    def queued(self):
        return sum(e.queued() for e in self.engines.values())

    # ------------------------------------------------------------- output
    def banner(self):
        return ('link up [%s]: my_addr=%d%s  frag=%dB  ack_timeout=%.2fs '
                'retries=%d  ARQ=%s\n%s'
                % (REVISION, self.my_addr,
                   '  peer_addr=%d' % self.peer_addr
                   if self.peer_addr is not None else '  peers: discovered',
                   self.frag_size, self.ack_timeout, self._max_retries,
                   'selective-repeat window %d per peer' % self.window
                   if self.window > 1 else 'stop-and-wait (window 1)', HELP))

    def stats_line(self, now=None):
        s = self.stats
        good = s['rx_valid']
        total = good + s['rx_bad']
        per = (100.0 * s['rx_bad'] / total) if total else 0.0
        backlog = max(0.0, self.busy_until - now) if now is not None else 0.0
        return ('tx frames=%d bytes=%d retx=%d dropped=%d | rx ok=%d bad=%d '
                '(%.1f%%) dup=%d not-mine=%d | ack tx=%d rx=%d | idle tx=%d '
                'rx=%d | queued=%d | tx backlog=%.2fs | frags lost=%d file '
                'rounds=%d | peers=%d'
                % (s['tx_frames'], s['tx_bytes'], s['retx'], s['dropped'],
                   good, s['rx_bad'], per, s['rx_dup'], s['rx_notme'],
                   s['ack_tx'], s['ack_rx'], s['idle_tx'], s['idle_rx'],
                   self.queued(), backlog, s['frag_lost'], s['file_rounds'],
                   len(self.engines)))

    def snapshot(self, now=None):
        s = self.stats
        total = s['rx_valid'] + s['rx_bad']
        s['per'] = (100.0 * s['rx_bad'] / total) if total else 0.0
        s['queued'] = self.queued()
        s['backlog'] = max(0.0, self.busy_until - now) if now is not None else 0.0
        s['peer_up'] = self.peer_up
        s['peer_addr'] = self.peer_addr
        s['my_addr'] = self.my_addr
        s['inflight'] = sum(len(e.outq) for e in self.engines.values())
        busy = [e for e in self.engines.values() if e.outq]
        if busy:
            first = next(iter(busy[0].outq.values()))
            s['pending'] = '%s -> %d%s' % (first['label'], busy[0].peer_addr,
                                           ' +%d' % (s['inflight'] - 1)
                                           if s['inflight'] > 1 else '')
        else:
            s['pending'] = None
        s['window'] = self.window
        s['revision'] = REVISION
        s['sym_rate'] = self.sym_rate
        s['frag_size'] = self.frag_size
        s['ack_timeout'] = self.ack_timeout
        s['max_retries'] = self._max_retries
        s['tx_enabled'] = self.radio.enabled
        s['links'] = {a: {'up': e.peer_up, 'inflight': len(e.outq),
                          'queued': e.queued(), 'retx': e.stats['retx'],
                          'file': bool(e.tx_file),
                          'rx_bytes': e.stats['rx_payload_bytes'],
                          'tx_bytes': e.stats['tx_payload_bytes']}
                      for a, e in self.engines.items()}
        return s

    # ----------------------------------------------------------- outbound
    def send_data(self, payload, now, mid=None, label='chat', dst=None):
        dst = self._dst(dst)
        return self._tag(dst, self.engine(dst).send_data(payload, now,
                                                         mid=mid, label=label))

    def send_file(self, name, data, now, meta=b'', mid=None, prepared=None,
                  items=None, dst=None):
        dst = self._dst(dst)
        return self._tag(dst, self.engine(dst).send_file(
            name, data, now, meta=meta, mid=mid, prepared=prepared,
            items=items))

    def tx_file_for(self, dst):
        eng = self.engines.get(int(dst) & 0xFF)
        return eng.tx_file if eng is not None else None

    def _dst(self, dst):
        if dst is None:
            dst = self.peer_addr
        if dst is None:
            raise ValueError('no destination and no default peer')
        return int(dst) & 0xFF

    def on_user(self, text, now):
        text = (text or '').strip()
        if text.startswith('/stats'):
            return [('log', self.stats_line(now))]
        if text.startswith('/help'):
            return [('log', HELP)]
        eng = self.default
        return (self._tag(eng.peer_addr, eng.on_user(text, now))
                if eng is not None else [])

    def send_beacon(self, payload, now):
        out = []
        self._idle_off = (self._idle_off + 1) & 0xFF
        n = self.radio.send(out, build(FT_BEACON, BROADCAST, self.my_addr, 0,
                                       0, payload, 0, self._idle_off), now,
                            sealed=False)
        if n:
            self.mstats['beacon_tx'] += 1
            self.mstats['tx_frames'] += 1
            self.mstats['tx_bytes'] += n
        return out

    def note_tx(self, frame_len, now):
        self.radio.busy_until = (max(self.radio.busy_until, now)
                                 + self.radio.airtime(frame_len))
        self.radio.last_tx = now
        self.mstats['tx_frames'] += 1
        self.mstats['tx_bytes'] += frame_len

    def kick(self, now):
        out = []
        for addr, eng in list(self.engines.items()):
            part = []
            eng._pump(part, now)
            out.extend(self._tag(addr, part))
        return out

    def reset_peer(self, src):
        src = int(src) & 0xFF
        eng = self.engines.get(src)
        return self._tag(src, eng.reset_peer(src)) if eng is not None else []

    # ------------------------------------------------------------ inbound
    def on_rx(self, raw, now):
        try:
            frm = parse(raw)
        except BadFrame:
            self.mstats['rx_bad'] += 1
            return []
        self.mstats['rx_valid'] += 1
        return self.on_frame(frm, now)

    def on_frame(self, frm, now):
        """Route one parsed frame. The caller counted rx_valid/rx_bad."""
        out = []
        dst, src, ftype = frm['dst'], frm['src'], frm['type']
        if src == self.my_addr:
            # Every node demodulates its own channel too; its own frames come
            # back and are dropped here.
            self.mstats['rx_self'] += 1
            return out
        if dst not in (self.my_addr, BROADCAST):
            self.mstats['rx_notme'] += 1
            return out
        if ftype == FT_BEACON:
            self.mstats['beacon_rx'] += 1
            eng = self.engines.get(src)
            if eng is not None:
                eng.peer_seen = now
            out.append(('evt', {'e': 'beacon', 'src': src,
                                'payload': frm['payload']}))
            return out
        if ftype == FT_IDLE and dst == BROADCAST:
            self.mstats['idle_rx'] += 1
            eng = self.engines.get(src)
            if eng is not None:
                out.extend(self._tag(src, eng.on_frame(frm, now)))
            return out
        return self._tag(src, self.engine(src).on_frame(frm, now))

    # --------------------------------------------------------------- tick
    def on_tick(self, now):
        out = []
        engines = list(self.engines.items())
        if engines:
            k = self._rr % len(engines)
            self._rr += 1
            for addr, eng in engines[k:] + engines[:k]:     # round robin
                out.extend(self._tag(addr, eng.on_tick(now)))
        burst = FILLER_BURST
        if self.filler == 'keepalive':
            burst = 1 if (now - self.radio.last_tx) >= KEEPALIVE else 0
        for _ in range(burst):                 # see LinkState.on_tick
            if not (self.radio.enabled and self.queue_ahead > 0
                    and (self.radio.busy_until - now) < self.queue_ahead):
                break
            self._idle_off = (self._idle_off + 1) & 0xFF
            n = self.radio.send(out, build(FT_IDLE, BROADCAST, self.my_addr,
                                           0, 0, b'', 0, self._idle_off), now,
                                sealed=False)
            if n:
                self.mstats['idle_tx'] += 1
                self.mstats['tx_frames'] += 1
                self.mstats['tx_bytes'] += n
        return out
