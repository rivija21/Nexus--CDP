"""CSMA/CA medium access for the shared-channel BPSK link (r6b).

Pure Python, no GNU Radio import - the same discipline as bpsk_link.py, so the
state machine can be driven by a unit test with simulated time instead of a
radio.

Where it sits:

    bpsk_link   decides WHAT to send and when to retransmit
        |  ('tx', frame)
    bpsk_mac    decides WHEN a frame may go onto a shared channel   <- here
        |  cleared frames
    modulator / Pluto sink
        ^
        |  carrier sense: mean power on the raw receive stream

Why this exists at all
----------------------
r5 and r6a are FDD: each node owns its transmit carrier outright, so it may
transmit whenever it likes and the idle filler runs continuously. On one shared
channel that is fatal - a node that transmits continuously holds the medium for
ever and nobody else ever gets it. Under CSMA the filler is off
(queue_ahead = 0) and every frame has to win the channel first.

Timing, in slots
----------------
    slot   must be >= the receiver's carrier-sense latency, because a station
           cannot react to a transmission it has not yet heard. With a Pluto
           source buffer of 2048 samples at 1 MS/s that latency is 2.05 ms, so
           the default slot is 3 ms. At the stock buffer of 32768 it would be
           32.8 ms - longer than an entire data frame, which is why that buffer
           size cannot be used with CSMA.
    SIFS   1 slot. An acknowledgement waits only this, so it always beats a
           station that must wait DIFS plus a backoff.
    DIFS   2 slots. Idle time a station with data must observe before it starts
           counting down.

Backoff is binary exponential and FREEZES while the channel is busy. Freezing
rather than redrawing is what makes the scheme fair: a station that has already
waited keeps its credit, so it is not repeatedly beaten by a station that just
arrived.

Collision inference
-------------------
The MAC never sees an acknowledgement - that is the link layer's business. It
infers a failure from the retry flag on the frame handed back to it: a frame
carrying FLAG_RETRY is by definition an attempt that was not acknowledged, so
the contention window doubles and the backoff is redrawn. It is an
approximation (a retry can also mean plain noise, not a collision), and it errs
toward backing off, which is the safe direction on a shared channel.
"""

import random
from collections import deque

import bpsk_link as link

PRIO_CONTROL = 0        # acknowledgements: SIFS, no backoff
PRIO_DATA = 1           # everything else: DIFS, then backoff

# Frame types that will be answered with an acknowledgement, so the channel is
# not really free the moment they end.
_ACKED = (link.FT_DATA, link.FT_FILE_START, link.FT_FILE_DATA,
          link.FT_FILE_END, link.FT_FILE_NACK, link.FT_FILE_DONE)

_ACK_FRAME_LEN = link.SCRAM_LEN + link.HDR_LEN + link.CRC_LEN      # 13 bytes


class MacState(object):
    """One station's view of a shared channel."""

    def __init__(self, slot=0.003, cw_min=8, cw_max=128, difs_slots=2,
                 sifs_slots=1, overhead=460, sym_rate=250000.0,
                 busy_threshold_db=-55.0, seed=None):
        self.slot = float(slot)
        self.cw_min = int(cw_min)
        self.cw_max = int(cw_max)
        self.difs = difs_slots * self.slot
        self.sifs = sifs_slots * self.slot
        self.overhead = int(overhead)
        self.sym_rate = float(sym_rate)
        self.busy_threshold_db = float(busy_threshold_db)
        self.rng = random.Random(seed)

        self.cw = self.cw_min
        self.qh = deque()               # control frames
        self.ql = deque()               # data frames
        self.busy = False               # physical carrier sense
        self.nav_until = 0.0            # virtual carrier sense
        self.tx_until = 0.0             # our own frame still on the air
        self.backoff = None             # slots remaining, None = not counting
        self._credit = 0.0              # idle seconds toward the next slot
        self._idle_since = 0.0          # when the medium last went idle
        self._last = None               # previous tick, for dt

        self.stats = dict(tx=0, tx_control=0, deferrals=0, backoff_slots=0,
                          retries_seen=0, cw_doublings=0, nav_defers=0,
                          busy_time=0.0, idle_time=0.0)

    # ------------------------------------------------------------- sensing
    def set_busy(self, busy, now):
        """Physical carrier sense, from the power detector."""
        busy = bool(busy)
        if busy == self.busy:
            return
        self.busy = busy
        if not busy:
            self._idle_since = now

    def set_level(self, level_db, now):
        """Convenience: threshold a measured level and set the busy flag."""
        self.set_busy(level_db is not None
                      and level_db >= self.busy_threshold_db, now)

    def note_rx(self, frame, now):
        """A frame was received. Hold off for the acknowledgement it will draw.

        Without this the medium looks free during the SIFS gap between a data
        frame and its acknowledgement, and a third station walks straight into
        the acknowledgement. That is the classic reason a NAV exists.
        """
        ftype, _ = link.peek(frame)
        if ftype in _ACKED:
            until = now + self.sifs + self._airtime(_ACK_FRAME_LEN)
            if until > self.nav_until:
                self.nav_until = until
                self.stats['nav_defers'] += 1

    # ------------------------------------------------------------ outbound
    def enqueue(self, frame, now):
        """Take a frame from the link layer. It goes out when the MAC says so."""
        ftype, retry = link.peek(frame)
        if retry:
            # The previous attempt was never acknowledged. Back off harder.
            self.stats['retries_seen'] += 1
            if self.cw < self.cw_max:
                self.cw = min(self.cw * 2, self.cw_max)
                self.stats['cw_doublings'] += 1
            self.backoff = None          # redraw against the bigger window
        if ftype == link.FT_ACK:
            self.qh.append(frame)
        else:
            self.ql.append(frame)

    def pending(self):
        return len(self.qh) + len(self.ql)

    # ---------------------------------------------------------------- tick
    def on_tick(self, now):
        """Advance the state machine. Returns frames cleared to transmit."""
        dt = 0.0 if self._last is None else max(0.0, now - self._last)
        self._last = now
        medium_busy = self.busy or now < self.nav_until
        if medium_busy:
            self.stats['busy_time'] += dt
        else:
            self.stats['idle_time'] += dt

        out = []
        if now < self.tx_until:
            return out                    # our own transmission is still going
        if not self.qh and not self.ql:
            self.backoff = None           # nothing to send, drop the countdown
            return out

        idle_ref = max(self._idle_since, self.nav_until)

        # Control frames take SIFS and never contend.
        if self.qh:
            if not medium_busy and (now - idle_ref) >= self.sifs:
                out.append(self._release(self.qh.popleft(), now, PRIO_CONTROL))
            else:
                self.stats['deferrals'] += 1
            return out

        if medium_busy:
            self.stats['deferrals'] += 1
            return out                    # backoff frozen: credit not advanced
        if (now - idle_ref) < self.difs:
            return out

        if self.backoff is None:
            # Draw on this tick, start counting on the next: the elapsed dt
            # belongs to the DIFS that just finished, not to the first slot.
            self.backoff = self.rng.randrange(self.cw)
            self._credit = 0.0
        else:
            self._credit += dt
        while self._credit >= self.slot and self.backoff > 0:
            self._credit -= self.slot
            self.backoff -= 1
            self.stats['backoff_slots'] += 1
        if self.backoff == 0:
            self.backoff = None
            out.append(self._release(self.ql.popleft(), now, PRIO_DATA))
        return out

    # -------------------------------------------------------------- helper
    def _airtime(self, frame_len):
        return (self.overhead + frame_len) * 8.0 / self.sym_rate

    def _release(self, frame, now, prio):
        self.tx_until = now + self._airtime(len(frame))
        self._credit = 0.0
        self.stats['tx'] += 1
        if prio == PRIO_CONTROL:
            self.stats['tx_control'] += 1
        else:
            _, retry = link.peek(frame)
            if not retry:
                self.cw = self.cw_min     # a fresh frame got out cleanly
        return frame

    def state_name(self, now):
        if now < self.tx_until:
            return 'transmitting'
        if not self.qh and not self.ql:
            return 'idle'
        if self.busy:
            return 'deferring'
        if now < self.nav_until:
            return 'nav'
        if self.backoff:
            return 'backoff'
        return 'contending'

    def snapshot(self, now=None):
        now = self._last if now is None else now
        total = self.stats['busy_time'] + self.stats['idle_time']
        return {'state': self.state_name(now or 0.0),
                'busy': self.busy,
                'nav': max(0.0, self.nav_until - (now or 0.0)),
                'backoff': self.backoff or 0,
                'cw': self.cw,
                'queued': self.pending(),
                'slot_ms': self.slot * 1000.0,
                'utilisation': (self.stats['busy_time'] / total) if total else 0.0,
                'threshold_db': self.busy_threshold_db,
                'tx': self.stats['tx'],
                'deferrals': self.stats['deferrals'],
                'retries_seen': self.stats['retries_seen']}
