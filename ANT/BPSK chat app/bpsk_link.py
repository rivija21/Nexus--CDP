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
"""

import os
import struct
import zlib
from collections import deque

VERSION = 1
HDR_LEN = 8
CRC_LEN = 4
SCRAM_LEN = 1        # clear-text scrambler offset byte
BROADCAST = 0xFF
MAX_PAYLOAD = 1024

FT_IDLE = 0x0
FT_DATA = 0x1
FT_ACK = 0x2
FT_FILE_START = 0x3
FT_FILE_DATA = 0x4
FT_FILE_END = 0x5
FT_FILE_NACK = 0x6         # receiver -> sender: these fragments never arrived
FT_FILE_DONE = 0x7         # receiver -> sender: file complete, here is the verdict

FT_NAME = {FT_IDLE: 'IDLE', FT_DATA: 'DATA', FT_ACK: 'ACK',
           FT_FILE_START: 'FILE_START', FT_FILE_DATA: 'FILE_DATA',
           FT_FILE_END: 'FILE_END', FT_FILE_NACK: 'FILE_NACK',
           FT_FILE_DONE: 'FILE_DONE'}

# Bumped whenever the wire format or the file protocol changes. Printed in the
# banner so both consoles show which build they are running - a mismatched pair
# is otherwise indistinguishable from a bad radio link.
REVISION = 'r5-applayer'

MAX_NACK_INDICES = 120     # fragment indices that fit in one NACK frame

FLAG_RETRY = 0x01

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


def _scramble(data, offset):
    """XOR with the keystream. Its own inverse, so TX and RX share it.

    Without this the transmitted waveform is a direct function of the payload.
    Differential BPSK turns a run of zero bits into a run of symbols with no
    phase transition: no timing information for the TED, and a pure tone for
    the band-edge FLL and the AD9363 DC-offset tracking to fight over. Real
    files - JPEG especially - contain such runs, and because a retransmission
    resends the identical bit pattern, ARQ cannot recover from one.
    """
    ks = KEYSTREAM
    return bytes(b ^ ks[(i + offset) % KLEN] for i, b in enumerate(data))

HELP = (
    "commands:\n"
    "  <text>              send a text message to the peer\n"
    "  /sendfile <path>    segment and send a file\n"
    "  /stats              print link counters\n"
    "  /ping               send a one-word probe\n"
    "  /help               this list"
)


class BadFrame(Exception):
    """Raised by parse() when a received buffer is not a valid link frame."""


def build(ftype, dst, src, seq, ack, payload=b'', flags=0, offset=0):
    """Serialise one link frame: clear offset byte, then scrambled body+CRC."""
    if len(payload) > MAX_PAYLOAD:
        raise ValueError('payload too long: %d' % len(payload))
    body = _HDR.pack(((VERSION & 0x0F) << 4) | (ftype & 0x0F),
                     dst & 0xFF, src & 0xFF, seq & 0xFF, ack & 0xFF,
                     flags & 0xFF, len(payload)) + payload
    body += struct.pack('!I', zlib.crc32(body) & 0xFFFFFFFF)
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
        name = os.path.basename(rest.decode('utf-8', 'replace')) or 'unnamed'
        return size, total, name, b''
    if len(rest) < 2:
        return None
    nlen = rest[1]
    name = os.path.basename(rest[2:2 + nlen].decode('utf-8', 'replace')) or 'unnamed'
    tail = rest[2 + nlen:]
    meta = b''
    if len(tail) >= 2:
        mlen = struct.unpack('!H', tail[:2])[0]
        meta = tail[2:2 + mlen]
    return size, total, name, meta


class LinkState(object):
    """Addressing + stop-and-wait ARQ + file segmentation state machine.

    Every entry point takes the current time and returns a list of actions:
        ('tx', bytes)  -- hand this frame to the modulator
        ('log', str)   -- show this line to the operator
        ('evt', dict)  -- a structured protocol event, for the application layer
    """

    def __init__(self, my_addr=1, peer_addr=2, ack_timeout=0.5, max_retries=5,
                 frag_size=256, queue_ahead=0.03, sym_rate=250000.0,
                 overhead=452, rx_dir='.'):
        self.my_addr = int(my_addr) & 0xFF
        self.peer_addr = int(peer_addr) & 0xFF
        self.ack_timeout = float(ack_timeout)
        self.max_retries = int(max_retries)
        self.frag_size = max(16, min(int(frag_size), MAX_PAYLOAD - 8))
        self.queue_ahead = float(queue_ahead)
        self.sym_rate = float(sym_rate)
        self.overhead = int(overhead)
        self.rx_dir = rx_dir

        self.txq = deque()
        self.pending = None
        self.tx_seq = 0
        self.last_rx_seq = {}
        self.busy_until = 0.0
        self.peer_seen = 0.0
        self.peer_up = False
        self.rx_file = None
        self.tx_file = None
        self.max_file_rounds = 8
        self._idle_off = 0

        self.stats = dict(tx_frames=0, tx_bytes=0, retx=0, dropped=0,
                          rx_valid=0, rx_bad=0, rx_notme=0, rx_dup=0,
                          ack_tx=0, ack_rx=0, idle_tx=0, idle_rx=0,
                          frag_lost=0, file_rounds=0,
                          rx_payload_bytes=0, tx_payload_bytes=0)

    # ------------------------------------------------------------------ util
    def banner(self):
        return ('link up [%s]: my_addr=%d peer_addr=%d  frag=%dB  '
                'ack_timeout=%.2fs retries=%d\n%s'
                % (REVISION, self.my_addr, self.peer_addr, self.frag_size,
                   self.ack_timeout, self.max_retries, HELP))

    def _airtime(self, frame_len):
        """Seconds of RF this frame occupies, preamble/postamble included."""
        return AIRTIME_MARGIN * (self.overhead + frame_len) * 8.0 / self.sym_rate

    @staticmethod
    def _evt(out, **kw):
        out.append(('evt', kw))

    def _tx(self, out, frame, now):
        self.busy_until = max(self.busy_until, now) + self._airtime(len(frame))
        self.stats['tx_frames'] += 1
        self.stats['tx_bytes'] += len(frame)
        out.append(('tx', frame))

    def _enqueue(self, ftype, payload, label, mid=None, frag=None):
        self.txq.append({'ftype': ftype, 'payload': payload, 'label': label,
                         'mid': mid, 'frag': frag})

    def _pump(self, out, now):
        """Start the next queued frame if the channel is not already busy."""
        if self.pending is not None or not self.txq:
            return
        item = self.txq.popleft()
        self._tx(out, build(item['ftype'], self.peer_addr, self.my_addr,
                            self.tx_seq, 0, item['payload'], 0, self.tx_seq),
                 now)
        self.stats['tx_payload_bytes'] += len(item['payload'])
        # busy_until is now the modelled instant this frame leaves the antenna.
        # Timing the ACK from here rather than from now makes the ARQ immune to
        # however deep the transmit queue happens to be.
        self.pending = dict(item, seq=self.tx_seq,
                            deadline=self.busy_until + self.ack_timeout,
                            tries=1)
        if item['mid'] is not None:
            self._evt(out, e='tx_state', mid=item['mid'], state='sent',
                      tries=1, frag=item['frag'], queued=len(self.txq))

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
                 len(self.txq), backlog) +
                ' | frags lost=%d file rounds=%d'
                % (s['frag_lost'], s['file_rounds']))

    def snapshot(self, now=None):
        """Machine-readable counterpart of stats_line(), for the UI."""
        s = dict(self.stats)
        total = s['rx_valid'] + s['rx_bad']
        s['per'] = (100.0 * s['rx_bad'] / total) if total else 0.0
        s['queued'] = len(self.txq)
        s['backlog'] = max(0.0, self.busy_until - now) if now is not None else 0.0
        s['peer_up'] = self.peer_up
        s['peer_addr'] = self.peer_addr
        s['my_addr'] = self.my_addr
        s['pending'] = self.pending['label'] if self.pending else None
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
        self._enqueue(FT_DATA, payload, label, mid=mid)
        if mid is not None:
            self._evt(out, e='tx_state', mid=mid, state='queued',
                      queued=len(self.txq))
        self._pump(out, now)
        return out

    def send_file(self, name, data, now, meta=b'', mid=None):
        """Segment an in-memory blob and queue the whole transfer.

        Same protocol as /sendfile, but the bytes come from the caller rather
        than from disk, and an opaque metadata blob rides along in FILE_START.
        """
        out = []
        if self.tx_file is not None:
            out.append(('log', 'a file transfer is already in progress'))
            self._evt(out, e='tx_file_done', mid=mid, ok=False,
                      reason='busy')
            return out
        name = os.path.basename(name) or 'unnamed'
        step = self.frag_size - 2                      # 2 bytes fragment index
        frags = [data[i:i + step] for i in range(0, len(data), step)] or [b'']
        if len(frags) > 0xFFFF:
            out.append(('log', 'file needs %d fragments, max 65535; '
                               'raise frag_size' % len(frags)))
            self._evt(out, e='tx_file_done', mid=mid, ok=False,
                      reason='too many fragments')
            return out
        self.tx_file = {'name': name, 'frags': frags, 'total': len(frags),
                        'size': len(data),
                        'crc': zlib.crc32(data) & 0xFFFFFFFF,
                        'round': 1, 'deadline': None, 'mid': mid,
                        'meta': meta, 'acked': set()}
        self._queue_whole_file(out)
        out.append(('log', 'sending %s: %d bytes in %d fragments'
                    % (name, len(data), len(frags))))
        self._evt(out, e='tx_file_start', mid=mid, name=name, size=len(data),
                  total=len(frags))
        self._pump(out, now)
        return out

    def _queue_whole_file(self, out):
        tf = self.tx_file
        self._enqueue(FT_FILE_START,
                      pack_file_start(tf['size'], tf['total'], tf['name'],
                                      tf['meta']),
                      'file-start', mid=tf['mid'])
        for i, frag in enumerate(tf['frags']):
            self._enqueue(FT_FILE_DATA, struct.pack('!H', i) + frag,
                          'file-frag %d' % i, mid=tf['mid'], frag=i)
        self._enqueue(FT_FILE_END, struct.pack('!I', tf['crc']), 'file-end',
                      mid=tf['mid'])

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
            self._enqueue(FT_DATA, data, 'chat')
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
            self._enqueue(FT_DATA, b'ping', 'ping')
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
        out = []
        try:
            frm = parse(raw)
        except BadFrame:
            self.stats['rx_bad'] += 1
            return out
        self.stats['rx_valid'] += 1
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
            return out

        if ftype == FT_ACK:
            self.stats['ack_rx'] += 1
            if self.pending is not None and frm['ack'] == self.pending['seq']:
                done = self.pending
                self.pending = None
                self.tx_seq = (self.tx_seq + 1) & 0xFF
                self._on_acked(out, done)
                self._pump(out, now)
            return out

        self.stats['rx_payload_bytes'] += len(frm['payload'])

        # Any data-bearing frame is acknowledged immediately, duplicates
        # included - a duplicate means our previous ACK was lost.
        self._tx(out, build(FT_ACK, frm['src'], self.my_addr, 0, frm['seq'],
                            b'', 0, frm['seq']), now)
        self.stats['ack_tx'] += 1
        if self.last_rx_seq.get(frm['src']) == frm['seq']:
            self.stats['rx_dup'] += 1
            return out
        self.last_rx_seq[frm['src']] = frm['seq']
        self._deliver(out, frm, now)
        self._pump(out, now)
        return out

    def _on_acked(self, out, done):
        """One outstanding frame was acknowledged by the peer."""
        if done['mid'] is None:
            return
        self._evt(out, e='tx_state', mid=done['mid'], state='acked',
                  tries=done['tries'], frag=done['frag'],
                  queued=len(self.txq))
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
            if (self.rx_file is not None and self.rx_file['name'] == name
                    and self.rx_file['total'] == total):
                return          # repair round for a transfer already in progress
            self.rx_file = {'src': src, 'size': size, 'total': total,
                            'name': name, 'frags': {}, 'meta': meta,
                            'started': now}
            out.append(('log', 'incoming file %s from %d: %d bytes, %d fragments'
                        % (name, src, size, total)))
            self._evt(out, e='rx_file_start', src=src, name=name, size=size,
                      total=total, meta=meta)
        elif ftype == FT_FILE_DATA:
            if self.rx_file is None or len(payload) < 2:
                return
            idx = struct.unpack('!H', payload[:2])[0]
            self.rx_file['frags'][idx] = payload[2:]
            got, total = len(self.rx_file['frags']), self.rx_file['total']
            self._evt(out, e='rx_file_progress', name=self.rx_file['name'],
                      got=got, total=total)
            if total and got % max(1, total // 10) == 0:
                out.append(('log', '  file %d/%d fragments' % (got, total)))
        elif ftype == FT_FILE_END:
            self._finish_file(out, payload)
        elif ftype == FT_FILE_NACK:
            self._on_nack(out, payload)
        elif ftype == FT_FILE_DONE:
            self._on_done(out, payload)

    def _finish_file(self, out, payload):
        meta = self.rx_file
        if meta is None:
            return
        missing = [i for i in range(meta['total']) if i not in meta['frags']]
        if missing:
            # Tell the sender exactly what is missing and keep what we have.
            chunk = missing[:MAX_NACK_INDICES]
            self._enqueue(FT_FILE_NACK,
                          b''.join(struct.pack('!H', i) for i in chunk),
                          'file-nack')
            out.append(('log', 'file %s: %d of %d fragments missing, asking '
                               'the sender to resend %d of them'
                        % (meta['name'], len(missing), meta['total'],
                           len(chunk))))
            self._evt(out, e='rx_file_repair', name=meta['name'],
                      missing=len(missing), asked=len(chunk))
            return
        self.rx_file = None
        data = b''.join(meta['frags'][i] for i in range(meta['total']))
        want = struct.unpack('!I', payload[:4])[0] if len(payload) >= 4 else None
        got = zlib.crc32(data) & 0xFFFFFFFF
        dest = os.path.join(self.rx_dir, 'rx_' + meta['name'])
        try:
            with open(dest, 'wb') as fh:
                fh.write(data)
        except OSError as exc:
            out.append(('log', 'could not write %s: %s' % (dest, exc)))
            dest = None
        ok = (want is None or want == got)
        self._enqueue(FT_FILE_DONE, struct.pack('!BI', 0 if ok else 1, got),
                      'file-done')
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
        """Receiver confirmed the file. This is the sender's end-to-end result."""
        tf = self.tx_file
        name = tf['name'] if tf else 'file'
        mid = tf['mid'] if tf else None
        self.tx_file = None
        status = payload[0] if payload else 1
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
        self.txq = deque(item for item in self.txq
                         if not item['label'].startswith('file'))

    # ------------------------------------------------------------------ tick
    def on_tick(self, now):
        """Called periodically. Drives the retransmit timer and TX filler."""
        out = []
        pending = self.pending
        if pending is not None and now >= pending['deadline']:
            if pending['tries'] > self.max_retries:
                self.stats['dropped'] += 1
                label = pending['label']
                self.pending = None
                self.tx_seq = (self.tx_seq + 1) & 0xFF
                if pending['mid'] is not None:
                    self._evt(out, e='tx_state', mid=pending['mid'],
                              state='failed', tries=pending['tries'],
                              frag=pending['frag'], queued=len(self.txq))
                if label.startswith('file-frag'):
                    # Recoverable: the receiver will name it in its NACK.
                    # Counted rather than logged, so one bad patch of channel
                    # does not bury the console.
                    self.stats['frag_lost'] += 1
                elif label == 'file-start' and self.tx_file is not None:
                    # The very first frame is the one that gives the receiver
                    # its file context, so losing it makes every fragment
                    # behind it undeliverable. Restart the transfer rather than
                    # abandon it - the link was simply down at that moment.
                    self._restart_file(out)
                else:
                    out.append(('log', 'dropped after %d attempts: %s'
                                % (pending['tries'], label)))
            else:
                pending['tries'] += 1
                self.stats['retx'] += 1
                self._tx(out, build(pending['ftype'], self.peer_addr,
                                    self.my_addr, pending['seq'], 0,
                                    pending['payload'], FLAG_RETRY,
                                    pending['seq'] + 37 * pending['tries']), now)
                pending['deadline'] = self.busy_until + self.ack_timeout
                if pending['mid'] is not None:
                    self._evt(out, e='tx_state', mid=pending['mid'],
                              state='retry', tries=pending['tries'],
                              frag=pending['frag'], queued=len(self.txq))
        self._pump(out, now)

        # Keep the Pluto TX buffer fed so the carrier is continuous and the
        # far end keeps timing/carrier lock. Emitting only when the modelled
        # backlog runs low bounds the queue instead of flooding it.
        if self.queue_ahead > 0 and (self.busy_until - now) < self.queue_ahead:
            self.stats['idle_tx'] += 1
            self._idle_off = (self._idle_off + 1) & 0xFF
            self._tx(out, build(FT_IDLE, BROADCAST, self.my_addr, 0, 0,
                                b'', 0, self._idle_off), now)

        # Everything queued has gone out; the transfer is now waiting on the
        # peer's verdict. Without this the sender simply falls silent and never
        # learns whether the file arrived.
        tf = self.tx_file
        if tf is not None and not self.txq and self.pending is None:
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

        if self.peer_up and self.peer_seen and (now - self.peer_seen) > 3.0:
            self.peer_up = False
            out.append(('log', 'peer unreachable (no valid frame for 3 s)'))
            self._evt(out, e='peer', up=False, addr=self.peer_addr)
        return out
