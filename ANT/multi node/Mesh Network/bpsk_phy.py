"""Physical-layer helpers for the r6.1 BPSK mesh node. numpy only.

Nothing here imports GNU Radio, so every piece is unit-tested on its own
(test_phy.py) and shared by the flowgraph's embedded blocks, the simulator
and the bench:

  frame format   unchanged on air since r5:
                   preamble (C0 AF ...) | access code (64 bits) | len | len |
                   link frame (len bytes) | postamble (C0 AF ...)
                 len is a 16-bit byte count, sent twice, MSB first.
  Deframer       finds frames in the hard-decision bit stream of one channel.
                 It never waits for data it does not have, rejects any header
                 whose length is implausible (< MIN_FRAME or > MAX_FRAME) at
                 once, and keeps searching for access codes INSIDE a frame it
                 is collecting, so a bad header that still looks plausible
                 costs at most that one frame. This replaces
                 correlate_access_code_bb_ts -> repack_bits_bb ->
                 tagged_stream_to_pdu, which on GNU Radio 3.10.12 waited for
                 up to 65535 bytes after a corrupted header.
  TxFramer       turns link frames into a continuous symbol stream: frames
                 when there are frames, a PRBS-15 filler when there are not.
                 The carrier is continuous without the link layer having to
                 produce ~200 IDLE frames a second, and the transmit backlog
                 is exact and observable instead of modelled.
  channel_plan   the one-sided channel plan and how it maps onto the
                 polyphase channelizer.
  estimate_cfo   carrier offset of one BPSK channel from a snapshot (the
                 squared signal has a line at twice the offset); the node
                 uses it to steer a rotator, replacing four FLLs.
  measures       EVM/SNR, level, ADC peak and clipping, spectrum.
  Afc            the per-channel carrier-offset loop around estimate_cfo.
  count_ou       counts gr-iio's overflow/underflow marks in its stderr text.

The gain staging law is in bpsk_gain.py (standard library only: the
application layer uses it without numpy).
"""

import math
import struct
from collections import deque

import numpy as np

# ---------------------------------------------------------------- framing
# digital.packet_utils.default_access_code; header_format_default puts it in
# front of the length, sent twice.
ACCESS_CODE = ('1010110011011101101001001110001011110010100011000010000011111100')
AC_BITS = np.array([int(c) for c in ACCESS_CODE], dtype=np.uint8)
AC_BYTES = np.packbits(AC_BITS).tobytes()
HEADER_BITS = 64 + 32
CODE_THRESHOLD = 2            # bit errors tolerated in the access code (as r6)
PRE_PATTERN = b'\xc0\xaf'     # preamble / postamble byte pattern (as r5/r6)

# Link-frame length limits, bytes (scrambler byte + header + payload + CRC).
# The longest frame the link layer builds is a sealed 256-byte fragment, 304
# bytes; a beacon is at most ~140. bpsk_link refuses to build anything longer
# than MAX_FRAME, so a header announcing more is corrupt by definition.
MIN_FRAME = 13
MAX_FRAME = 600

# The access code split in three: with at most two bit errors one of the
# three parts is intact, so an exact search for the parts finds every
# candidate (bytes.find, fast) and only candidates are checked bit by bit.
_SPLITS = ((0, 22), (22, 43), (43, 64))
_PARTS = tuple((a, AC_BITS[a:b].tobytes()) for a, b in _SPLITS)


def frame_bytes(payload, preamble_size=64, postamble_size=64):
    """The bytes the modulator sends for one link frame."""
    n = len(payload)
    if not MIN_FRAME <= n <= MAX_FRAME:
        raise ValueError('link frame of %d bytes (limits %d..%d)'
                         % (n, MIN_FRAME, MAX_FRAME))
    pre = (PRE_PATTERN * (preamble_size // 2 + 1))[:preamble_size]
    post = (PRE_PATTERN * (postamble_size // 2 + 1))[:postamble_size]
    return pre + AC_BYTES + struct.pack('!HH', n, n) + bytes(payload) + post


def frame_bits(payload, preamble_size=64, postamble_size=64):
    return np.unpackbits(np.frombuffer(
        frame_bytes(payload, preamble_size, postamble_size), dtype=np.uint8))


def _lfsr(taps, n, seed=1):
    deg = max(taps)
    reg = [(seed >> i) & 1 for i in range(deg)]
    out = np.zeros(n, dtype=np.uint8)
    for i in range(n):
        b = 0
        for t in taps:
            b ^= reg[t - 1]
        out[i] = reg[-1]
        reg = [b] + reg[:-1]
    return out


# PRBS-15 (x^15 + x^11 + 1). Checked exhaustively (test_phy.py): every 64-bit
# window of it, and of it running into a preamble or out of a postamble, is
# at least 15 bits away from the access code - the deframer allows 2.
PRBS_TAPS = (15, 11)
PRBS = _lfsr(PRBS_TAPS, (1 << 15) - 1)


class Deframer(object):
    """Frames out of one channel's bit stream (values 0/1, one per byte)."""

    def __init__(self, max_len=MAX_FRAME, min_len=MIN_FRAME,
                 threshold=CODE_THRESHOLD):
        self.max_len = int(max_len)
        self.min_len = int(min_len)
        self.threshold = int(threshold)
        self.buf = np.zeros(0, dtype=np.uint8)
        self.base = 0            # absolute bit index of buf[0]
        self.scanned = 0         # every start below this has been examined
        self.pending = []        # [start, length or None]
        self.stats = dict(bits=0, hits=0, frames=0, hdr_mismatch=0,
                          hdr_bad_len=0, max_pending=0)

    def feed(self, bits):
        """Add bits; return the list of complete link frames (bytes)."""
        bits = np.asarray(bits, dtype=np.uint8)
        self.stats['bits'] += len(bits)
        self.buf = np.concatenate((self.buf, bits)) if len(self.buf) else \
            bits.copy()
        end = self.base + len(self.buf)
        # 1. candidate access codes starting in [scanned, end - 64]
        lo, hi = self.scanned, end - 63
        if hi > lo:
            raw = self.buf[lo - self.base:hi - self.base + 63].tobytes()
            cands = set()
            for off, part in _PARTS:
                i = raw.find(part)
                while i != -1:
                    s = i - off
                    if 0 <= s < hi - lo:
                        cands.add(s)
                    i = raw.find(part, i + 1)
            for s in sorted(cands):
                a = lo + s - self.base
                if np.count_nonzero(self.buf[a:a + 64] != AC_BITS) \
                        <= self.threshold:
                    self.pending.append([lo + s, None])
                    self.stats['hits'] += 1
            self.scanned = hi
        # 2. headers, then payloads, of every pending hit
        frames, keep = [], []
        for item in self.pending:
            start, length = item
            if length is None:
                if start + HEADER_BITS > end:
                    keep.append(item)
                    continue
                a = start + 64 - self.base
                l0, l1 = struct.unpack('!HH', np.packbits(
                    self.buf[a:a + 32]).tobytes())
                if l0 != l1:
                    self.stats['hdr_mismatch'] += 1
                    continue
                if not self.min_len <= l0 <= self.max_len:
                    self.stats['hdr_bad_len'] += 1
                    continue
                item[1] = length = l0
            stop = start + HEADER_BITS + 8 * length
            if stop > end:
                keep.append(item)
                continue
            a = start + HEADER_BITS - self.base
            frames.append(np.packbits(self.buf[a:a + 8 * length]).tobytes())
            self.stats['frames'] += 1
        self.pending = keep
        self.stats['max_pending'] = max(self.stats['max_pending'], len(keep))
        # 3. forget what no pending frame and no future search needs
        keep_from = min([p[0] for p in keep] + [self.scanned])
        drop = keep_from - self.base
        if drop > 0:
            self.buf = self.buf[drop:]
            self.base = keep_from
        return frames

    @property
    def held_bits(self):
        return len(self.buf)


class TxFramer(object):
    """Link frames in, a continuous stream of BPSK symbols out.

    Pulled by the radio: next_symbols(n) is called for exactly as many
    symbols as the DAC is about to need, so the queue here IS the transmit
    backlog - there is no unbounded message queue in front of the modulator
    any more (r6.0.1: pdu_to_tagged_stream dropped frames once 100 waited).
    Differential encoding as digital.generic_mod(differential=True): the
    transmitted bit is the running XOR of the data bits, 0 -> -1, 1 -> +1.
    """

    def __init__(self, preamble_size=64, postamble_size=64, sym_rate=250e3,
                 max_backlog=0.5):
        self.preamble_size = int(preamble_size)
        self.postamble_size = int(postamble_size)
        self.sym_rate = float(sym_rate)
        self.max_bits = int(max_backlog * self.sym_rate)
        self.queue = deque()
        self.queued_bits = 0
        self.cur = None
        self.pos = 0
        self.ppos = 0
        self.state = 0
        self.stats = dict(accepted=0, sent=0, dropped=0, data_bits=0,
                          fill_bits=0, rejected=0)

    def push(self, frame):
        """Queue one link frame. False if refused (too long or backlog full)."""
        try:
            bits = frame_bits(frame, self.preamble_size, self.postamble_size)
        except ValueError:
            self.stats['rejected'] += 1
            return False
        if self.backlog_bits() + len(bits) > self.max_bits:
            self.stats['dropped'] += 1
            return False
        self.queue.append(bits)
        self.queued_bits += len(bits)
        self.stats['accepted'] += 1
        return True

    def backlog_bits(self):
        cur = (len(self.cur) - self.pos) if self.cur is not None else 0
        return self.queued_bits + cur

    def backlog(self):
        """Seconds of frames not yet handed to the modulator."""
        return self.backlog_bits() / self.sym_rate

    def next_bits(self, n):
        parts, need = [], int(n)
        while need > 0:
            if self.cur is None and self.queue:
                self.cur = self.queue.popleft()
                self.queued_bits -= len(self.cur)
                self.pos = 0
            if self.cur is not None:
                take = min(need, len(self.cur) - self.pos)
                parts.append(self.cur[self.pos:self.pos + take])
                self.pos += take
                need -= take
                self.stats['data_bits'] += take
                if self.pos >= len(self.cur):
                    self.cur = None
                    self.stats['sent'] += 1
                continue
            take = min(need, len(PRBS) - self.ppos)
            parts.append(PRBS[self.ppos:self.ppos + take])
            self.ppos = (self.ppos + take) % len(PRBS)
            need -= take
            self.stats['fill_bits'] += take
        return np.concatenate(parts) if len(parts) > 1 else parts[0]

    def next_symbols(self, n):
        bits = self.next_bits(n)
        enc = (np.cumsum(bits, dtype=np.int64) + self.state) & 1
        self.state = int(enc[-1])
        return (2.0 * enc - 1.0).astype(np.complex64)


def diff_decode(symbols, last=None):
    """Reference receiver for tests: hard decisions, then differential."""
    hard = (np.real(symbols) > 0).astype(np.uint8)
    prev = np.concatenate(([hard[0] if last is None else last], hard[:-1]))
    return hard ^ prev


# ------------------------------------------------------------ channel plan
def channel_plan(radio_rate=6e6, ch_first=450e3, ch_spacing=600e3,
                 num_channels=4, sym_rate=250e3, sps=4):
    """Offsets of the channels and the channelizer that receives them.

    The channelizer has nbins = radio_rate / ch_spacing bins centred on
    multiples of ch_spacing, so the whole band is first rotated by `shift`
    to put channel 0 on a bin. Output rate per channel sps * sym_rate
    (oversampling ratio nbins / decim).
    """
    nbins = radio_rate / ch_spacing
    if abs(nbins - round(nbins)) > 1e-9:
        raise ValueError('radio_rate must be a multiple of ch_spacing')
    nbins = int(round(nbins))
    out_rate = sps * sym_rate
    decim = radio_rate / out_rate
    if abs(decim - round(decim)) > 1e-9 or not 1 <= round(decim) <= nbins:
        raise ValueError('radio_rate / (sps * sym_rate) must be an integer '
                         'between 1 and the number of bins')
    decim = int(round(decim))
    shift = math.ceil(ch_first / ch_spacing - 1e-9) * ch_spacing - ch_first
    offsets = [ch_first + k * ch_spacing for k in range(num_channels)]
    bins = [int(round((f + shift) / ch_spacing)) % nbins for f in offsets]
    if len(set(bins)) != len(bins):
        raise ValueError('two channels share a channelizer bin')
    return {'offsets': offsets, 'bins': bins, 'shift': shift, 'nbins': nbins,
            'decim': decim, 'osr': float(nbins) / decim, 'out_rate': out_rate,
            'tx_interp': int(round(radio_rate / sym_rate)),
            'radio_rate': radio_rate, 'spacing': ch_spacing}


def plan_report(plan, occupied=337.5e3, nyquist_margin=0.85):
    """Where every channel's IQ image and the LO leakage land."""
    half = occupied / 2.0
    offs = plan['offsets']
    images = []
    for k, f in enumerate(offs):
        hit = [j for j, g in enumerate(offs) if abs(-f - g) < occupied]
        images.append(hit)
    dc_guard = min(abs(f) for f in offs) - half
    edge = max(abs(f) for f in offs) + half
    return {'image_hits': images, 'image_free': not any(images),
            'dc_guard': dc_guard, 'band_edge': edge,
            'edge_fraction': edge / (plan['radio_rate'] / 2.0),
            'inside_nyquist': edge < nyquist_margin * plan['radio_rate'] / 2}


# ------------------------------------------------------------ measurements
def estimate_cfo(x, fs, min_ratio_db=15.0):
    """Carrier offset (Hz) of a BPSK signal: the squared signal has a line at
    twice the offset. Returns (cfo, line-to-median ratio dB) or (None, r)."""
    x = np.asarray(x, dtype=np.complex64)
    n = len(x)
    if n < 256:
        return None, 0.0
    y = (x * x) * np.hanning(n).astype(np.float32)
    nfft = 1 << int(math.ceil(math.log2(2 * n)))
    mag = np.abs(np.fft.fft(y, nfft))
    k = int(np.argmax(mag))
    floor = float(np.median(mag)) + 1e-20
    ratio = 20.0 * math.log10(mag[k] / floor + 1e-20)
    if ratio < min_ratio_db:
        return None, ratio
    a, b, c = mag[(k - 1) % nfft], mag[k], mag[(k + 1) % nfft]
    den = a - 2 * b + c
    delta = 0.5 * (a - c) / den if den else 0.0
    kk = k + delta
    if kk > nfft / 2:
        kk -= nfft
    return 0.5 * kk * fs / nfft, ratio


def evm_snr(sym):
    """(snr_db, evm) of recovered BPSK symbols, or (None, None)."""
    sym = np.asarray(sym, dtype=np.complex64)
    if len(sym) < 64:
        return None, None
    amp = float(np.mean(np.abs(sym.real)))
    if amp <= 1e-6:
        return None, None
    err = sym - np.sign(sym.real) * amp
    evm = float(np.sqrt(np.mean(np.abs(err) ** 2)) / amp)
    evm = min(max(evm, 1e-3), 10.0)
    return -20.0 * math.log10(evm), evm


def rrc_taps(sps=4, alpha=0.35, span=11):
    """Root-raised-cosine taps (unity DC gain), span symbols long."""
    n = span * sps + 1
    t = (np.arange(n) - (n - 1) / 2.0) / float(sps)
    h = np.zeros(n)
    for i, ti in enumerate(t):
        if abs(ti) < 1e-12:
            h[i] = 1.0 - alpha + 4 * alpha / math.pi
        elif alpha and abs(abs(ti) - 1.0 / (4 * alpha)) < 1e-9:
            h[i] = (alpha / math.sqrt(2)) * (
                (1 + 2 / math.pi) * math.sin(math.pi / (4 * alpha))
                + (1 - 2 / math.pi) * math.cos(math.pi / (4 * alpha)))
        else:
            h[i] = (math.sin(math.pi * ti * (1 - alpha))
                    + 4 * alpha * ti * math.cos(math.pi * ti * (1 + alpha))) \
                / (math.pi * ti * (1 - (4 * alpha * ti) ** 2))
    return (h / h.sum()).astype(np.float32)


_RRC4 = None


def inband_level_db(x, sps=4):
    """Level of a channel as its matched filter sees it (dBFS when x is
    the unity-gain channel filter output): what the decoder gets, without
    the neighbours' energy that the channel filter's transition band lets
    through (r6.1: the wideband reading showed the own transmitter 56 dB
    down in the adjacent channels; after the matched filter it is 69)."""
    global _RRC4
    x = np.asarray(x, dtype=np.complex64)
    if len(x) < 64:
        return None
    if _RRC4 is None or len(_RRC4) != 11 * sps + 1:
        _RRC4 = rrc_taps(sps)
    y = np.convolve(x, _RRC4, mode='valid')
    return 10.0 * math.log10(float(np.mean(np.abs(y) ** 2)) + 1e-20)


def level_db(x):
    x = np.asarray(x, dtype=np.complex64)
    if not len(x):
        return None
    return 10.0 * math.log10(float(np.mean(np.abs(x) ** 2)) + 1e-20)


CLIP_LEVEL = 0.95


def adc_stats(x):
    """peak (dBFS), rms (dBFS), clipped fraction of raw receiver samples."""
    x = np.asarray(x, dtype=np.complex64)
    if not len(x):
        return None
    mag = np.maximum(np.abs(x.real), np.abs(x.imag))
    peak = float(mag.max())
    rms = float(np.sqrt(np.mean(np.abs(x) ** 2)))
    return {'peak_dbfs': 20.0 * math.log10(peak + 1e-9),
            'rms_dbfs': 20.0 * math.log10(rms + 1e-9),
            'clip': float(np.count_nonzero(mag >= CLIP_LEVEL)) / len(x)}


def spectrum_db(x, nbins=256):
    """Averaged power spectrum (dB, fftshifted) of a snapshot, for the UI."""
    x = np.asarray(x, dtype=np.complex64)
    segs = len(x) // nbins
    if segs < 1:
        return None
    w = np.hanning(nbins).astype(np.float32)
    s = x[:segs * nbins].reshape(segs, nbins) * w
    p = np.mean(np.abs(np.fft.fft(s, axis=1)) ** 2, axis=0) / (np.sum(w) ** 2)
    return [round(float(v), 1)
            for v in 10 * np.log10(np.fft.fftshift(p) + 1e-20)]


class Afc(object):
    """Per-channel carrier-offset loop: estimate_cfo on snapshots taken after
    the channel's rotator, the rotator moved by the residual.

    Replaces the four FLL band-edge blocks (0.54 of the 1.5 cores the r6.0.1
    receiver needed, and in a test with +-18..23 kHz offsets they did not
    pull in at all). The Costas loop tracks what is left; the AFC only has
    to bring a new transmitter (crystals up to +-25 ppm: +-23 kHz each, up
    to 45 kHz between two nodes at 910 MHz) inside its pull-in range and
    follow slow drift.

    An update needs two consecutive estimates that agree (a spur or a noise
    peak does not repeat), a residual above `deadband`, a result inside
    +-limit (an estimate that would put the correction outside is refused,
    not clipped), and `holdoff` seconds since the last update (a snapshot
    taken before it cannot cause a second one). A channel with no BPSK line
    for `forget` seconds drops its correction: the next node to claim it has
    its own crystal. Limits are fractions of the channel rate, so the loop
    behaves the same on the slowed-down bench.
    """

    def __init__(self, fs, deadband=1.5e-4, limit=0.06, agree=1.5e-3,
                 holdoff=0.45, forget=10.0, min_ratio_db=15.0):
        self.fs = float(fs)
        self.deadband = deadband * self.fs
        self.limit = limit * self.fs
        self.agree = agree * self.fs
        self.holdoff = float(holdoff)
        self.forget = float(forget)
        self.min_ratio_db = float(min_ratio_db)
        self.correction = 0.0      # Hz removed by the rotator
        self.residual = None
        self.ratio = 0.0
        self._next = 0.0
        self._prev = None
        self._seen = None
        self.updates = 0
        self.resets = 0

    def update(self, now, x):
        """Returns the new rotator phase increment (rad/sample) or None."""
        res, ratio = estimate_cfo(x, self.fs, self.min_ratio_db)
        self.ratio = ratio
        self.residual = res
        prev, self._prev = self._prev, res
        if res is None:
            if self._seen is not None and now - self._seen > self.forget \
                    and self.correction:
                self.correction = 0.0
                self.resets += 1
                self._seen = None
                return self.phase_inc()
            return None
        self._seen = now
        if prev is None or abs(res - prev) > self.agree:
            return None
        if now < self._next or abs(res) < self.deadband:
            return None
        new = self.correction + res
        if abs(new) > self.limit:
            return None
        self.correction = new
        self._prev = None          # the next pair starts after the move
        self._next = now + self.holdoff
        self.updates += 1
        return self.phase_inc()

    def phase_inc(self):
        return -2.0 * math.pi * self.correction / self.fs

    @property
    def cfo(self):
        """Best estimate of the channel's total carrier offset, Hz (a
        residual the loop would refuse - a spur, a neighbour's leakage on an
        empty channel - is not part of it)."""
        r = self.residual
        if r is None or abs(self.correction + r) > self.limit:
            r = 0.0
        return self.correction + r


def count_ou(text, state=None):
    """Count gr-iio's overflow 'O' and underflow 'U' marks in text written
    to stderr. gr-iio prints them with fprintf(stderr, "O") - at most one a
    second per direction, no newline - so they can sit between other lines.
    A run of O/U characters counts when it is not part of a word (the
    characters on either side are not letters or digits). state carries
    the character before `text` across calls. Returns (o, u, state)."""
    prev = (state or {}).get('prev', '\n')
    in_run = bool((state or {}).get('run'))
    o = u = 0
    i, n = 0, len(text)
    counted_end = in_run
    while i < n:
        c = text[i]
        if c in 'OU':
            j = i
            while j < n and text[j] in 'OU':
                j += 1
            before = prev if i == 0 else text[i - 1]
            after = text[j] if j < n else None
            # gr-iio writes one mark a second with no newline, so a run
            # grows across reads: 'O' after a counted 'O' is another mark
            cont = i == 0 and in_run
            if (cont or not before.isalnum()) and (
                    after is None or not after.isalnum()):
                run = text[i:j]
                o += run.count('O')
                u += run.count('U')
                counted_end = j == n
            else:
                counted_end = False
            i = j
            continue
        counted_end = False
        i += 1
    return o, u, {'prev': text[-1] if text else prev,
                  'run': counted_end if text else in_run}
