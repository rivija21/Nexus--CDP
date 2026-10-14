"""Radio-free simulator for the r6 network: several ChatApp nodes, K channels.

Models what matters to the MAC and the link, and nothing else:

  * each node transmits on ONE channel (the latest ('chan', k) it asked for)
    and receives ALL channels, its own included;
  * a frame occupies its channel for its airtime (the node's own airtime
    model, so pacing is real) and frames from one node never overlap;
  * two frames on the same channel from different nodes that overlap in time
    are both lost, for every receiver - including a node that is itself
    transmitting on that channel (its own carrier drowns the other);
  * optional random frame loss per receiver, and optional adjacent-channel
    leakage: a frame is also decoded on channel k+-1 with probability `leak`,
    as a strong transmitter next to a receiver produces on real hardware;
  * garble: probability that a collision still yields an access-code hit
    and hence a CRC failure at a receiver (0 = collisions leave no trace);
  * energy: every 0.1 s each node gets a level per channel - noise at
    -85 dB, each transmitter at a random per-pair level between -60 and
    -35 dB, its adjacent-channel leakage and IQ image image_db lower, and
    the node's own transmitter far above everything on its channel.

r6.1 adds an optional radio front-end model (MeshSim(rf=RfModel(...))),
so the failures seen on hardware reproduce here before anything is fixed:

  * continuous carriers (the transmit filler keeps every transmitter on
    its channel while it is enabled), each node with a self-coupling path:
    its own transmitter 20-40 dB above the other nodes at its own ADC;
  * RX gain and TX attenuation per node, driven by the node's ('gain', ...)
    actions; a 12-bit ADC with full scale: peak, RMS and the fraction of
    clipped samples go to ChatApp.set_adc, clipping adds distortion to every
    channel (a strong own signal survives it, weak neighbours do not);
  * IQ image at image_db on the mirror FREQUENCY (wherever the node's
    channel plan puts it: with the r6.0 two-sided plan that is another
    channel, with the r6.1 one-sided plan it is no channel) and TX
    adjacent-channel leakage at aclr_db;
  * a frame decodes with a probability set by its SINR at that receiver
    (co-channel carriers, images, leakage, noise, clipping); a lost frame
    in the ramp still trips the access code and fails its CRC;
  * per-channel SNR (capped at 30 dB, as the EVM estimate) and in-band
    levels every 0.1 s, as the flowgraph's snapshot taps provide them;
  * per-node receive- and transmit-pipeline latency (Pluto buffers, a slow
    host: a node's own carrier keeps arriving at its receiver for tx +
    rx latency after it stops), and a transmitter slower than the node's
    airtime model (air_scale), with the transmit framer's queue report fed
    back as the flowgraph does.

Used by test_mesh.py, test_r61.py and bpsk_offline_demo.py. Imports nothing
from GNU Radio.
"""

import heapq
import inspect
import math
import os
import random
import zlib

import bpsk_app as app
import bpsk_secure as S


class SimNode(object):
    def __init__(self, name, chat, start):
        self.name = name
        self.app = chat
        self.start = start
        self.alive = True
        self.channel = None
        self.air_free = 0.0
        self.phase = 0
        self.sent = 0
        # r6.1 front end (used when the simulator has an RfModel)
        self.rx_gain = 30.0
        self.tx_atten = 50.0
        self.txen = None            # None: the app never said (r6.0.x)
        self.self_db = 0.0
        self.tx_loss = 0.0          # extra loss of its transmit antenna path
        self.rx_latency = 0.0       # receive pipeline latency, seconds
        self.last_end = -1.0        # end of its latest frame on air
        self.air_scale = 1.0        # real airtime / the app's model
        self.max_backlog = 0.0      # longest real transmit queue seen, s
        self.hist = []              # (t, channel, txen): carrier history
        self.tx_latency = 0.0       # transmit pipeline: a change of channel
        #                             or carrier reaches the air this late


class RfModel(object):
    """Radio front ends and paths for MeshSim (r6.1). Levels are dB
    relative to ADC full scale, in-channel after the matched filter.

    level(s -> r) = (50 - tx_atten[s]) - tx_loss[s] + (rx_gain[r] - 30)
                    + path(s, r)
    path(r, r) = self_db[r]      (drawn from self_db_range: own transmitter
                                  at the defaults 50/30 dB is at or above
                                  full scale, as on the bench)
    path(s, r) = mean self_db - U(below)   (reciprocal)
    """

    def __init__(self, self_db_range=(-2.0, 4.0), below=(20.0, 40.0),
                 image_db=-45.0, aclr_db=-60.0, noise_db=-82.0,
                 adc_floor_db=-80.0, crest_db=6.0, decode_db=(6.0, 12.0),
                 clip_dbc=-25.0, snr_cap=30.0, paths=None):
        self.self_db_range = self_db_range
        self.below = below
        self.image_db = float(image_db)
        self.aclr_db = float(aclr_db)
        self.noise_db = float(noise_db)
        self.adc_floor_db = float(adc_floor_db)
        self.crest_db = float(crest_db)
        self.decode_db = decode_db
        self.clip_dbc = float(clip_dbc)
        self.snr_cap = float(snr_cap)
        self.paths = dict(paths or {})     # {(a, b): dB} overrides


def _db(p):
    return 10.0 * math.log10(max(p, 1e-30))


def _lin(db):
    return 10.0 ** (db / 10.0)


class MeshSim(object):
    def __init__(self, root, num_channels=4, loss=0.0, leak=0.0,
                 latency=0.002, seed=1, opts=None, garble=0.0, energy=True,
                 image_db=-45.0, rf=None, meas_period=0.1):
        self.root = root
        # r6.1: how often the snapshot taps deliver channel levels (0.1 s on
        # the radio; S x 0.1 s on the bench at --scale S, whose taps count
        # samples)
        self.meas_step = max(2, int(round(float(meas_period) * 1000)))
        self.k = num_channels
        self.loss = float(loss)
        self.leak = float(leak)
        self.garble = float(garble)
        self.energy = bool(energy)
        self.image_db = float(image_db)
        self._path = {}
        self.latency = float(latency)
        self.rnd = random.Random(seed)
        self.seed = seed
        # overhead 140 = preamble 64 + postamble 64 + 12, the r6 default
        self.opts = dict(ack_timeout=0.5, max_retries=5, frag_size=256,
                         sym_rate=250000.0, overhead=140)
        self.opts.update(opts or {})
        self.nodes = {}
        self.t = 0.0
        self._pending = []          # heap: (deliver_at, n, record)
        self._onair = {k: [] for k in range(num_channels)}
        self._n = 0
        self.log = []               # (t, sender, channel, raw) if recording
        self.record = False
        self.lost_collision = 0
        self.delivered = 0
        self.rf = rf if rf is not True else RfModel()
        self.lost_rf = 0
        self._sinr_last = {}        # (sender, receiver) -> last SINR, dB
        if self.rf is not None:
            # As the flowgraph runs the node: continuous carrier (PRBS fill,
            # IDLE only as a keep-alive), the one-sided channel plan and the
            # gain control - where this version of ChatApp has them.
            params = inspect.signature(app.ChatApp.__init__).parameters
            if 'filler' in params:
                self.opts.setdefault('filler', 'keepalive')
            if 'ch_first' in params:
                self.opts.setdefault('ch_first', 450e3)
            if 'gain' in params:
                self.opts.setdefault('gain', dict(rx_gain=30, tx_atten=50,
                                                  auto=True))

    # --------------------------------------------------------------- nodes
    def add(self, name, security=True, channel=None, key=None, start=None,
            nick=None, rx_latency=0.0, self_db=None, tx_loss=0.0,
            air_scale=1.0, tx_latency=0.0, **kw):
        base = os.path.join(self.root, name)
        keys = os.path.join(base, 'keys')
        if key is not None:
            os.makedirs(keys, exist_ok=True)
            path = os.path.join(keys, 'node.key')
            if not os.path.exists(path):
                S.write_private(path, key)
        opts = dict(self.opts)
        opts.update(kw)
        rng = opts.pop('rng', None) or random.Random(
            zlib.crc32(('%s/%s' % (self.seed, name)).encode()))
        chat = app.ChatApp(None, None, nick or name, os.path.join(base, 'files'),
                           security=security, keys_dir=keys, create_keys=True,
                           channel=channel, num_channels=self.k, rng=rng,
                           **opts)
        node = SimNode(name, chat, self.t if start is None else start)
        node.phase = self.rnd.randrange(10)
        node.rx_latency = float(rx_latency)
        node.tx_loss = float(tx_loss)
        node.air_scale = float(air_scale)
        node.tx_latency = float(tx_latency)
        if self.rf is not None:
            lo, hi = self.rf.self_db_range
            node.self_db = (self.rnd.uniform(lo, hi) if self_db is None
                            else float(self_db))
            g = opts.get('gain') or {}
            node.rx_gain = float(g.get('rx_gain', 30))
            node.tx_atten = float(g.get('tx_atten', 50))
        self.nodes[name] = node
        return chat

    def restart(self, name, **kw):
        """Same keys and files, fresh process."""
        old = self.nodes[name]
        old.alive = False
        chat = self.add(name, security=old.app.sec is not None
                        or old.app.sec_error is not None,
                        nick=old.app.nick, **kw)
        return chat

    def stop(self, name):
        self.nodes[name].alive = False

    def trust_all(self, names=None):
        names = list(names or self.nodes)
        for a in names:
            for b in names:
                if a != b:
                    ka = self.nodes[a].app.keys
                    kb = self.nodes[b].app.keys
                    ka.trust(kb.public, b)

    def app(self, name):
        return self.nodes[name].app

    def pid(self, name):
        return app.peer_id(self.nodes[name].app.identity)

    # ----------------------------------------------------------------- air
    def _emit(self, node, actions):
        for kind, value in actions:
            if kind == 'chan':
                node.channel = value
                node.hist.append((self.t + node.tx_latency, node.channel,
                                  node.txen))
            elif kind == 'txen':
                node.txen = bool(value)
                node.hist.append((self.t + node.tx_latency, node.channel,
                                  node.txen))
            elif kind == 'gain':
                if value.get('what') == 'rx':
                    node.rx_gain = float(value['value'])
                elif value.get('what') == 'tx':
                    node.tx_atten = float(value['value'])
            elif kind == 'tx':
                if node.channel is None:
                    continue
                air = node.app.link.radio.airtime(len(value)) * node.air_scale
                start = max(self.t + node.tx_latency, node.air_free)
                end = start + air
                node.air_free = end
                node.max_backlog = max(node.max_backlog, end - self.t)
                node.last_end = end
                node.sent += 1
                rec = (start, end, node.name, node.channel, value)
                self._onair[node.channel].append(rec)
                self._n += 1
                if self.rf is not None:
                    heapq.heappush(self._pending, (end, self._n,
                                                   ('air', rec)))
                elif any(n.rx_latency for n in self.nodes.values()):
                    for r in list(self.nodes.values()):
                        self._n += 1
                        heapq.heappush(self._pending, (
                            end + self.latency + r.rx_latency, self._n,
                            ('to', rec, r.name)))
                else:
                    heapq.heappush(self._pending, (end + self.latency,
                                                   self._n, rec))
                if self.record:
                    self.log.append((start, node.name, node.channel, value))

    def _collided(self, rec):
        start, end, who, ch, _ = rec
        for s, e, w, _c, _v in self._onair[ch]:
            if w != who and s < end and e > start:
                return True
        return False

    def _deliver(self, rec, only=None):
        if rec[0] == 'air':
            return self._air_rf(rec[1])
        if rec[0] == 'rx':
            _, rec, name, raw = rec
            node = self.nodes.get(name)
            if node is not None and node.alive and self.t >= node.start:
                self._emit(node, node.app.on_rx(raw, self.t, channel=rec[3]))
            return
        if rec[0] == 'to':
            _, rec, only = rec
        start, end, who, ch, raw = rec
        if self._collided(rec):
            # Overlapping transmissions: often the access code is still
            # found and the frame then fails its CRC.
            self.lost_collision += 1
            bad = bytes([raw[0]]) + bytes(b ^ 0x5A for b in raw[1:])
            for node in list(self.nodes.values()):
                if node.alive and self.t >= node.start and node.name != who \
                        and self.rnd.random() < self.garble:
                    self._emit(node, node.app.on_rx(bad, self.t, channel=ch))
            return
        for node in list(self.nodes.values()):
            if not node.alive or self.t < node.start:
                continue
            if only is not None and node.name != only:
                continue
            if node.name != who and self.loss and self.rnd.random() < self.loss:
                continue
            self.delivered += 1
            self._emit(node, node.app.on_rx(raw, self.t, channel=ch))
            if self.leak and node.name != who:
                for adj in (ch - 1, ch + 1):
                    if 0 <= adj < self.k and self.rnd.random() < self.leak:
                        self._emit(node, node.app.on_rx(raw, self.t,
                                                        channel=adj))

    def run(self, seconds, until=None, check_every=0.05):
        end = self.t + seconds
        step = int(round(self.t * 1000))
        next_check = self.t
        while self.t < end:
            step += 1
            self.t = step / 1000.0
            while self._pending and self._pending[0][0] <= self.t:
                _, _, rec = heapq.heappop(self._pending)
                self._deliver(rec)
            for node in list(self.nodes.values()):
                if node.alive and self.t >= node.start \
                        and step % 10 == node.phase:
                    self._emit(node, node.app.on_tick(self.t))
                if node.alive and step % 50 == node.phase:
                    # r6.1: the transmit framer reports its real queue every
                    # 50 ms and the shim hands it to the airtime model.
                    radio = node.app.link.radio
                    if hasattr(radio, 'observe') and \
                            getattr(radio, 'enabled', True):
                        radio.observe(self.t, max(self.t, node.air_free))
            if self.energy and step % self.meas_step == self.meas_step // 2:
                for node in list(self.nodes.values()):
                    if node.alive and self.t >= node.start:
                        if self.rf is not None:
                            self._measure_rf(node)
                        else:
                            node.app.set_channel_levels(self._levels(node),
                                                        self.t)
            if step % 1000 == 0:
                for n in self.nodes.values():
                    if len(n.hist) > 64:
                        n.hist = n.hist[-64:]
                keep = 1.0 + self.latency + max(
                    [n.rx_latency for n in self.nodes.values()] or [0.0])
                for ch, lst in self._onair.items():
                    self._onair[ch] = [r for r in lst if r[1] > self.t - keep]
            if until is not None and self.t >= next_check:
                next_check = self.t + check_every
                if until(self):
                    return True
        return until(self) if until is not None else True

    def _pair_db(self, a, b):
        key = tuple(sorted((a, b)))
        if key not in self._path:
            self._path[key] = self.rnd.uniform(-60.0, -35.0)
        return self._path[key]

    def _levels(self, me):
        k = self.k
        power = [10 ** (-85.0 / 10)] * k
        on = {}
        t = self.t - self.latency       # what the receiver sees now
        for ch, lst in self._onair.items():
            for s0, e0, who, _c, _v in lst:
                if s0 <= t < e0:
                    on[who] = ch
        for who, ch in on.items():
            p = 5.0 if who == me.name else self._pair_db(who, me.name)
            for kk in range(k):
                if kk == ch:
                    lv = p
                elif abs(kk - ch) == 1 or kk == k - 1 - ch:
                    lv = p + self.image_db
                else:
                    lv = p + self.image_db - 15.0
                power[kk] += 10 ** (lv / 10)
        return [10 * math.log10(x) for x in power]

    # ---------------------------------------------------------- RF (r6.1)
    def _state_at(self, node, t):
        """(channel, txen) of a node at time t (its carrier history)."""
        ch, en = node.channel, node.txen
        for ht, hch, hen in reversed(node.hist):
            if ht <= t:
                return hch, hen
            ch, en = None, None
        return ch, en

    def _carrier_on(self, node, t):
        if not node.alive or t < node.start:
            return False
        ch, en = self._state_at(node, t)
        if ch is None:
            return False
        if en is not None:
            return en
        # r6.0.x: the IDLE filler keeps the carrier up while frames flow
        return t - node.last_end < 0.02

    def _carriers(self, t):
        out = []
        for n in self.nodes.values():
            if self._carrier_on(n, t):
                out.append((n, self._state_at(n, t)[0]))
        return out

    def _rf_path(self, s, r):
        rf = self.rf
        if (s.name, r.name) in rf.paths:
            return rf.paths[(s.name, r.name)]
        if s is r:
            return s.self_db
        key = tuple(sorted((s.name, r.name)))
        if key not in self._path:
            lo, hi = rf.self_db_range
            self._path[key] = 0.5 * (lo + hi) - self.rnd.uniform(*rf.below)
        return self._path[key]

    def level(self, s, r):
        """Level of transmitter s at receiver r, dBFS (in-channel)."""
        # tx_loss: a broken antenna or cable - the node still hears its own
        # transmitter through the board's own coupling
        return ((50.0 - s.tx_atten) - (s.tx_loss if s is not r else 0.0)
                + (r.rx_gain - 30.0) + self._rf_path(s, r))

    def _front_end(self, r, t):
        """(per-channel interference+signal powers, carriers with levels,
        noise power, adc dict) at receiver r."""
        rf = self.rf
        k = self.k
        offs = [r.app.offset(c) for c in range(k)]
        spacing = abs(offs[1] - offs[0]) if k > 1 else 600e3
        noise = _lin(rf.noise_db + (r.rx_gain - 30.0)) + _lin(rf.adc_floor_db)
        cars = [(s, ch, self.level(s, r)) for s, ch in self._carriers(t)]
        total = noise + sum(_lin(lv) for _, _, lv in cars)
        peak = _db(total) + rf.crest_db
        clip = 0.0 if peak <= 0.0 else min(0.7, 0.08 * peak)
        clipn = _lin(_db(total) + rf.clip_dbc + 2.0 * peak) if peak > 0 \
            else 0.0
        power = [noise + clipn] * k
        for s, ch, lv in cars:
            f = offs[ch]
            for c in range(k):
                if c == ch:
                    power[c] += _lin(lv)
                elif abs(abs(offs[c] - f) - spacing) < 0.25 * spacing:
                    power[c] += _lin(lv + rf.aclr_db)
                if abs(offs[c] + f) < 0.5 * spacing:
                    power[c] += _lin(lv + rf.image_db)
        adc = {'peak_dbfs': min(peak, 0.0), 'rms_dbfs': min(_db(total), 0.0),
               'clip': clip}
        return power, cars, adc

    def _sinr_at(self, s, ch, r, t):
        power, cars, _ = self._front_end(r, t)
        lv = self.level(s, r)
        return lv - _db(max(power[ch] - _lin(lv), 1e-30))

    def _air_rf(self, rec):
        """A frame has left the antenna: decide per receiver (SINR) and
        schedule its delivery after that receiver's pipeline latency."""
        start, end, who, ch, raw = rec
        sender = self.nodes.get(who)
        lo, hi = self.rf.decode_db
        mid = 0.5 * (start + end)
        for node in list(self.nodes.values()):
            if not node.alive or self.t < node.start or sender is None:
                continue
            sinr = self._sinr_at(sender, ch, node, mid)
            self._sinr_last[(who, node.name)] = sinr
            p = 1.0 if sinr >= hi else 0.0 if sinr <= lo else \
                (sinr - lo) / (hi - lo)
            if node.name != who and self.loss and self.rnd.random() < self.loss:
                p = 0.0
            if self.rnd.random() < p:
                data = raw
            elif sinr > lo - 6.0 and self.rnd.random() < 0.5:
                self.lost_rf += 1
                data = bytes([raw[0]]) + bytes(b ^ 0x5A for b in raw[1:])
            else:
                self.lost_rf += 1
                continue
            self._n += 1
            heapq.heappush(self._pending, (
                end + self.latency + node.rx_latency, self._n,
                ('rx', rec, node.name, data)))

    def _measure_rf(self, node):
        """What the snapshot taps would report every 0.1 s - of the air as
        it was one receive-pipeline latency ago."""
        t = self.t
        power, cars, adc = self._front_end(node, t - node.rx_latency)
        a = node.app
        a.set_channel_levels([_db(p) for p in power], t)
        a.set_adc(peak_dbfs=round(adc['peak_dbfs'], 1),
                  rms_dbfs=round(adc['rms_dbfs'], 1), clip=adc['clip'], t=t)
        best = {}
        for s, ch, lv in cars:
            if ch not in best or lv > best[ch][1]:
                best[ch] = (s, lv)
        for c in range(self.k):
            kw = dict(channel=c)
            if self._set_t:
                kw['t'] = t
            if c in best:
                s, lv = best[c]
                snr = lv - _db(max(power[c] - _lin(lv), 1e-30))
                a.set_radio_metrics(snr_db=round(min(snr, self.rf.snr_cap), 1),
                                    level_db=round(_db(power[c]), 1),
                                    locked=snr > 5.0, **kw)
            else:
                a.set_radio_metrics(snr_db=None, level_db=round(
                    _db(power[c]), 1), locked=False, **kw)

    @property
    def _set_t(self):
        if not hasattr(self, '_set_t_cache'):
            self._set_t_cache = 't' in inspect.signature(
                app.ChatApp.set_radio_metrics).parameters
        return self._set_t_cache

    def link_snr(self, a, b):
        """SINR of a's transmission at b in the last frame decided, dB."""
        return self._sinr_last.get((a, b))

    # ------------------------------------------------------------- queries
    def alive(self):
        return [n for n in self.nodes.values() if n.alive]

    def settled(self):
        """Every live node transmits, on a channel of its own, with an
        address of its own, and knows every other live node."""
        live = self.alive()
        chans = [n.app.chan.channel for n in live]
        addrs = [n.app.my_addr for n in live]
        if any(n.app.chan.state not in ('active', 'silent') for n in live):
            return False
        if len(set(chans)) != len(live) or len(set(addrs)) != len(live):
            return False
        for n in live:
            for m in live:
                if m is n:
                    continue
                e = n.app.peers.get(self.pid(m.name))
                if e is None or e['addr'] != m.app.my_addr or not e['present']:
                    return False
        return True

    def secure(self, a, b):
        pa, pb = self.nodes[a].app, self.nodes[b].app
        return (pa.sec.ready(pb.my_addr, self.t)
                and pb.sec.ready(pa.my_addr, self.t))

    def state(self, name, mid):
        msg = self.nodes[name].app._by_mid.get(mid)
        return msg['state'] if msg else None

    def texts(self, name, conv=None, frm=None):
        out = []
        for m in self.nodes[name].app.messages:
            if m.get('dir') != 'in' or m.get('kind') != 'text':
                continue
            if conv is not None and m.get('conv') != conv:
                continue
            if frm is not None and m.get('from') != self.pid(frm):
                continue
            out.append(m['text'])
        return out

    def files_in(self, name):
        return [m for m in self.nodes[name].app.messages
                if m.get('dir') == 'in' and m.get('kind') == 'file']
