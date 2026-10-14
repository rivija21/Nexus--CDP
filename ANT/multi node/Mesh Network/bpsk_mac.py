"""Medium access for the multi-node BPSK network (r6): FDMA with self-assigned
channels and self-assigned addresses.

Every node transmits continuously on ONE channel of K and receives all K at
once, so there is no contention: a node never waits for the medium and ACKs
flow on the receiver's own channel while data flows on the sender's. What has
to be managed is only WHO owns WHICH channel, and which short address each
node answers to. Both are settled without a coordinator:

Addresses
    address = 1 + SHA-256("bpsk-addr" | identity | n) mod 254, n = 0, 1, ...
    identity is the node's public key (or a random id with security off), so
    an address is stable across restarts. When two identities map to the same
    address, the one that is still listening (has not transmitted yet) moves;
    between two active nodes, the larger identity moves. n is persisted.

Channels
    LISTEN   transmit nothing for a random 2-4 s; note every channel on which
             a valid frame from another node is decoded
    CLAIM    pick a free channel at random and start transmitting on it
    ACTIVE   beacon once a second
    SILENT   now and then stop transmitting and watch our own channel for
             0.35-0.5 s, starting once our own signal has drained out of the
             transmit and receive buffers (the time our beacons take to come
             back to us, measured continuously): three times at 1.5-5 s intervals right after claiming,
             then every 20-40 s. A frame from anyone else on it - valid, or
             several failing CRC - means two nodes claimed the same channel:
             go back to LISTEN and pick another.
    DEAF     every beacon lists the addresses its sender has decoded in the
             last 3 s. If every node we hear has been beaconing for 4 s
             without listing us, our transmissions are not getting through -
             a collision the silence check missed (three nodes on one
             channel, or silences that happened to coincide): pick another
             channel. Backs off if it keeps happening (a node simply out of
             range must not hop for ever).

    A channel counts as busy when, within the last 1.5 s, a frame from
    another node was decoded on it, three frames on it failed their CRC, or
    it carried ENERGY: two readings in a row 10 dB over the quietest channel
    of the same reading and within 30 dB of the strongest. The second condition keeps a strong neighbour's
    image (IQ imbalance puts it on the mirror channel, 40-50 dB down) and its
    adjacent-channel leakage from counting. Energy is what exposes two nodes
    of similar strength on one channel: neither decodes, but the power is
    there. Levels are only used while this node is not transmitting (LISTEN,
    FULL, SILENT) - its own carrier would dominate every reading.
    Every active node beacons at 1 Hz and keeps the carrier up with idle
    filler, so an occupied channel is never quiet.

Beacon (link frame type FT_BEACON, broadcast, never sealed - it carries only
public information; a key it announces is trusted only after the operator
approves it, and every session still proves possession of the private key):

    byte  0      version
    byte  1      channel index
    byte  2      n (address derivation counter)
    byte  3      flags: bit 0 = identity is an X25519 public key
    bytes 4-35   identity
    byte  36     nickname length N
    bytes 37..   nickname, UTF-8
    next byte    M, then M addresses this node decoded in the last 3 s

r6.1 appends a link report after the neighbour list. r6.0 nodes ignore
trailing bytes, so BEACON_VERSION stays 1 and the beacon remains readable by
every r6.x node (unpack_beacon returns ext=None for an r6.0 beacon):

    byte  0      EXT_TAG 0xE6
    byte  1      extension version (1)
    byte  2      flags: bit 0 gain control automatic, bit 1 receiver
                 overloaded (ADC clipping), bit 2 receiver impaired (a
                 receive channel stalled, or the host is falling behind)
    byte  3      ADC peak, dBFS (int8)
    byte  4      RX gain, dB      byte 5   TX attenuation, dB
    byte  6      K, then K x int8: level of each channel as this node
                 receives it, dBFS (-128 = no reading)
    next byte    M2, then M2 x (address, SNR int8 in 0.5 dB, level int8 dBFS):
                 how well this node hears each neighbour (-128 = unknown) -
                 the other half of every link, which the neighbour's own
                 receiver cannot measure ("X hears me at b dB").
"""

import hashlib
import random
import struct

BEACON_VERSION = 1
EXT_TAG = 0xE6
EXT_VERSION = 1
EF_AUTO = 0x01
EF_OVERLOAD = 0x02
EF_IMPAIRED = 0x04
NO_READING = -128
BEACON_PERIOD = 1.0
BF_KEY = 0x01

NUM_CHANNELS = 4
CH_SPACING = 600e3

LISTEN_TIME = (2.0, 4.0)
RELISTEN_TIME = (0.3, 1.5)
FIRST_CHECK = (1.0, 3.0)
EARLY_CHECKS = 3         # checks at EARLY_EVERY intervals after each claim
EARLY_EVERY = (1.5, 5.0)
CHECK_EVERY = (20.0, 40.0)
SILENCE = 0.5            # seconds without transmitting during a check
DETECT_FROM = 0.15       # ignore frames still in the pipeline before this
ECHO_GUARD = 0.1         # ... and this long after our own last echo, or
                         # after the measured echo latency
SILENCE_CLEAN = 0.35     # observe at least this long once the echo is gone
SILENCE_MAX = 2.0        # but never stay silent longer than this ...
SILENCE_CAP = 8.0        # r6.1: ... unless the echo needs longer (a slow
                         # host): then echo + guard + SILENCE_CLEAN, up to this
BUSY_HOLD = 1.5          # a channel is busy this long after a frame on it
BAD_BUSY = 3             # CRC failures within BUSY_HOLD that make it busy
BAD_COLLIDE = 2          # CRC failures during a check that mean collision
FULL_RETRY = 2.0         # re-evaluate this often when every channel is taken
NEIGHBOUR_WINDOW = 3.0   # a beacon lists nodes decoded this recently
MAX_NEIGHBOURS = 32
DEAF_AFTER = 4.0         # unheard by every beaconing node for this long
DEAF_MAX = 60.0          # back-off limit for repeated deafness
ENERGY_MARGIN = 10.0     # dB over the quietest channel ...
ENERGY_SPAN = 30.0       # ... and within this of the strongest
ENERGY_COLLIDE = 2       # consecutive readings over the threshold
LOOK_MAX = 8.0           # r6.1: how much longer a check of a node that hears
                         # nobody may wait to see its channel
OWN_TAIL = 12.0          # r6.1: during a check, a reading on the own channel
                         # within this of the own transmitter's level is still
                         # the own signal draining out of the buffers


def channel_offset(k, num_channels=NUM_CHANNELS, spacing=CH_SPACING,
                   first=None):
    """Baseband offset of channel k.

    first=None: the r6.0 symmetric plan, (k - (K-1)/2) * spacing - with an
    even K no channel sits at DC, but channel k and K-1-k are each other's
    IQ image. first=f0: the r6.1 one-sided plan, f0 + k * spacing - every
    channel on one side of the LO, so every image (own transmitter's
    included) lands on the empty other side.
    """
    if first is None:
        return (k - (num_channels - 1) / 2.0) * spacing
    return first + k * spacing


def _i8(v, scale=1.0):
    if v is None:
        return NO_READING
    try:
        x = int(round(float(v) * scale))
    except (TypeError, ValueError):
        return NO_READING
    return max(-127, min(127, x))


def pack_ext(flags=0, adc_peak=None, rx_gain=0, tx_atten=0, levels=(),
             links=()):
    """r6.1 link report. links: (address, snr_db, level_dbfs) tuples."""
    lv = list(levels)[:16]
    lk = list(links)[:MAX_NEIGHBOURS]
    out = bytes([EXT_TAG, EXT_VERSION, flags & 0xFF]) + struct.pack(
        '!bBB', _i8(adc_peak), max(0, min(255, int(rx_gain))),
        max(0, min(255, int(tx_atten))))
    out += bytes([len(lv)]) + struct.pack('!%db' % len(lv),
                                          *[_i8(x) for x in lv])
    out += bytes([len(lk)])
    for addr, snr, level in lk:
        out += struct.pack('!Bbb', int(addr) & 0xFF, _i8(snr, 2.0),
                           _i8(level))
    return out


def unpack_ext(raw):
    """Parse a link report; None if absent or malformed."""
    try:
        if len(raw) < 7 or raw[0] != EXT_TAG or raw[1] != EXT_VERSION:
            return None
        flags = raw[2]
        adc, rxg, txa = struct.unpack('!bBB', raw[3:6])
        k = raw[6]
        pos = 7
        lv = list(struct.unpack('!%db' % k, raw[pos:pos + k]))
        pos += k
        m = raw[pos]
        pos += 1
        links = {}
        for _ in range(m):
            addr, snr, level = struct.unpack('!Bbb', raw[pos:pos + 3])
            pos += 3
            links[addr] = {'snr': None if snr == NO_READING else snr / 2.0,
                           'level': None if level == NO_READING else level}
        return {'auto': bool(flags & EF_AUTO),
                'overloaded': bool(flags & EF_OVERLOAD),
                'impaired': bool(flags & EF_IMPAIRED),
                'adc_peak': None if adc == NO_READING else adc,
                'rx_gain': rxg, 'tx_atten': txa,
                'levels': [None if x == NO_READING else x for x in lv],
                'links': links}
    except (IndexError, struct.error):
        return None


def derive_address(identity, n=0):
    h = hashlib.sha256(b'bpsk-addr' + bytes(identity) + bytes([n & 0xFF]))
    return 1 + int.from_bytes(h.digest()[:4], 'big') % 254


def _trim_utf8(text, limit):
    """At most limit bytes of UTF-8, never cutting a character in half."""
    raw = (text or '').encode('utf-8', 'replace')[:limit]
    return raw.decode('utf-8', 'ignore').encode('utf-8')


def pack_beacon(channel, n, identity, nick, is_key, neighbours=(), ext=b''):
    nick_b = _trim_utf8(nick, 32)
    nb = bytes(a & 0xFF for a in list(neighbours)[:MAX_NEIGHBOURS])
    return (bytes([BEACON_VERSION, channel & 0xFF, n & 0xFF,
                   BF_KEY if is_key else 0])
            + bytes(identity)[:32].ljust(32, b'\x00')
            + bytes([len(nick_b)]) + nick_b + bytes([len(nb)]) + nb
            + bytes(ext or b''))


def unpack_beacon(payload):
    """Returns a dict, or None if the payload is not a valid beacon."""
    if len(payload) < 37 or payload[0] != BEACON_VERSION:
        return None
    nlen = payload[36]
    if len(payload) < 37 + nlen:
        return None
    rest = payload[37 + nlen:]
    neighbours = bytes(rest[1:1 + rest[0]]) if rest else b''
    tail = bytes(rest[1 + rest[0]:]) if rest else b''
    return {'channel': payload[1], 'n': payload[2],
            'is_key': bool(payload[3] & BF_KEY),
            'identity': bytes(payload[4:36]),
            'nick': payload[37:37 + nlen].decode('utf-8', 'replace'),
            'neighbours': list(neighbours),
            'ext': unpack_ext(tail) if tail else None}


class AddressManager(object):
    """This node's short address, derived from its identity."""

    def __init__(self, identity, n=0, fixed=None):
        self.identity = bytes(identity)
        self.n = int(n)
        self.fixed = fixed
        self.addr = int(fixed) if fixed else derive_address(self.identity,
                                                            self.n)
        self.known = {}          # address -> identity heard in beacons

    def _next_free(self):
        n = self.n
        while True:
            n += 1
            addr = derive_address(self.identity, n)
            other = self.known.get(addr)
            if other is None or other == self.identity:
                return n, addr

    def on_beacon(self, src, identity, active, now=None):
        """A beacon from src claiming identity. Returns an event or None.

        active: whether WE have started transmitting. A listening node always
        yields; between two active nodes the larger identity yields, so both
        reach the same decision independently.
        """
        identity = bytes(identity)
        self.known[src] = identity
        if self.fixed or src != self.addr or identity == self.identity:
            return None
        if active and self.identity < identity:
            return ('conflict', src)          # the other node will move
        old = self.addr
        self.n, self.addr = self._next_free()
        return ('readdress', old, self.addr)


class ChannelManager(object):
    """Decides when this node may transmit, and on which channel."""

    def __init__(self, num_channels=NUM_CHANNELS, fixed_channel=None,
                 rng=None):
        self.k = int(num_channels)
        self.rng = rng or random.Random()
        self.fixed = fixed_channel
        self.channel = fixed_channel
        self.state = 'active' if fixed_channel is not None else 'idle'
        self.last_heard = [-1e9] * self.k
        self.heard_src = [None] * self.k
        self.bad_times = [[] for _ in range(self.k)]
        self.levels = [None] * self.k
        self.floor = None
        self.energy_at = [-1e9] * self.k
        self._ehits = [0] * self.k
        self.own_last = -1e9
        self.own_level = None    # r6.1: own channel level while transmitting
        self.active_since = -1e9  # r6.1: transmitting (again) since
        self._learn_after = -1e9  # r6.1: no own-level learning before this
        self.echo = []           # recent own-beacon round trips, seconds
        self.claimed_at = None
        self.early_left = 0
        self.deaf_moves = 0
        self._move = None
        self.until = 0.0
        self.next_check = 0.0
        self.silent_from = 0.0
        self.collision = None
        self.avoid = set()
        self.claims = 0
        self.collisions = 0
        self.checks = 0
        # r6.1: whether a check actually saw the channel: ENERGY_COLLIDE
        # level readings of the own channel taken once the own signal had
        # drained (or decoded-frames mode on a lagging host). Its 'clear'
        # says so; only a check that saw the channel counts towards a link
        # fault. On the bench at 1/32 speed the readings come every 3.2 s,
        # a plain check saw nothing, and two nodes on one channel each
        # reported a link fault instead of parting.
        self._obs = 0
        self.unobserved = 0       # checks that ended without a reading
        self.energy_used = False  # level readings arrive at all (energy())
        # r6.1: set by the application while this node hears no neighbour
        # at all: it has no link a longer silence could hurt, and it may be
        # sharing its channel with a node it cannot decode (two nodes alone
        # on one channel decode nothing from each other), so its checks
        # last until they have seen the channel (LOOK_MAX at most)
        self.must_see = False
        # r6.1: extra silence for the next check, set by the application
        # (a deafness check that must outlast a second node's own check)
        self.extend_next = 0.0
        self.cur_extend = 0.0
        # r6.1: score(k) -> higher is better, or None. Set by the
        # application from the neighbours' link reports: a channel on which
        # the receivers see less interference gives every link more SNR.
        self.scorer = None
        self.last_scores = None

    # ------------------------------------------------------------ queries
    @property
    def tx_enabled(self):
        return self.state == 'active'

    def _bad_recent(self, k, now, since=None):
        lo = now - BUSY_HOLD if since is None else max(since, now - BUSY_HOLD)
        return sum(1 for t in self.bad_times[k] if t >= lo)

    def busy(self, now):
        return [now - self.last_heard[k] < BUSY_HOLD
                or now - self.energy_at[k] < BUSY_HOLD
                or self._bad_recent(k, now) >= BAD_BUSY
                for k in range(self.k)]

    def deaf_window(self):
        return min(DEAF_MAX, DEAF_AFTER * 2 ** max(0, self.deaf_moves - 2))

    def gain_changed(self, now, delta_db):
        """The RX gain moved by delta_db: every level reading moves with it,
        the own level included - but the readings still in the pipeline
        were taken before the change, so none is learnt from for an echo.
        (Bench: a 12 dB drop at start-up left the own level 12 dB too high,
        and the own signal's tail in the next check looked like a second
        transmitter.)"""
        if self.own_level is not None:
            self.own_level += float(delta_db)
        self._learn_after = now + (max(self.echo) + ECHO_GUARD
                                   if self.echo else DETECT_FROM)

    def check_length(self):
        """How long a plain check normally stays silent (for the
        application's timing): until the own signal has drained, then
        SILENCE_CLEAN of listening; SILENCE at least, silence_max at most."""
        tail = max(self.echo) + ECHO_GUARD if self.echo else DETECT_FROM
        return min(self.silence_max(),
                   max(SILENCE, max(DETECT_FROM, tail) + SILENCE_CLEAN))

    def snapshot(self, now):
        return {'state': self.state, 'channel': self.channel,
                'channels': self.k, 'busy': self.busy(now),
                'heard': list(self.heard_src), 'claims': self.claims,
                'levels': list(self.levels), 'floor': self.floor,
                'echo': max(self.echo) if self.echo else None,
                'lagging': self.slow,
                'checks_on_frames_only': self.lagging,
                'collisions': self.collisions, 'checks': self.checks,
                'next_check': (max(0.0, self.next_check - now)
                               if self.state == 'active' and self.fixed is None
                               else None)}

    # ------------------------------------------------------------ inputs
    def start(self, now):
        if self.fixed is None:
            self.state = 'listen'
            self.until = now + self.rng.uniform(*LISTEN_TIME)

    def heard(self, channel, src, now):
        """A valid frame from another node was decoded on channel.

        The caller filters out adjacent-channel leakage: a frame from a node
        whose beacon names a different channel is not reported here.
        """
        if channel is None or not 0 <= channel < self.k:
            return
        self.last_heard[channel] = now
        self.heard_src[channel] = src
        if channel != self.channel or self.fixed is not None:
            return
        if self.state == 'silent' and now >= self._clean_from():
            self.collision = src
        elif self.state == 'active':
            # Decoded on top of our own carrier: the other node is strong
            # enough to be heard through it, so it certainly shares the channel.
            self.collision = src
            self.state = 'silent'
            self.silent_from = now - SILENCE

    def bad(self, channel, now):
        """A frame on channel failed its CRC (overlapping transmissions)."""
        if channel is None or not 0 <= channel < self.k:
            return
        q = self.bad_times[channel]
        q.append(now)
        while q and q[0] < now - BUSY_HOLD:
            q.pop(0)
        if (self.state == 'silent' and channel == self.channel
                and self.fixed is None and not self.lagging
                and now >= self._clean_from()
                and self._bad_recent(channel, now, self._clean_from())
                >= BAD_COLLIDE):
            self.collision = 'garbled'

    def own_echo(self, now):
        """One of our own frames came back on our channel. During a check it
        means our transmission is still draining out of the buffers."""
        self.own_last = now

    def echo_latency(self, seconds):
        """One of our beacons came back after this long: how long our own
        signal keeps arriving after we stop."""
        if 0 <= seconds < 30.0:
            self.echo = (self.echo + [seconds])[-8:]

    def silence_max(self):
        """Longest a check stays silent. r6.1: long enough for the own
        signal to drain out of the buffers and then SILENCE_CLEAN of clean
        listening, up to SILENCE_CAP. r6.0 capped it at 2 s, so on a slow
        host (echo above 1.65 s) a check never saw a clean moment and fell
        back to decoded frames only - which, with the r6.1 PRBS filler
        instead of 200 IDLE frames a second, are too rare to catch a second
        transmitter (bench at 1/32: two nodes shared a channel for 10 min)."""
        if not self.echo:
            return SILENCE_MAX
        need = max(self.echo) + ECHO_GUARD + SILENCE_CLEAN + 0.2
        return min(SILENCE_CAP, max(SILENCE_MAX, need))

    @property
    def slow(self):
        """The own signal comes back later than a normal check lasts
        (r6.0's 2 s): the host lags the sample stream. Checks then last
        longer (silence_max), and the operator is told."""
        return bool(self.echo) and (max(self.echo) + ECHO_GUARD
                                    > SILENCE_MAX - SILENCE_CLEAN)

    @property
    def lagging(self):
        """Our own signal takes longer to come back than a check can wait:
        the host is not keeping up with the sample stream. Power and CRC
        readings during a check would then still show our own tail, so only
        frames decoded from other nodes (and deafness) are trusted."""
        return bool(self.echo) and (max(self.echo) + ECHO_GUARD
                                    > self.silence_max() - SILENCE_CLEAN)

    def _clean_from(self):
        tail = max(self.echo) + ECHO_GUARD if self.echo else DETECT_FROM
        return min(self.silent_from + self.silence_max() - SILENCE_CLEAN,
                   max(self.silent_from + max(DETECT_FROM, tail),
                       self.own_last + ECHO_GUARD))

    def energy(self, levels, now):
        """One level reading per channel (dB, None = no reading), measured
        at time now.

        Compared within the reading itself (quietest vs strongest channel),
        not against a remembered noise floor: a gain change or one odd
        reading cannot then make every channel look busy for minutes.
        """
        self.energy_used = True
        # The own level is learnt only from readings that can show the own
        # signal: taken after it can have come back, and clearly above the
        # quietest channel. Learnt from the empty channel (the first second
        # of a claim, or a reading older than the echo on a slow host), every
        # quiet reading in the next check looked like our own tail - and the
        # check, never seeing the channel, could not end.
        back = self.active_since + (max(self.echo) + ECHO_GUARD
                                    if self.echo else DETECT_FROM)
        heard = [lv for lv in levels if lv is not None]
        if self.state == 'active' and self.channel is not None \
                and now >= max(back, self._learn_after) and len(heard) >= 2 \
                and self.channel < len(levels) \
                and levels[self.channel] is not None \
                and levels[self.channel] > min(heard) + ENERGY_MARGIN:
            lv = float(levels[self.channel])
            self.own_level = lv if self.own_level is None else \
                0.8 * self.own_level + 0.2 * lv
        if self.fixed is not None or self.state in ('active', 'idle'):
            self._ehits = [0] * self.k
            return
        if self.state == 'silent' and (now < self._clean_from()
                                       or self.lagging):
            return                      # our own tail is still in the pipes
        known = [(k, lv) for k, lv in enumerate(levels) if lv is not None]
        if len(known) < 2:
            return
        for k, lv in known:
            self.levels[k] = lv
        floor = min(lv for _, lv in known)
        top = max(lv for _, lv in known)
        self.floor = floor
        for k, lv in known:
            if (self.state == 'silent' and k == self.channel
                    and lv > floor + ENERGY_MARGIN
                    and (self.own_level is None
                         or lv > self.own_level - OWN_TAIL)):
                # Our own transmission is still in the receive path (the
                # host or the bench is slower than the echo said): not
                # another transmitter. A second node on the channel is
                # far weaker here than our own signal, or it is decoded.
                # Before the own signal has been seen at all on this claim
                # (a slow host's first check) a strong reading cannot be
                # told from it, and counts for neither.
                self._ehits[k] = 0
                continue
            if self.state == 'silent' and k == self.channel:
                self._obs += 1          # the own channel, seen without us
            if lv > floor + ENERGY_MARGIN and lv > top - ENERGY_SPAN:
                self._ehits[k] += 1
                if self._ehits[k] >= 2:
                    self.energy_at[k] = now
                if (self.state == 'silent' and k == self.channel
                        and self._ehits[k] >= ENERGY_COLLIDE):
                    self.collision = 'energy'
            else:
                self._ehits[k] = 0

    def heard_by(self, now):
        """Some node's beacon lists us: our channel works."""
        self.deaf_moves = 0

    def move(self, reason):
        """Leave the current channel at the next tick (e.g. 'deaf')."""
        if self.fixed is None and self.state in ('active', 'silent'):
            self._move = reason

    # -------------------------------------------------------------- tick
    def tick(self, now):
        """Advance the state machine. Returns a list of (event, dict)."""
        ev = []
        if self.fixed is not None:
            return ev
        if self.state in ('listen', 'full') and now >= self.until:
            busy = self.busy(now)
            free = [k for k in range(self.k)
                    if not busy[k] and k not in self.avoid]
            if not free:
                free = [k for k in range(self.k) if not busy[k]]
            if free:
                self.channel = self.rng.choice(self._best(free))
                self.state = 'active'
                self.avoid = set()
                self.claims += 1
                self.claimed_at = now
                self.active_since = now
                self.early_left = EARLY_CHECKS
                self.next_check = now + self.rng.uniform(*FIRST_CHECK)
                ev.append(('claim', {'channel': self.channel}))
            else:
                if self.state != 'full':
                    ev.append(('full', {}))
                self.state = 'full'
                self.until = now + FULL_RETRY
        elif self.state in ('active', 'silent') and self._move is not None:
            reason, self._move = self._move, None
            if reason == 'deaf':
                self.deaf_moves += 1
            self._leave(now, reason, ev)
        elif self.state == 'active' and now >= self.next_check:
            self.state = 'silent'
            self.silent_from = now
            self.collision = None
            self._ehits = [0] * self.k
            self._obs = 0
            self.cur_extend, self.extend_next = self.extend_next, 0.0
            self.checks += 1
            ev.append(('silence', {'channel': self.channel,
                                   'extended': self.cur_extend}))
        elif self.state == 'silent':
            if self.collision is not None:
                self._leave(now, self.collision, ev)
            elif now >= min(self.silent_from + self.silence_max(),
                            max(self.silent_from + SILENCE,
                                self._clean_from() + SILENCE_CLEAN)) \
                    + self.cur_extend:
                # r6.1: did the check see the channel? (decoded frames are
                # all a lagging host can trust.) Not waited for - a longer
                # silence costs every link - but reported with the result.
                looked = (self.lagging or self._obs >= ENERGY_COLLIDE
                          or not self.energy_used)
                if not looked and self.must_see and now < (
                        self.silent_from + self.silence_max()
                        + self.cur_extend + LOOK_MAX):
                    return ev
                self.state = 'active'
                self.active_since = now
                if not looked:
                    self.unobserved += 1
                if self.early_left > 0:
                    self.early_left -= 1
                    self.next_check = now + self.rng.uniform(*EARLY_EVERY)
                else:
                    self.next_check = now + self.rng.uniform(*CHECK_EVERY)
                ev.append(('clear', {'channel': self.channel,
                                     'observed': looked,
                                     'extended': self.cur_extend}))
                self.cur_extend = 0.0
        return ev

    SCORE_SPAN = 3.0         # dB: channels this close to the best are equal

    def _best(self, free):
        """The free channels worth choosing between. Still a random choice
        among near-equals, so two nodes starting together rarely pick the
        same one."""
        if self.scorer is None or len(free) < 2:
            return free
        scores = {}
        for k in free:
            try:
                v = self.scorer(k)
            except Exception:                    # noqa: BLE001
                v = None
            if v is not None:
                scores[k] = float(v)
        self.last_scores = scores
        if not scores:
            return free
        top = max(scores.values())
        best = [k for k in free if k in scores
                and scores[k] >= top - self.SCORE_SPAN]
        return best or free

    def _leave(self, now, why, ev):
        self.collisions += 1
        ev.append(('collision', {'channel': self.channel, 'with': why}))
        self.avoid = {self.channel}
        self.channel = None
        self.collision = None
        self.state = 'listen'
        self.until = now + self.rng.uniform(*RELISTEN_TIME)
