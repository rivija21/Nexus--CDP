"""
Application layer for the BPSK PlutoSDR network (r6: several nodes, FDMA).

Layering
--------
    chat_ui.html          browser: peers, conversations, telemetry, composer
        | HTTP + Server-Sent Events on 127.0.0.1
    bpsk_app.py           this file: peers, conversations, receipts, server
        | ('tx', bytes) / ('chan', k) / ('evt', dict)
    bpsk_mac.py           who owns which channel, which address is whose
    bpsk_link.py          Selective Repeat ARQ per peer (LinkMux), files
        | plaintext link frames
    bpsk_secure.py        end-to-end encryption: Noise KK per pair of nodes
        | sealed frames (PDU)
    GNU Radio flowgraph   BPSK modem on one of K channels, receives all K

Like bpsk_link, this module imports nothing from GNU Radio, so the whole
network can be exercised without a radio (see bpsk_offline_demo.py and
test_mesh.py). Only the standard library is used.

Two modes
---------
    mesh    (peer_addr = 0, the r6 default) every node beacons its key and
            nickname once a second, the transmit channel is chosen
            automatically, and the operator approves new nodes in the UI.
            Any number of peers. The address is derived from this node's
            public key (my_addr = 0) or fixed by hand (my_addr != 0).
    static  (my_addr and peer_addr both set) the r5.4 behaviour: fixed
            address, one peer, keys from roster.json, a fixed channel, no
            beacons.

Application envelope, carried inside the payload of a link DATA frame:

    byte  0        : 0x9A magic
    byte  1        : app version (4b) | app frame type (4b)
    bytes 2-5      : message id, uint32 big-endian, sender-scoped
    byte  6        : part index of a multi-frame message
    byte  7        : number of parts
    byte  8        : sender nickname length N
    bytes 9..9+N-1 : nickname, utf-8
    rest           : this part's payload

    type 1  text to this node          type 3  text to the group (r6)
    type 2  presence (static mode)

A group message is sent to every approved peer in range as a separate,
separately encrypted 1:1 copy, so the group has exactly the privacy of the
pairs it is made of and each copy has its own delivery receipt.

File attachments reuse the link layer's file protocol; the envelope fields
travel in the FILE_START metadata blob as compact JSON.

r6.1
----
    two-way link table   every beacon carries how well its sender hears each
                         neighbour (bpsk_mac link report); each peer row shows
                         "I hear X at a dB / X hears me at b dB".
    link faults          a node nobody can hear is not moved to another
                         channel on a guess (r6.0: up to two blind moves). It
                         runs a silence check on its own channel at once; if
                         that finds no other transmitter, the fault is in the
                         link, and the node says which neighbour cannot hear
                         it and the likely reason (its receiver overloaded,
                         no signal arriving at all, ...), and stays put.
    channel choice       among free channels, those on which the neighbours'
                         receivers see the least interference are preferred.
    gain staging         bpsk_gain.GainControl moves RX gain and TX
                         attenuation (('gain', {...}) actions for the
                         flowgraph); /gain and /agc override it.
    telemetry            one CSV row per second (node) plus one per peer.
"""

import json
import math
import mimetypes
import os
import random
import re
import struct
import threading
import time
from collections import deque, OrderedDict

import bpsk_gain as gainlaw
import bpsk_link as link
import bpsk_mac as mac
import bpsk_secure as secure

APP_MAGIC = 0x9A
APP_VERSION = 1

AT_TEXT = 0x1
AT_PRESENCE = 0x2
AT_GROUP = 0x3

GROUP = 'all'                    # conversation id of the group chat

MAX_PARTS = 255
PRESENCE_MIN_INTERVAL = 10.0     # seconds between nickname announcements
STATS_INTERVAL = 0.25            # seconds between telemetry pushes to the UI
EVENT_BACKLOG = 2000             # SSE events retained for reconnecting clients
SNAPSHOT_EVENTS = ('stats', 'view', 'peers')   # only the newest is kept
MAX_UPLOAD = 8 * 1024 * 1024     # bytes accepted from the browser in one POST
MAX_FILE_TOKENS = 1000           # attachment URLs retained; oldest evicted
PART_TIMEOUT = 60.0              # seconds before an incomplete multi-part text is dropped
PEER_TIMEOUT = 5.0               # a peer silent this long is shown as away
PEER_SWEEP = 0.5                 # how often presence is re-evaluated
HEARS_ME = 5.0                   # a beacon listing us this recently: "hears you"
DEAF_MOVE_LIMIT = 2              # r6.0 only: blind moves on deafness (r6.1: none)
FRESH = 2.0                      # a channel reading older than this is stale
LINK_FAULT_EVERY = 30.0          # seconds between repeated link-fault reports
ALONE_AFTER = 10.0               # r6.1: hearing nobody this long on a channel
                                 # (+ 2 x echo): 'alone', see _mac_tick
OVERLOAD_CLIP = 0.001            # clipped fraction this node calls overload
ADC_CLIP_WARN = 0.001            # fraction of clipped ADC samples that is an overload
ADC_WARN_EVERY = 30.0            # seconds between overload warnings
WAIT_LIMIT = 60.0                # a message for a node not (yet) in range waits this long

_SPACES = re.compile(r'\s+')

# set_radio_metrics() has to be able to say "no reading" as well as "unchanged",
# so an omitted argument and an explicit None mean different things.
_UNSET = object()


def _safe_filename(name):
    """Sanitise a local filename without discarding non-Latin scripts.

    r5.1 kept only [A-Za-z0-9._-], so a Sinhala or Tamil name collapsed to
    its extension ('<sinhala>.png' -> 'png') and the receiver lost both the
    name and the type. Separators, control characters and the characters
    Windows rejects are still removed (bpsk_link.clean_name).
    """
    name = _SPACES.sub('_', link.clean_name(name or '').strip())
    return name if name and name != 'unnamed' else 'file'


def pack_envelope(atype, mid, part, parts, nick, body):
    nick_b = nick.encode('utf-8', 'replace')[:32]
    return (bytes([APP_MAGIC, ((APP_VERSION & 0xF) << 4) | (atype & 0xF)])
            + struct.pack('!I', mid & 0xFFFFFFFF)
            + bytes([part & 0xFF, parts & 0xFF, len(nick_b)])
            + nick_b + body)


def unpack_envelope(payload):
    """Decode an application envelope, or None if this is not one."""
    if len(payload) < 9 or payload[0] != APP_MAGIC:
        return None
    if (payload[1] >> 4) != APP_VERSION:
        return None
    atype = payload[1] & 0x0F
    mid = struct.unpack('!I', payload[2:6])[0]
    part, parts, nlen = payload[6], payload[7], payload[8]
    if len(payload) < 9 + nlen:
        return None
    nick = payload[9:9 + nlen].decode('utf-8', 'replace')
    return {'type': atype, 'mid': mid, 'part': part, 'parts': parts,
            'nick': nick, 'body': payload[9 + nlen:]}


def peer_id(identity):
    """Stable conversation id for a node: its key fingerprint, no spaces."""
    return 'p' + secure.fingerprint(bytes(identity)).replace(' ', '')


def _aggregate(rcpts):
    """Overall delivery state of a message sent to several peers."""
    states = [r['state'] for r in rcpts]
    if all(s == 'delivered' for s in states):
        return 'delivered'
    if all(s in ('delivered', 'failed') for s in states):
        return 'partial' if 'delivered' in states else 'failed'
    if any(s in ('sent', 'delivered', 'failed') for s in states):
        return 'sent'
    return 'queued'


def _rss_mb():
    """Resident memory of this process, MB (Linux; '' elsewhere) - the soak
    test watches it for growth."""
    try:
        with open('/proc/self/statm') as fh:
            pages = int(fh.read().split()[1])
        return round(pages * os.sysconf('SC_PAGE_SIZE') / 1048576.0, 1)
    except (OSError, ValueError, IndexError, AttributeError):
        return ''


class ChatApp(object):
    """Peers, conversations, receipts and telemetry on top of a LinkMux.

    The GNU Radio thread drives on_rx / on_tick / on_console and gets an
    action list back:
        ('tx', bytes)    transmit this frame
        ('chan', k)      move the transmitter to channel k (mesh mode)
        ('log', str)     console line
    The HTTP threads only ever call submit() and the read-only accessors, both
    of which take the same lock, so no radio call can be re-entered from a
    request handler.
    """

    def __init__(self, my_addr=None, peer_addr=None, nick=None, store_dir=None,
                 ack_timeout=0.5, max_retries=5, frag_size=256,
                 queue_ahead=0.03, sym_rate=250000.0, overhead=452,
                 tx_freq=0.0, rx_freq=0.0, security=True, keys_dir=None,
                 create_keys=False, window=32, channel=None,
                 num_channels=mac.NUM_CHANNELS, ch_spacing=mac.CH_SPACING,
                 rng=None, ch_first=None, filler='frames', gain=None,
                 telemetry_dir=None, plan_file=None):
        self.store_dir = os.path.abspath(store_dir or 'chat_files')
        try:
            os.makedirs(self.store_dir, exist_ok=True)
        except OSError:
            self.store_dir = os.path.abspath('.')
        self.mesh = not (my_addr and peer_addr)
        self.rng = rng or random.Random()
        self.num_channels = max(1, int(num_channels))
        self.ch_spacing = float(ch_spacing)
        if channel is not None and not 0 <= int(channel) < self.num_channels:
            channel = None                       # out of range: automatic
        # r6.1: None = the r6.0 symmetric plan, a number = one-sided plan
        # whose channel 0 sits at ch_first (bpsk_mac.channel_offset).
        self.ch_first = None if ch_first is None else float(ch_first)
        self.plan_file = plan_file or None
        self.plan_note = None
        self.tx_freq = float(tx_freq or 0.0)
        self.rx_freq = float(rx_freq or 0.0)

        self.sec = None
        self.sec_error = None
        self.keys_dir = None
        self.keys = None
        if security:
            self.keys_dir = os.path.abspath(
                keys_dir or os.path.join(os.path.dirname(self.store_dir),
                                         'keys'))
            try:
                # create=False: keys are made deliberately with bpsk_keys.py,
                # never as a side effect. GRC instantiates this block every
                # time it validates the flowgraph, from whatever directory it
                # was started in; auto-creating a key there left stray private
                # keys around and a different identity per directory.
                if self.mesh:
                    self.keys = secure.MeshKeyStore(self.keys_dir,
                                                    create=create_keys)
                else:
                    self.keys = secure.KeyStore(self.keys_dir, my_addr,
                                                create=create_keys)
            except (secure.KeyError_, RuntimeError, OSError) as exc:
                self.sec_error = str(exc)

        # ---------------------------------------------------- identity, MAC
        self.addrm = None
        if self.mesh and channel is None and self.plan_file:
            channel = self._planned_channel(nick)
        if self.mesh:
            if self.keys is not None:
                self.identity, is_key = self.keys.public, True
                n = self.keys.get_state('addr_n', 0)
            else:
                self.identity, n = self._plain_identity()
                is_key = False
            self.is_key = is_key
            self.addrm = mac.AddressManager(self.identity, _int(n),
                                            fixed=my_addr or None)
            my_addr = self.addrm.addr
            self.chan = mac.ChannelManager(self.num_channels, channel,
                                           rng=self.rng)
        else:
            self.identity = self.keys.public if self.keys is not None else None
            self.is_key = self.keys is not None
            self.chan = mac.ChannelManager(
                self.num_channels,
                channel if channel is not None
                else (int(my_addr) - 1) % self.num_channels, rng=self.rng)
        if self.keys is not None:
            self.keys.my_addr = my_addr

        self.link = link.LinkMux(my_addr, None if self.mesh else peer_addr,
                                 ack_timeout, max_retries, frag_size,
                                 queue_ahead, sym_rate, overhead,
                                 rx_dir=self.store_dir, window=window,
                                 filler=filler)
        # r6.1: closed-loop gain staging (None: the flowgraph has no gain
        # actuators, e.g. the protocol simulator without a level model).
        self.gain = gainlaw.GainControl(**gain) if gain is not None else None
        self._gain_sent = False
        self.chan.scorer = self._channel_score if self.mesh else None
        self.nick = (nick or ('Node %d' % my_addr)).strip()[:32] \
            or 'Node %d' % my_addr

        # End-to-end encryption. Fails CLOSED: if security was asked for and
        # the keys cannot be loaded, this node sends nothing but carrier
        # filler (static) or nothing at all (mesh) rather than plaintext.
        if self.keys is not None:
            try:
                self.sec = secure.SecureLink(
                    my_addr, self.keys, peers=[] if self.mesh else [peer_addr])
                self.link.seal_overhead = secure.SEAL_OVERHEAD
                self.link.tx_gate = (
                    lambda now, addr: self.sec.ready(addr, now))
            except (secure.KeyError_, RuntimeError, OSError) as exc:
                self.sec_error = str(exc)
        if self.sec_error is not None:
            self.link.tx_gate = lambda now, addr: False
            if self.mesh:
                self.link.radio.enabled = False

        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._inbox = deque()            # commands from the HTTP threads
        self._events = deque(maxlen=EVENT_BACKLOG)
        self._latest = {}                # snapshot events: only the newest
        self.logs = deque(maxlen=300)    # (timestamp, line) for the log drawer
        self._event_seq = 0
        self._closed = False

        self.messages = []               # transcript, oldest first
        self._by_mid = {}                # our own message id -> message
        self._rx_parts = {}              # (src, mid) -> {'t': float, 'parts': {}}
        self._files = OrderedDict()      # file token -> (path, mime), bounded
        self._file_seq = 0
        self._next_mid = 1
        self._file_jobs = deque()        # attachments waiting for their peer
        self._active_tx = {}             # peer address -> mid being sent
        self._rx_msgs = {}               # src -> inbound transfer being reassembled
        self._waiting = []               # texts for a node not in range yet

        # Peers. peers: pid -> entry; _by_addr: current address -> entry.
        self.peers = OrderedDict()
        self._by_addr = {}
        self._peers_dirty = True
        self._last_sweep = 0.0
        self.peer_nick = 'Node %d' % peer_addr if peer_addr else ''
        if not self.mesh:
            pub = (self.keys.peer_public(peer_addr)
                   if self.keys is not None else None)
            entry = self._new_peer('n%d' % peer_addr, pub)
            entry.update(addr=int(peer_addr) & 0xFF, nick=self.peer_nick,
                         trusted=pub is not None or self.sec is None)
            self._by_addr[entry['addr']] = entry

        self.radio = {'snr_db': None, 'level_db': None, 'evm': None,
                      'locked': False, 'ts': 0.0}
        self.radio_ch = [dict(self.radio) for _ in range(self.num_channels)]
        # r6.1: recent SNR readings per channel. A snapshot taken while that
        # channel's transmitter was silent (its channel check) reads ~-3 dB;
        # the median over FRESH seconds is what the link report carries.
        self._snr_hist = [deque(maxlen=48) for _ in range(self.num_channels)]
        self.ch_frames = [0] * self.num_channels
        self.ch_bad = [0] * self.num_channels
        self._src_chan = {}              # src address -> channel it beacons
        self._heard_at = {}              # src address -> last valid frame
        self._levels = None              # (t, [dB per channel]) not yet used
        self.ch_levels = [None] * self.num_channels
        self._beacons_sent = OrderedDict()   # raw beacon -> time handed out
        self._lag_warned = False
        self._lag_told = -1e9
        self._deaf_told = -1e9
        self.adc = {'peak_dbfs': None, 'rms_dbfs': None, 'clip': None,
                    'ts': 0.0}
        # r6.1: physical-layer facts reported by the flowgraph shim (O/U
        # counters, AFC, deframers, liveness, transmit framer, CPU, ...).
        self.phy = {}
        self._txen = None                # last transmitter on/off announced
        self._tx_on_since = None
        self._deaf_since = None          # r6.1 link-fault logic
        self._deaf_reset(None)
        self._last_clear = -1e9          # last check that saw a clear channel
        self._last_check_end = -1e9      # last check that ended, seen or not
        self._fault_told = {}
        self.telemetry_dir = telemetry_dir or None
        self._tele = None
        self._tele_next = 0.0
        self._tele_prev = None
        self._adc_bad_since = None
        self._adc_told = -1e9
        self._addr_changed_at = 0.0
        self.started = time.time()
        self._started_mac = False
        self._tx_channel = None          # last channel announced to the radio
        self._next_beacon = 0.0
        self._conflict_logged = set()
        self._last_stats_push = 0.0
        self._last_presence = 0.0
        self._rate = {'t': 0.0, 'tx': 0, 'rx': 0, 'tx_bps': 0.0, 'rx_bps': 0.0}

    # ------------------------------------------------------------ identity
    def _plain_identity(self):
        """Security off: a random, persistent 32-byte node id."""
        path = os.path.join(self.store_dir, 'node_id.json')
        try:
            with open(path) as fh:
                doc = json.load(fh)
            ident = bytes.fromhex(doc['identity'])
            if len(ident) == 32:
                return ident, doc.get('addr_n', 0)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        ident = os.urandom(32)
        self._save_plain_identity(ident, 0)
        return ident, 0

    def _save_plain_identity(self, ident, n):
        path = os.path.join(self.store_dir, 'node_id.json')
        try:
            tmp = path + '.tmp'
            with open(tmp, 'w') as fh:
                json.dump({'identity': ident.hex(), 'addr_n': n}, fh)
            os.replace(tmp, path)
        except OSError:
            pass

    def _save_addr_n(self, n):
        if self.keys is not None and hasattr(self.keys, 'set_state'):
            self.keys.set_state('addr_n', n)
        elif self.keys is None:
            self._save_plain_identity(self.identity, n)

    @property
    def my_addr(self):
        return self.link.my_addr

    @property
    def fingerprint(self):
        return (secure.fingerprint(self.identity)
                if self.identity is not None else None)

    # ------------------------------------------------------------ event bus
    def _emit(self, kind, **payload):
        """Queue one UI event. Safe to call with or without the lock held.

        r6.1: snapshot events (stats 4 Hz, view 1 Hz, the peer list) replace
        their predecessor instead of filling the backlog - a reconnecting
        page needs only the latest, and 2000 retained snapshots (constellation
        points, spectra) were ~100 MB of memory."""
        with self._cv:
            self._event_seq += 1
            payload['type'] = kind
            if kind in SNAPSHOT_EVENTS:
                self._latest[kind] = (self._event_seq, payload)
            else:
                self._events.append((self._event_seq, payload))
            self._cv.notify_all()

    def events_since(self, seq, timeout=25.0):
        """Block until an event newer than seq exists; return (seq, [events])."""
        deadline = time.time() + timeout
        with self._cv:
            while not self._closed:
                if self._event_seq > seq:
                    out = [e for e in self._events if e[0] > seq]
                    out += [e for e in self._latest.values() if e[0] > seq]
                    if out:
                        out.sort(key=lambda e: e[0])
                        return out[-1][0], [e[1] for e in out]
                remaining = deadline - time.time()
                if remaining <= 0:
                    return seq, []
                self._cv.wait(min(remaining, 1.0))
            return seq, []

    def current_seq(self):
        with self._lock:
            return self._event_seq

    def close(self):
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    # -------------------------------------------------------------- helpers
    def _new_mid(self):
        mid = self._next_mid
        self._next_mid = (self._next_mid + 1) & 0xFFFFFFFF or 1
        return mid

    def _register_file(self, path, mime=None):
        self._file_seq += 1
        token = 'f%d' % self._file_seq
        self._files[token] = (path, mime)
        while len(self._files) > MAX_FILE_TOKENS:
            self._files.popitem(last=False)      # oldest attachment URL expires
        return token

    def file_path(self, token):
        with self._lock:
            entry = self._files.get(token)
        return entry[0] if entry else None

    def file_entry(self, token):
        """(path, mime) for an attachment URL token, or (None, None)."""
        with self._lock:
            return self._files.get(token) or (None, None)

    def _add(self, msg):
        with self._lock:
            msg.setdefault('ts', time.time())
            msg.setdefault('state', 'info')
            self.messages.append(msg)
            if len(self.messages) > 5000:
                del self.messages[:1000]
            self._emit('message', message=msg)
        return msg

    def _touch(self, msg, **fields):
        with self._lock:
            msg.update(fields)
            self._emit('message', message=dict(msg))

    def _system(self, text, level='info', conv=None):
        with self._lock:
            return self._add({'id': 'sys%d' % (self._event_seq + 1),
                              'dir': 'sys', 'kind': 'system', 'text': text,
                              'level': level, 'conv': conv})

    def _log(self, out, text):
        self._consume(out, [('log', text)], 0.0)

    # ---------------------------------------------------------------- peers
    def _new_peer(self, pid, identity):
        entry = {'id': pid, 'identity': identity, 'addr': None,
                 'nick': '', 'channel': None, 'first': None, 'last': None,
                 'present': False, 'trusted': False, 'is_key': identity
                 is not None, 'fp': secure.fingerprint(identity)
                 if identity is not None else None}
        self.peers[pid] = entry
        self._peers_dirty = True
        return entry

    def _peer_for_addr(self, addr):
        return self._by_addr.get(addr)

    def _pid_for_addr(self, addr):
        entry = self._by_addr.get(addr)
        return entry['id'] if entry is not None else 'n%d' % (addr or 0)

    def _addr_of(self, conv):
        entry = self.peers.get(conv)
        return entry['addr'] if entry is not None else None

    def _default_conv(self):
        if not self.mesh:
            return next(iter(self.peers))
        return GROUP

    def _may_send(self, entry):
        """Encrypted: only to approved keys. Plain: to anyone."""
        if entry is None or entry['addr'] is None:
            return False
        if self.sec is not None:
            return bool(entry['trusted'])
        return self.sec_error is None

    def _release_addr(self, out, addr, why, now):
        """Forget the link and session state that belongs to an address.

        Used when a node moves to another address, another node takes one
        over, or a key is revoked. Messages still queued for it are failed
        rather than silently delivered to whoever holds the address next.
        """
        eng = self.link.drop_peer(addr)
        if self.sec is not None:
            self._sec_actions(out, self.sec.forget(addr), now)
        if eng is not None:
            mids = set()
            for entry in list(eng.outq.values()) + list(eng.txq_hi) \
                    + list(eng.txq):
                if entry.get('mid'):
                    mids.add(entry['mid'])
            if eng.tx_file is not None:
                mids.add(eng.tx_file.get('mid'))
            pid = self._pid_for_addr(addr)
            for mid in mids:
                self._rcpt_failed(mid, pid, why)
        if self._active_tx.pop(addr, None) is not None:
            self._start_next_file(out, now)
        with self._lock:
            for key in [k for k in self._rx_parts if k[0] == addr]:
                del self._rx_parts[key]
            msg = self._rx_msgs.pop(addr, None)
            if msg is not None:
                self._touch(msg, state='incomplete', note=why)

    def peers_payload(self, now=None):
        now = time.time() if now is None else now
        with self._lock:
            links = self.link.snapshot(now).get('links', {})
            rows = []
            for entry in self.peers.values():
                addr = entry['addr']
                row = {k: entry[k] for k in ('id', 'addr', 'nick', 'channel',
                                             'present', 'trusted', 'is_key',
                                             'fp')}
                row['age'] = (now - entry['last']) if entry['last'] else None
                row['security'] = (self.sec.peers[addr].state
                                   if self.sec is not None
                                   and addr in self.sec.peers else
                                   ('off' if self.sec is None else 'idle'))
                row['link'] = links.get(addr)
                # Whether its latest beacons list us: the other half of the
                # link, which our own receiver cannot measure.
                row['hears_me'] = (None if not entry.get('beacon_at')
                                   else bool(entry.get('hears_me')
                                             and now - entry['hears_me']
                                             < self._hears_me_window()))
                # r6.1 two-way table: "I hear X at a dB / X hears me at b dB"
                row['snr_in'], row['level_in'] = (
                    self._hear(addr, now) if addr is not None
                    else (None, None))
                row['snr_out'] = entry.get('snr_out')
                row['level_out'] = entry.get('level_out')
                x = entry.get('ext')
                row['their'] = ({'adc_peak': x.get('adc_peak'),
                                 'overloaded': x.get('overloaded'),
                                 'impaired': x.get('impaired'),
                                 'rx_gain': x.get('rx_gain'),
                                 'tx_atten': x.get('tx_atten'),
                                 'auto': x.get('auto')} if x else None)
                row['state'], row['reason'] = self.link_state(entry, now)
                p = (self.sec.peers.get(addr) if self.sec is not None
                     and addr is not None else None)
                row['unanswered'] = p.unanswered if p is not None else 0
                rows.append(row)
            return rows

    def _push_peers(self, now):
        if self._peers_dirty:
            self._peers_dirty = False
            self._emit('peers', peers=self.peers_payload(now))

    # ------------------------------------------------- outbound (UI thread)
    def submit(self, kind, **kw):
        """Thread-safe hand-off from an HTTP handler to the radio thread.

        Returns the message id so the browser can correlate its optimistic
        bubble with the one the radio thread creates a few milliseconds later.

        An attachment is staged to disk and fragmented HERE, on the calling
        thread, before anything is handed over. Done on the GNU Radio thread it
        blocks every message handler, which stops the transmit filler and
        starves the Pluto TX buffer - measured at 57 ms for an 8 MiB file
        against a 30 ms queue_ahead cushion.
        """
        with self._lock:
            mid = self._new_mid() if kind in ('text', 'file') else None
        if kind == 'file':
            kw = dict(self._prepare_attachment(mid, kw.get('name', 'file'),
                                               kw.get('mime', ''),
                                               kw.get('data', b'')) or {},
                      to=kw.get('to'))
            if 'data' not in kw:
                return mid
        with self._lock:
            self._inbox.append(dict(kw, kind=kind, mid=mid))
            self._cv.notify_all()
        return mid

    def _prepare_attachment(self, mid, name, mime, data):
        """Stage and fragment an attachment. Never call from the radio thread."""
        name = _safe_filename(name)
        mime = mime or mimetypes.guess_type(name)[0] or 'application/octet-stream'
        path = os.path.join(self.store_dir, 'tx_%d_%s' % (mid, name))
        try:
            with open(path, 'wb') as fh:
                fh.write(data)
        except OSError as exc:
            self._system('could not stage %s: %s' % (name, exc), 'error')
            return None
        frags, crc = link.segment(data, self.link.frag_size)
        with self._lock:
            meta = json.dumps({'n': self.nick, 'm': mime, 'i': mid,
                               't': round(time.time(), 3)},
                              separators=(',', ':')
                              ).encode('utf-8')[:link.MAX_FILE_META]
        items = link.build_file_items(len(data), len(frags), name, meta,
                                      crc, frags, mid)
        return {'name': name, 'mime': mime, 'data': data, 'path': path,
                'frags': frags, 'crc': crc, 'size': len(data),
                'meta': meta, 'items': items}

    def _sendfile_worker(self, path, to=None):
        """Read and fragment a /sendfile target off the radio thread."""
        try:
            with open(path, 'rb') as fh:
                data = fh.read()
        except OSError as exc:
            self._system('could not read %s: %s' % (path, exc), 'error')
            return
        self.submit('file', name=os.path.basename(path), mime='', data=data,
                    to=to)

    def _drain_inbox(self, out, now):
        while True:
            with self._lock:
                if not self._inbox:
                    return
                job = self._inbox.popleft()
            kind = job['kind']
            if kind == 'text':
                self._send_text(out, job['mid'], job.get('text', ''), now,
                                job.get('to'))
            elif kind == 'file':
                self._queue_attachment(out, job, now)
            elif kind == 'command':
                self._console(out, job.get('text', ''), now, job.get('to'))
            elif kind == 'trust':
                self._set_trust(out, job.get('peer'), bool(job.get('trust')),
                                now)
            elif kind == 'gain':
                self.gain_command(out, job.get('rx'), job.get('tx'),
                                  job.get('auto'))
            elif kind == 'nick':
                with self._lock:
                    self.nick = (job.get('nick') or self.nick).strip()[:32] \
                        or self.nick
                    self._emit('identity', nick=self.nick,
                               peer_nick=self.peer_nick)
                self._announce(out, now, force=True)

    def _targets(self, conv):
        """Peers a message to conv goes to, or (None, reason)."""
        if conv == GROUP:
            targets = [e for e in self.peers.values()
                       if e['present'] and self._may_send(e)]
            if not targets:
                return None, 'nobody approved is in range'
            return targets, None
        entry = self.peers.get(conv)
        if entry is None or entry['addr'] is None:
            return None, 'unknown node'
        if not self._may_send(entry):
            return None, ('its key has not been approved - compare the '
                          'fingerprint and press Trust'
                          if self.sec is not None else 'security error')
        return [entry], None

    def _absent(self, conv):
        """A 1:1 conversation whose node is not in range (yet): its
        messages wait for it instead of failing at once."""
        if conv == GROUP:
            return False
        entry = self.peers.get(conv)
        return entry is None or entry['addr'] is None

    def _send_text(self, out, mid, text, now, conv=None):
        text = (text or '').strip()
        if not text:
            return
        conv = conv or self._default_conv()
        with self._lock:
            wait = self._absent(conv)
            targets, why = (None, None) if wait else self._targets(conv)
            nick_len = len(self.nick.encode('utf-8', 'replace')[:32])
            chunk = max(32, self.link.frag_size - 9 - nick_len)
            raw = text.encode('utf-8')
            parts = [raw[i:i + chunk] for i in range(0, len(raw), chunk)] or [b'']
            if len(parts) > MAX_PARTS:
                parts = parts[:MAX_PARTS]
                self._system('message truncated to %d frames' % MAX_PARTS,
                             'warn', conv)
            rcpt = OrderedDict((e['id'], {'state': 'queued', 'tries': 1,
                                          'acked': 0, 'note': None,
                                          'nick': e['nick']})
                               for e in targets or ())
            msg = self._add({'id': mid, 'dir': 'out', 'kind': 'text',
                             'conv': conv, 'nick': self.nick,
                             'addr': self.link.my_addr, 'text': text,
                             'state': 'waiting' if wait else
                             'queued' if targets else 'failed',
                             'note': 'waiting for the node to come into range'
                             if wait else why,
                             'tries': 1, 'frames': len(parts),
                             'acked': 0, 'rcpt': rcpt,
                             'secure': self.sec is not None})
            self._by_mid[mid] = msg
            atype = AT_GROUP if conv == GROUP else AT_TEXT
            frames = [pack_envelope(atype, mid, i, len(parts), self.nick, p)
                      for i, p in enumerate(parts)]
            if wait:
                self._waiting.append({'mid': mid, 'conv': conv,
                                      'frames': frames,
                                      'until': now + WAIT_LIMIT})
        self._send_frames(out, mid, frames, targets, now)

    def _send_frames(self, out, mid, frames, targets, now):
        for entry in targets or ():
            for frame in frames:
                self._consume(out, self.link.send_data(frame, now, mid=mid,
                                                       dst=entry['addr']), now)

    def _flush_waiting(self, out, now):
        """Send what was waiting for a node that has now appeared."""
        if not self._waiting:
            return
        ready = []
        with self._lock:
            for item in list(self._waiting):
                msg = self._by_mid.get(item['mid'])
                if self._absent(item['conv']):
                    if now > item['until']:
                        self._waiting.remove(item)
                        if msg is not None:
                            self._touch(msg, state='failed',
                                        note='the node never came into range')
                    continue
                self._waiting.remove(item)
                targets, why = self._targets(item['conv'])
                if msg is None:
                    continue
                if not targets:
                    self._touch(msg, state='failed', note=why)
                    continue
                e = targets[0]
                msg['rcpt'] = OrderedDict([(e['id'], {
                    'state': 'queued', 'tries': 1, 'acked': 0, 'note': None,
                    'nick': e['nick']})])
                self._touch(msg, state='queued', note=None)
                ready.append((item, targets))
        for item, targets in ready:
            self._send_frames(out, item['mid'], item['frames'], targets, now)

    def _queue_attachment(self, out, job, now):
        """Radio thread: register an already-prepared attachment. No bulk work."""
        mid = job['mid']
        conv = job.get('to') or self._default_conv()
        with self._lock:
            token = self._register_file(job['path'], job['mime'])
            wait = self._absent(conv)
            targets, why = ((None, 'files go to one node at a time - pick a '
                                   'node, not the group')
                            if conv == GROUP else
                            (None, None) if wait else self._targets(conv))
            rcpt = OrderedDict((e['id'], {'state': 'queued', 'tries': 1,
                                          'acked': 0, 'note': None,
                                          'nick': e['nick']})
                               for e in targets or ())
            msg = self._add({'id': mid, 'dir': 'out', 'kind': 'file',
                             'conv': conv, 'nick': self.nick,
                             'addr': self.link.my_addr,
                             'state': 'waiting' if wait else
                             'queued' if targets else 'failed',
                             'note': 'waiting for the node to come into range'
                             if wait else why,
                             'tries': 1, 'rcpt': rcpt,
                             'secure': self.sec is not None,
                             'file': {'name': job['name'], 'mime': job['mime'],
                                      'size': job['size'],
                                      'url': '/file/' + token},
                             'progress': {'done': 0, 'total': 0}})
            self._by_mid[mid] = msg
            if targets or wait:
                self._file_jobs.append(dict(job, conv=conv,
                                            until=now + WAIT_LIMIT))
        self._start_next_file(out, now)

    def _start_next_file(self, out, now):
        """One transfer per peer at a time (a link engine has one tx_file);
        transfers to different peers run side by side."""
        started = []
        with self._lock:
            for job in list(self._file_jobs):
                entry = self.peers.get(job['conv'])
                addr = entry['addr'] if entry is not None else None
                msg = self._by_mid.get(job['mid'])
                if addr is None:
                    if now > job.get('until', now) and msg is not None:
                        self._file_jobs.remove(job)
                        self._touch(msg, state='failed',
                                    note='the node never came into range')
                    continue
                if not self._may_send(entry):
                    self._file_jobs.remove(job)
                    if msg is not None:
                        self._touch(msg, state='failed',
                                    note=self._targets(job['conv'])[1])
                    continue
                if addr in self._active_tx or self.link.tx_file_for(addr):
                    continue
                if msg is not None and not msg.get('rcpt'):
                    msg['rcpt'] = OrderedDict([(entry['id'], {
                        'state': 'queued', 'tries': 1, 'acked': 0,
                        'note': None, 'nick': entry['nick']})])
                    self._touch(msg, state='queued', note=None)
                self._file_jobs.remove(job)
                self._active_tx[addr] = job['mid']
                started.append((addr, job))
        for addr, job in started:
            meta = job.get('meta') or json.dumps(
                {'n': self.nick, 'm': job['mime'], 'i': job['mid'],
                 't': round(time.time(), 3)},
                separators=(',', ':')).encode('utf-8')[:link.MAX_FILE_META]
            prepared = ((job['frags'], job['crc'])
                        if job.get('frags') is not None else None)
            self._consume(out, self.link.send_file(
                job['name'], job['data'], now, meta=meta, mid=job['mid'],
                prepared=prepared, items=job.get('items'), dst=addr), now)

    def _announce(self, out, now, force=False):
        """Static mode: tell the peer our nickname, at most every few seconds.
        Mesh mode: the beacon carries it; send one now."""
        if self.mesh:
            if force:
                self._next_beacon = now
            return
        with self._lock:
            if not force and (now - self._last_presence) < PRESENCE_MIN_INTERVAL:
                return
            self._last_presence = now
            frame = pack_envelope(AT_PRESENCE, 0, 0, 1, self.nick, b'')
        self._consume(out, self.link.send_data(frame, now, label='presence'),
                      now)

    # --------------------------------------------------------------- trust
    def _set_trust(self, out, pid, trust, now):
        entry = self.peers.get(pid)
        if entry is None:
            self._system('no such node: %s' % pid, 'warn')
            return
        if self.sec is None or not hasattr(self.keys, 'trust'):
            self._system('approval applies to encrypted mesh mode only',
                         'warn', pid)
            return
        if not entry['is_key']:
            self._system('%s runs without encryption - it has no key to '
                         'approve' % (entry['nick'] or pid), 'warn', pid)
            return
        name = entry['nick'] or ''
        if trust:
            try:
                self.keys.trust(entry['identity'], name)
            except (OSError, ValueError) as exc:
                self._system('could not save the approval: %s' % exc, 'error',
                             pid)
                return
            entry['trusted'] = True
            if entry['addr'] is not None:
                self.sec.add_peer(entry['addr'])
                self.sec.peers[entry['addr']].next_try = now
            self._system('approved %s (key %s). A secure session is being '
                         'set up.' % (name or pid, entry['fp']), 'info', pid)
        else:
            still = not self.keys.revoke(entry['identity'])
            if still:
                self._system('%s is listed in roster.json, so it stays trusted '
                             '- remove it there (bpsk_keys.py remove)'
                             % (name or pid), 'warn', pid)
                return
            entry['trusted'] = False
            if entry['addr'] is not None:
                self._release_addr(out, entry['addr'], 'approval withdrawn',
                                   now)
            self._system('approval of %s withdrawn - nothing more is sent to '
                         'or accepted from it' % (name or pid), 'warn', pid)
        self._peers_dirty = True

    # --------------------------------------------------------------- console
    def _console(self, out, text, now, conv=None):
        """Slash commands typed into the composer."""
        text = (text or '').strip()
        if not text:
            return
        cmd = text.split(None, 1)[0].lower()
        arg = text[len(cmd):].strip()
        if cmd == '/nick':
            if arg:
                with self._lock:
                    self.nick = arg[:32]
                    self._emit('identity', nick=self.nick,
                               peer_nick=self.peer_nick)
                self._system('you are now "%s"' % self.nick)
                self._announce(out, now, force=True)
            else:
                self._system('usage: /nick <name>', 'warn')
        elif cmd == '/clear':
            with self._lock:
                self.messages = []
                self._emit('reset')
        elif cmd == '/sendfile':
            path = os.path.expanduser(arg)
            if not arg or not os.path.isfile(path):
                self._system('no such file: %s' % (arg or '<none>'), 'warn')
                return
            # Reading and fragmenting happen on a worker; this handler is on
            # the radio thread and must return immediately.
            threading.Thread(target=self._sendfile_worker, args=(path, conv),
                             daemon=True, name='bpsk-sendfile').start()
        elif cmd == '/secure':
            for line in self._secure_report(now):
                self._system(line)
                out.append(('log', line))
        elif cmd == '/peers':
            for line in self._peers_report(now):
                self._system(line)
                out.append(('log', line))
        elif cmd in ('/trust', '/untrust'):
            entry = self._find_peer(arg)
            if entry is None:
                self._system('usage: %s <address or nickname>' % cmd, 'warn')
            else:
                self._set_trust(out, entry['id'], cmd == '/trust', now)
        elif cmd == '/msg':
            who, _, body = arg.partition(' ')
            entry = GROUP if who == GROUP else self._find_peer(who)
            if entry is None or not body.strip():
                self._system('usage: /msg <address|nickname|all> <text>',
                             'warn')
            else:
                with self._lock:
                    mid = self._new_mid()
                self._send_text(out, mid, body, now,
                                GROUP if entry == GROUP else entry['id'])
        elif cmd == '/gain':
            # /gain rx 20 | /gain tx 60 | /gain (show)
            parts = arg.split()
            try:
                if len(parts) == 2 and parts[0] in ('rx', 'tx'):
                    kw = {'rx': None, 'tx': None}
                    kw[parts[0]] = float(parts[1])
                    self.gain_command(out, kw['rx'], kw['tx'], auto=False)
                elif self.gain is not None:
                    g = self.gain.snapshot()
                    self._system('gain control %s: RX gain %d dB, TX '
                                 'attenuation %d dB, ADC peak %s dBFS%s'
                                 % ('automatic' if g['auto'] else 'manual',
                                    g['rx_gain'], g['tx_atten'],
                                    '--' if g['adc_peak'] is None
                                    else '%.1f' % g['adc_peak'],
                                    (' - last change: ' + g['reason'])
                                    if g['reason'] else ''))
                else:
                    self._system('this node has no gain control', 'warn')
            except ValueError:
                self._system('usage: /gain [rx <dB> | tx <dB>]', 'warn')
        elif cmd == '/agc':
            if arg in ('on', 'off'):
                self.gain_command(out, auto=arg == 'on')
            else:
                self._system('usage: /agc on|off', 'warn')
        elif cmd in ('/stats', '/help', '/ping'):
            for action in self.link.on_user(text, now):
                if action[0] == 'log':
                    # Both places: the transcript, and the terminal - the GNU
                    # Radio edit box is the fallback console for when the
                    # browser is not available, and r5.1 printed nothing there.
                    self._system(action[1])
                    out.append(action)
                elif action[0] == 'evt':
                    self._on_link_event(out, action[1], now)
                else:
                    out.append(action)
            if cmd == '/help':
                self._system('mesh: /peers  /trust <node>  /untrust <node>  '
                             '/msg <node|all> <text>   radio: /gain [rx|tx '
                             '<dB>]  /agc on|off')
        else:
            self._system('unknown command %s' % cmd, 'warn')

    def _find_peer(self, key):
        key = (key or '').strip()
        if not key:
            return None
        if key in self.peers:
            return self.peers[key]
        if key.isdigit():
            return self._by_addr.get(int(key))
        for entry in self.peers.values():
            if entry['nick'] and entry['nick'].lower() == key.lower():
                return entry
        return None

    def _peers_report(self, now):
        mine = ('this node: address %d, channel %s, key %s'
                % (self.my_addr, self.chan.channel
                   if self.chan.channel is not None else '-',
                   self.fingerprint or '-'))
        lines = [mine]
        f = lambda v: '--' if v is None else '%.0f' % v       # noqa: E731
        for row in self.peers_payload(now):
            lines.append('  %-3s %-16s ch %s  %-7s %-9s %s  %s'
                         % (row['addr'] if row['addr'] is not None else '-',
                            (row['nick'] or '?')[:16],
                            row['channel'] if row['channel'] is not None
                            else '-',
                            'here' if row['present'] else 'away',
                            'trusted' if row['trusted'] else 'NEW',
                            row['security'], row['fp'] or ''))
            lines.append('      I hear it at %s dB / it hears me at %s dB'
                         ' - %s%s' % (f(row['snr_in']), f(row['snr_out']),
                                      row['state'], (': ' + row['reason'])
                                      if row['reason'] else ''))
        if len(lines) == 1:
            lines.append('  no other node heard yet')
        return lines

    def _secure_report(self, now):
        if self.sec is None:
            return ['security: %s' % ('ERROR - ' + self.sec_error
                                      if self.sec_error else 'OFF')]
        st = self.sec.stats
        lines = ['security: Noise_KK_25519_ChaChaPoly_SHA256, %s backend, '
                 'network "%s"' % (secure.C.BACKEND, self.keys.network),
                 'my key   %s' % self.sec.fingerprint()]
        for addr, p in sorted(self.sec.peers.items()):
            snap = self.sec.snapshot(now, addr)
            age = snap['session_age']
            lines.append('  %d: %s  key %s  session %s, %d established'
                         % (addr, snap['state'],
                            snap['peer_fp'] or 'NOT TRUSTED',
                            '%.0f s old' % age if age is not None else 'none',
                            snap['sessions']))
        lines.append('sealed tx %d  opened rx %d  auth failures %d  replays %d  '
                     'unencrypted dropped %d  handshakes tx/rx %d/%d'
                     % (st['sealed_tx'], st['opened_rx'], st['auth_fail'],
                        st['replay'], st['plain_dropped'], st['hs_tx'],
                        st['hs_rx']))
        return lines

    # ------------------------------------------------- radio-thread entries
    def on_console(self, text, now):
        """Text arriving from the GNU Radio edit box, kept for compatibility."""
        out = []
        text = (text or '').strip()
        if not text:
            return out
        if text.startswith('/'):
            self._console(out, text, now)
        else:
            with self._lock:
                mid = self._new_mid()
            self._send_text(out, mid, text, now)
        return out

    def on_rx(self, raw, now, channel=None):
        """One decoded frame. channel: index of the receive channel it came
        from (None if the flowgraph has a single receiver)."""
        out = []
        if channel is not None and not 0 <= channel < self.num_channels:
            channel = None
        try:
            frm = link.parse(raw)
        except link.BadFrame:
            self.link.count_rx(False)
            if channel is not None:
                self.ch_bad[channel] += 1
                if channel != self.chan.channel or not self.chan.tx_enabled:
                    self.chan.bad(channel, now)
            return out
        self.link.count_rx(True)
        src, ftype = frm['src'], frm['type']
        own = src == self.link.my_addr
        if not own:
            self._heard_at[src] = now
        if channel is not None:
            self.ch_frames[channel] += 1
            beaconed = self._src_chan.get(src)
            # A node heard on a channel other than the one it announces is
            # adjacent-channel leakage from a strong transmitter nearby, not
            # a second occupant of that channel.
            if not own and (beaconed is None or beaconed == channel):
                self.chan.heard(channel, src, now)
        if own and channel is not None and channel == self.chan.channel:
            self.chan.own_echo(now)
            sent = self._beacons_sent.pop(bytes(raw), None)
            if sent is not None:
                # Our own beacon, back through the whole TX and RX chain.
                self.chan.echo_latency(now - sent)
                self.link.mstats['rx_self'] += 1
                return out
        if ftype == link.FT_BEACON and frm['dst'] == link.BROADCAST:
            self.link.mstats['beacon_rx'] += 1
            if self.mesh:
                self._on_beacon(out, frm, channel, now)
            return out
        if own:
            self.link.mstats['rx_self'] += 1
            return out
        entry = self._by_addr.get(src)
        if entry is not None:
            entry['last'] = now
            if not entry['present']:
                self._set_present(entry, True)
        if self.sec is not None:
            self._sec_actions(out, self.sec.accept(frm, now), now)
        elif self.sec_error is not None:
            if ftype == link.FT_IDLE and frm['dst'] == link.BROADCAST:
                # Keys unusable: carrier presence only, nothing else accepted.
                self._consume(out, self.link.on_frame(frm, now), now)
        else:
            self._consume(out, self.link.on_frame(frm, now), now)
        return out

    def _on_beacon(self, out, frm, channel, now):
        b = mac.unpack_beacon(frm['payload'])
        if b is None:
            return
        src, ident = frm['src'], b['identity']
        if ident == self.identity:
            return                      # our own, or a copy of it
        if self.my_addr in b['neighbours']:
            self.chan.heard_by(now)
        if 0 <= b['channel'] < self.num_channels:
            self._src_chan[src] = b['channel']
            # The beacon says which channel its sender occupies. If that is
            # ours, the larger identity moves (both nodes decide the same way
            # independently); the smaller one ignores the claim, and its own
            # periodic silence check still catches a one-sided collision.
            ours = (b['channel'] == self.chan.channel
                    and self.chan.state in ('active', 'silent'))
            if not ours or ident < self.identity:
                self.chan.heard(b['channel'], src, now)
        ev = self.addrm.on_beacon(src, ident, active=self.chan.claims > 0)
        if ev is not None and ev[0] == 'readdress':
            self._readdress(out, ev[1], ev[2], now)
        elif ev is not None and ev[0] == 'conflict' \
                and src not in self._conflict_logged:
            self._conflict_logged.add(src)
            self._log(out, 'address %d is also used by another node (key %s)'
                           ' - it will move' % (src, secure.fingerprint(ident)))

        pid = peer_id(ident)
        with self._lock:
            entry = self.peers.get(pid)
            if entry is None:
                entry = self._new_peer(pid, ident)
                entry['first'] = now
                entry['is_key'] = b['is_key']
                if not b['is_key']:
                    entry['fp'] = None
                new = True
            else:
                new = False
            if entry['addr'] != src:
                old = entry['addr']
                if old is not None and self._by_addr.get(old) is entry:
                    del self._by_addr[old]
                    self._release_addr(out, old, 'the node moved from address '
                                                 '%d to %d' % (old, src), now)
                    if self.keys is not None and hasattr(self.keys, 'unbind'):
                        self.keys.unbind(old)
                other = self._by_addr.get(src)
                if other is not None and other is not entry:
                    other['addr'] = None
                    self._release_addr(out, src, 'another node took over its '
                                                 'address', now)
                entry['addr'] = src
                self._by_addr[src] = entry
                self._peers_dirty = True
            if entry['nick'] != b['nick'] or entry['channel'] != b['channel']:
                entry['nick'] = b['nick']
                entry['channel'] = b['channel']
                self._peers_dirty = True
            entry['last'] = entry['beacon_at'] = now
            if self.my_addr in b['neighbours']:
                entry['hears_me'] = now
            x = b.get('ext')
            entry['ext'] = x
            mine = (x.get('links') or {}).get(self.my_addr) if x else None
            new_out = mine.get('snr') if mine else None
            if entry.get('snr_out') != new_out:
                self._peers_dirty = True
            entry['snr_out'] = new_out
            entry['level_out'] = mine.get('level') if mine else None
            hist = entry.setdefault('snr_out_hist', deque(maxlen=3))
            hist.append(new_out)
            if b['is_key'] and self.keys is not None \
                    and hasattr(self.keys, 'bind'):
                self.keys.bind(src, ident, b['nick'])
            trusted = (self.keys.is_trusted(ident) if self.sec is not None
                       and b['is_key'] else self.sec is None)
            if trusted != entry['trusted']:
                entry['trusted'] = trusted
                self._peers_dirty = True
            if not entry['present']:
                self._set_present(entry, True)
        if trusted and self.sec is not None and src not in self.sec.peers:
            p = self.sec.add_peer(src)
            p.next_try = now
        if new:
            if self.sec is None:
                note = ('%s is in range (address %d, channel %d)'
                        % (b['nick'] or 'a node', src, b['channel']))
            elif not b['is_key']:
                note = ('%s is in range but runs WITHOUT encryption - this '
                        'node will not talk to it' % (b['nick'] or 'a node'))
            elif trusted:
                note = ('%s is in range (address %d, key %s, approved)'
                        % (b['nick'] or 'a node', src, entry['fp']))
            else:
                note = ('NEW node %s in range - key fingerprint %s. Compare '
                        'it with that node\'s own screen, then press Trust.'
                        % (b['nick'] or '?', entry['fp']))
            self._system(note, 'info' if trusted else 'warn', pid)

    def _set_present(self, entry, present):
        entry['present'] = present
        self._peers_dirty = True
        if self.sec is not None and entry['addr'] is not None \
                and entry['trusted'] and self.mesh:
            if present:
                self.sec.add_peer(entry['addr'])
            else:
                self.sec.pause(entry['addr'])

    def _readdress(self, out, old, new, now):
        self.link.set_address(new)
        if self.keys is not None:
            self.keys.my_addr = new
        if self.sec is not None:
            self._sec_actions(out, self.sec.set_address(new, now), now)
        self._save_addr_n(self.addrm.n)
        self._next_beacon = now
        self._addr_changed_at = now
        self._log(out, 'address %d is taken by another node - this node is '
                       'now %d' % (old, new))
        self._system('this node moved from address %d to %d (address '
                     'collision; resolved automatically)' % (old, new), 'warn')
        self._emit('identity', nick=self.nick, peer_nick=self.peer_nick,
                   my_addr=new)

    def banner(self):
        text = self.link.banner()
        if self.mesh:
            text += ('\nmesh: address %d (derived from %s), %d channels at '
                     '%.0f kHz spacing, channel %s'
                     % (self.my_addr, 'this node\'s key' if self.is_key
                        else 'node_id.json', self.num_channels,
                        self.ch_spacing / 1e3,
                        'automatic' if self.chan.fixed is None
                        else self.chan.fixed))
        else:
            text += '\nstatic: channel %d' % self.chan.channel
        if self.sec is not None:
            if self.mesh:
                text += ('\nsecurity: ON  Noise_KK_25519_ChaChaPoly_SHA256 [%s]'
                         '  my key %s  %d approved key(s)  (keys in %s)'
                         % (secure.C.BACKEND, self.sec.fingerprint(),
                            len(self.keys.trusted) + len(self.keys.roster_keys),
                            self.keys_dir))
                if self.keys.adopted:
                    text += ('\nusing the r5 key %s as this node\'s identity'
                             % self.keys.adopted)
            else:
                peer = self.link.peer_addr
                text += ('\nsecurity: ON  Noise_KK_25519_ChaChaPoly_SHA256 [%s]'
                         '  my key %s  peer %d key %s  (keys in %s)'
                         % (secure.C.BACKEND, self.sec.fingerprint(), peer,
                            self.sec.fingerprint(peer) or 'NOT IN ROSTER',
                            self.keys_dir))
        elif self.sec_error is not None:
            text += ('\nsecurity: ERROR - %s\nnothing will be transmitted '
                     'until this is fixed (or set security=False)'
                     % self.sec_error)
        else:
            text += ('\nsecurity: OFF - frames are sent in the clear')
        return text

    def on_tick(self, now):
        out = []
        self._drain_inbox(out, now)
        self._mac_tick(out, now)
        self._check_adc(out, now)
        self._gain_tick(out, now)
        if self.chan.echo:
            # Handshake retries and ACK timers wait for the measured round
            # trip (r6.0.1 used fixed timers: on a slow host every frame
            # timed out before its ACK could arrive).
            if self.sec is not None:
                self.sec.rtt = 2.0 * max(self.chan.echo)
            if hasattr(self.link, 'set_rx_latency'):
                self.link.set_rx_latency(max(self.chan.echo))
        self._flush_waiting(out, now)
        if self.sec is not None:
            self._sec_actions(out, self.sec.on_tick(now), now)
        self._consume(out, self.link.on_tick(now), now)
        if now - self._last_sweep >= PEER_SWEEP:
            self._last_sweep = now
            self._sweep_peers(now)
        self._start_next_file(out, now)
        self._sweep_rx_parts(now)
        with self._lock:
            self._push_peers(now)
        self._push_stats(now)
        self._telemetry(now)
        return out

    def _mac_tick(self, out, now):
        """Channel state machine, radio on/off, beacons."""
        if self.mesh and self.sec_error is not None:
            self.link.radio.enabled = False
            return
        if not self._started_mac:
            self._started_mac = True
            self.chan.start(now)
        with self._lock:
            levels, self._levels = self._levels, None
        if levels is not None:
            self.ch_levels = list(levels[1])        # for the UI, always
            self.chan.energy(levels[1], levels[0])  # the MAC, when quiet
        for ev, info in self.chan.tick(now):
            if ev == 'claim':
                self._log(out, 'transmitting on channel %d (%+.0f kHz)'
                          % (info['channel'], self.offset(info['channel'])
                             / 1e3))
                self._next_beacon = now
                if self.sec is not None:
                    self.sec.nudge(now)
            elif ev == 'collision':
                why = info['with']
                what = {'energy': 'another transmitter (power on it while '
                                  'this node was silent)',
                        'garbled': 'another transmitter (frames failing CRC '
                                   'while this node was silent)',
                        'deaf': 'nobody hears this node on it'}.get(
                    why, 'node %s' % why)
                echo = self.chan.echo
                self._log(out, 'channel %d: %s - choosing another [echo '
                               '%s, levels %s]'
                          % (info['channel'], what,
                             '%.0f ms' % (1e3 * max(echo)) if echo else '-',
                             self.chan.levels))
                self._system('channel %d is shared with %s - moving to '
                             'another channel' % (info['channel'], what),
                             'warn')
            elif ev == 'full':
                self._log(out, 'every channel is in use - waiting for one to '
                               'become free (receiving continues)')
                self._system('every channel is taken: this node can receive '
                             'but not transmit until one frees up', 'error')
            elif ev == 'clear':
                self._last_check_end = now
                if info.get('observed', True):
                    self._last_clear = now
                    if info.get('extended'):
                        self._last_long_clear = now
                if self.sec is not None:
                    self.sec.nudge(now)
        if self.mesh and self.chan.state in ('active', 'silent') \
                and self.chan.fixed is None:
            # r6.1: a node that hears no neighbour at all has no link a
            # longer check could hurt - and it may share its channel with a
            # node it cannot decode (two nodes alone on one channel decode
            # nothing from each other, and nobody is there to be deaf to).
            # It runs the deafness checks (spread, repeated, extended by the
            # address bits), and its checks last until they have seen the
            # channel. Not in its first seconds on a channel: there a node
            # that hears nobody yet is merely new, and long checks would
            # only delay its first sessions.
            echo = max(self.chan.echo) if self.chan.echo else 0.0
            self._alone = not any(
                e.get('beacon_at') and now - e['beacon_at'] < 3.0
                for e in self.peers.values()) \
                and self.chan.claimed_at is not None \
                and now - self.chan.claimed_at > ALONE_AFTER + 2.0 * echo
            self.chan.must_see = self._alone
            self._deafness(out, now)
        self._link_faults(out, now)
        lag = self.chan.slow
        if lag != self._lag_warned:
            self._lag_warned = lag
            if lag and now - self._lag_told > 60.0:
                self._lag_told = now
                text = ('this node hears its own transmission %.1f s late: the '
                        'computer is not keeping up with the sample stream '
                        '(CPU). Expect slower replies; %s until it recovers.'
                        % (max(self.chan.echo),
                           'channel checks rely on decoded frames only'
                           if self.chan.lagging else
                           'channel checks stay silent longer'))
                self._log(out, text)
                self._system(text, 'error')
        self.link.radio.enabled = self.chan.tx_enabled
        if self.chan.tx_enabled != self._txen:
            self._txen = self.chan.tx_enabled
            self._tx_on_since = now if self._txen else None
            out.append(('txen', self._txen))
        if self.chan.tx_enabled and self.chan.channel != self._tx_channel:
            self._tx_channel = self.chan.channel
            out.append(('chan', self.chan.channel))
        if self.mesh and self.chan.tx_enabled and now >= self._next_beacon:
            self._next_beacon = now + mac.BEACON_PERIOD * self.rng.uniform(
                0.9, 1.1)
            heard = sorted((t, a) for a, t in self._heard_at.items()
                           if now - t < mac.NEIGHBOUR_WINDOW)
            order = [a for _, a in reversed(heard)]
            payload = mac.pack_beacon(self.chan.channel, self.addrm.n,
                                      self.identity, self.nick, self.is_key,
                                      order, self._ext(now, order))
            sent = self.link.send_beacon(payload, now)
            for kind, frame in sent:
                if kind == 'tx':
                    self._beacons_sent[bytes(frame)] = now
            while len(self._beacons_sent) > 8:
                self._beacons_sent.popitem(last=False)
            out.extend(sent)

    def _deaf_reset(self, now):
        self._deaf_since = None
        self._deaf_heard = None          # heard again since (episode ending)
        self._deaf_tries = 0             # checks run in this episode
        self._deaf_clears = 0            # ... that saw a clear channel
        self._deaf_long = 0              # ... of which extended
        self._deaf_due = None            # when the next check is to start
        self._deaf_check_at = None       # when the last one was asked for
        self._last_long_clear = -1e9

    def _deafness(self, out, now):
        """r6.1: nobody hears this node. A collision is checked for, not
        assumed: if a silence check finds another transmitter the channel
        manager moves, as for any collision; if the channel is clear the
        fault is in the link, and the node reports it and stays - hopping
        cannot fix an overloaded receiver or a missing antenna (r6.0.1:
        endless 'nobody hears this node' channel changes).

        Two nodes that claimed one channel are deaf for the same reason and
        from the same moment, so their checks must not coincide: each one
        stays silent until the other has come back, and the other is not
        heard. So the checks of one episode start at random over a spread
        longer than a check, they repeat (backing off) while the node stays
        deaf, and every other one - chosen by the bits of this node's
        address, which differ between any two nodes - stays silent long
        enough to see a node that ran a plain check at the same time come
        back on the air. A link fault is concluded only from checks that
        actually observed the channel, one of them extended (the 2-h bench
        soak, 1/32 speed: two pairs of nodes, each pair on one channel,
        reported link faults for minutes and never parted)."""
        if self.chan.state != 'active':
            return
        if self._deaf_since is not None and \
                (self.chan.claimed_at or 0.0) > self._deaf_since:
            self._deaf_reset(now)            # a new channel since
        alone = getattr(self, '_alone', False)
        if not (alone or self._deaf(now)):
            if self._deaf_since is not None:
                if self._deaf_heard is None:
                    self._deaf_heard = now
                elif now - self._deaf_heard > self._hears_me_window():
                    self._deaf_reset(now)
            return
        self._deaf_heard = None
        spread = 1.0 + self.chan.check_length()
        if self._deaf_since is None:
            self._deaf_since = now
            self._deaf_due = now + self.rng.uniform(0.0, spread)
            if alone:
                self._log(out, 'no other node heard on any channel - checking '
                               'channel %d for one this node cannot decode'
                          % self.chan.channel)
            else:
                self._log(out, 'no node in range hears this node on channel '
                               '%d - checking the channel for another '
                               'transmitter' % self.chan.channel)
        if self._deaf_due is not None:
            if now >= self._deaf_due:
                echo = max(self.chan.echo) if self.chan.echo else 0.0
                bit = (int(self.my_addr or 0) >> (self._deaf_tries % 8)) & 1
                self.chan.extend_next = (spread + echo + 2.5) if bit else 0.0
                self.chan.next_check = min(self.chan.next_check, now)
                self._deaf_tries += 1
                self._deaf_due = None
                self._deaf_check_at = now
            return
        if self._last_check_end <= self._deaf_check_at:
            return                           # that check is still running
        if self._last_clear > self._deaf_check_at:
            self._deaf_clears += 1
            self._deaf_long += self._last_long_clear > self._deaf_check_at
        self._deaf_check_at = now            # counted; next one:
        self._deaf_due = now + self.rng.uniform(spread, 2.0 * spread) * min(
            4, 2 ** max(0, self._deaf_tries - 1))
        if (self._deaf_clears >= 2 and self._deaf_long >= 1
                or self._deaf_clears >= 4) and not alone \
                and now - self._deaf_told > LINK_FAULT_EVERY:
            self._deaf_told = now
            rows = [e for e in self.peers.values() if e.get('beacon_at')
                    and now - e['beacon_at'] < 2.5]
            why = '; '.join(self._why_unheard(e, now) for e in rows)
            text = ('LINK FAULT: no node in range can hear this node, and '
                    'channel %d is clear (%d silence checks) - not a '
                    'collision, so the channel is kept. %s.'
                    % (self.chan.channel, self._deaf_clears,
                       why or 'no reports'))
            self._log(out, text)
            self._system(text, 'error')

    def _link_faults(self, out, now):
        """Per peer: a one-way link (we hear it, it does not hear us) is
        reported, with the reason, at most every LINK_FAULT_EVERY s."""
        for e in self.peers.values():
            addr = e.get('addr')
            p = self.sec.peers.get(addr) if (self.sec is not None
                                             and addr is not None) else None
            state, why = self.link_state(e, now)
            if p is not None:
                p.slow = state == 'cant-hear-you'
            if state != 'cant-hear-you':
                continue
            # Not before this node has been on air long enough to be heard,
            # nor for a node that only just appeared.
            on_air = (self.chan.tx_enabled and self.chan.claimed_at is not None
                      and now - self.chan.claimed_at > 2 * HEARS_ME) \
                or (self.chan.fixed is not None and self.chan.tx_enabled
                    and now - self.started > 2 * HEARS_ME)
            if not on_air or now - (e.get('first') or now) < 2 * HEARS_ME:
                continue
            last = self._fault_told.get(e['id'], -1e9)
            if now - last < LINK_FAULT_EVERY:
                continue
            self._fault_told[e['id']] = now
            snr, _ = self._hear(addr, now)
            text = ('%s cannot hear this node (this node hears it%s): %s'
                    % (e.get('nick') or addr, '' if snr is None
                       else ' at %.0f dB' % snr, why))
            self._log(out, 'link fault: ' + text)
            self._system(text, 'warn', e['id'])

    def _hears_me_window(self):
        """How long a neighbour may go without listing this node before the
        link counts as one-way. HEARS_ME (5 s) covers a lost beacon or two
        and a 2 s silence check on either side. On a lagging host checks last
        longer (bpsk_mac.silence_max: echo + 0.65 s, up to 8 s) - this
        node's, and most likely the neighbour's too (same software, similar
        host) - while a neighbour drops a node from its list after 3 s
        without a frame from it. So the window grows by the extra length of
        both checks: unchanged at a normal echo, 5 -> 8 s at the bench's
        ~3 s. Without it the bench showed, every few minutes, one second of
        'can't hear you' and a logged link fault whenever a check on one
        side was followed by one on the other."""
        extra = max(0.0, self.chan.silence_max() - mac.SILENCE_MAX)
        return HEARS_ME + 2.0 * extra

    def _deaf(self, now):
        """True if every node beaconing around us has, for a while, failed to
        list us among the nodes it hears."""
        # r6.1: never sooner than a listing can come back - our transmit
        # pipeline, theirs to receive, a beacon period, theirs to transmit,
        # ours to receive (about 2 x echo + 1 s); on a lagging host the
        # plain 4 s fired at every start-up and after every move
        window = max(self.chan.deaf_window(),
                     self._hears_me_window() - HEARS_ME + mac.DEAF_AFTER,
                     2.0 * max(self.chan.echo) + 2.0 * mac.BEACON_PERIOD
                     if self.chan.echo else 0.0)
        ref = max(self.chan.claimed_at or 0.0, self._addr_changed_at)
        if now - ref < window:
            return False
        recent = [e for e in self.peers.values()
                  if e.get('beacon_at') and now - e['beacon_at'] < 2.5]
        return bool(recent) and all(now - e.get('hears_me', -1e9) > window
                                    for e in recent)

    def offset(self, k):
        return mac.channel_offset(k, self.num_channels, self.ch_spacing,
                                  self.ch_first)

    # ------------------------------------------------------- r6.1 helpers
    def _planned_channel(self, nick):
        """A lab 'static plan': {"channels": {"<fingerprint|nickname>": k}}.
        Matched on this node's key fingerprint (spaces optional) or nickname."""
        try:
            with open(self.plan_file) as fh:
                doc = json.load(fh)
            table = doc.get('channels') or {}
            fp = (secure.fingerprint(self.keys.public).replace(' ', '')
                  if self.keys is not None else None)
            for key, k in table.items():
                k = int(k)
                if not 0 <= k < self.num_channels:
                    continue
                if (fp and str(key).replace(' ', '').lower() == fp) or \
                        (nick and str(key) == nick):
                    self.plan_note = ('channel %d from the plan %s'
                                      % (k, self.plan_file))
                    return k
            self.plan_note = ('not in the plan %s - choosing a channel '
                              'automatically' % self.plan_file)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            self.plan_note = 'plan %s unusable: %s' % (self.plan_file, exc)
        return None

    def _reading(self, k, now):
        """(snr_db, level_db) of receive channel k, or (None, None)."""
        if k is None or not 0 <= k < self.num_channels:
            return None, None
        r = self.radio_ch[k]
        if not r.get('ts') or now - r['ts'] > FRESH:
            return None, None
        vals = sorted(v for t, v in self._snr_hist[k]
                      if v is not None and now - t <= FRESH)
        snr = vals[len(vals) // 2] if vals else r.get('snr_db')
        return snr, r.get('level_db')

    def _hear(self, addr, now):
        """How well this node hears a neighbour: its channel's reading, if
        the neighbour is the one beaconing that channel."""
        k = self._src_chan.get(addr)
        if k is None or k == self.chan.channel:
            return None, None
        return self._reading(k, now)

    def _overloaded(self, now):
        a = self.adc
        return bool(a.get('clip') is not None and now - a.get('ts', 0) < 3.0
                    and a['clip'] >= OVERLOAD_CLIP)

    def _impaired(self, now):
        live = self.phy.get('live') or {}
        return bool(live.get('fault')) or bool(self.chan.slow)

    def _ext(self, now, heard):
        """This node's link report for its beacon."""
        flags = 0
        if self.gain is not None and self.gain.auto:
            flags |= mac.EF_AUTO
        if self._overloaded(now):
            flags |= mac.EF_OVERLOAD
        if self._impaired(now):
            flags |= mac.EF_IMPAIRED
        links = []
        for a in heard:
            snr, lvl = self._hear(a, now)
            links.append((a, snr, lvl))
        g = self.gain
        return mac.pack_ext(flags, self.adc.get('peak_dbfs'),
                            g.rx_gain if g else 0, g.tx_atten if g else 0,
                            self.ch_levels, links)

    def _channel_score(self, k):
        """Higher = better place to transmit: minus the strongest level any
        neighbour's receiver reports on channel k (interference it would
        add to our signal there). None without r6.1 neighbours."""
        worst = None
        for e in self.peers.values():
            x = e.get('ext')
            if not x or not e.get('present'):
                continue
            lv = x.get('levels') or []
            if k < len(lv) and lv[k] is not None:
                worst = lv[k] if worst is None else max(worst, lv[k])
        return None if worst is None else -float(worst)

    def link_state(self, entry, now):
        """(state, reason) of the link to one peer, for the UI."""
        addr = entry.get('addr')
        if not entry.get('present'):
            return 'away', 'not heard for %d s' % PEER_TIMEOUT
        if self.sec is not None and entry.get('is_key') \
                and not entry.get('trusted'):
            return 'not-approved', 'not approved here - compare its ' \
                'fingerprint, then press Trust'
        if self.sec is not None and not entry.get('is_key'):
            return 'no-encryption', 'it runs without encryption'
        heard_me = (entry.get('beacon_at') and entry.get('hears_me')
                    and now - entry['hears_me'] < self._hears_me_window())
        if entry.get('beacon_at') and not heard_me:
            return 'cant-hear-you', self._why_unheard(entry, now)
        if self.sec is not None and addr in self.sec.peers:
            p = self.sec.peers[addr]
            if p.current is not None:
                return 'ok', None
            if p.unanswered >= 3:
                return 'no-response', ('%d handshakes unanswered - has %s '
                                       'approved this node?'
                                       % (p.unanswered,
                                          entry.get('nick') or 'it'))
            return 'securing', 'setting up the session'
        return 'ok', None

    def _why_unheard(self, entry, now):
        name = entry.get('nick') or 'it'
        x = entry.get('ext')
        if not x:
            return '%s does not list this node in its beacons' % name
        if x.get('overloaded'):
            return ('%s\'s receiver is overloaded (ADC %s dBFS) - its gain '
                    'control should fix that' % (name, x.get('adc_peak')))
        if x.get('impaired'):
            return '%s reports a receiver fault or a slow host' % name
        k = self.chan.channel
        lv = x.get('levels') or []
        if k is not None and k < len(lv) and lv[k] is not None:
            known = [v for v in lv if v is not None]
            floor = min(known) if known else lv[k]
            if lv[k] < floor + mac.ENERGY_MARGIN:
                return ('%s sees no signal on channel %d (%d dBFS, its floor '
                        '%d) - check this node\'s TX antenna and attenuation'
                        % (name, k, lv[k], floor))
            return ('%s sees this node\'s carrier on channel %d (%d dBFS) '
                    'but cannot decode it' % (name, k, lv[k]))
        return '%s does not list this node in its beacons' % name

    def publish_view(self, view):
        """Constellation and spectrum snapshots for the browser, about once
        a second - a separate event, so the 4 Hz stats stay small."""
        self._emit('view', view=view)

    def set_phy(self, key, value):
        """Physical-layer facts from the flowgraph shim (any JSON value)."""
        with self._lock:
            self.phy[key] = value

    def gain_command(self, out, rx=None, tx=None, auto=None):
        """Manual override. Returns the actions (also appended to out)."""
        acts = []
        if self.gain is None:
            self._system('this node has no gain control', 'warn')
            return acts
        if auto is not None:
            self.gain.auto = bool(auto)
        for what, value, why in self.gain.set_manual(rx, tx):
            acts.append(('gain', {'what': what, 'value': value,
                                  'reason': why}))
        if auto is not None or acts:
            g = self.gain
            self._system('gain control %s - RX gain %d dB, TX attenuation '
                         '%d dB' % ('automatic' if g.auto else 'MANUAL',
                                    g.rx_gain, g.tx_atten))
        out.extend(acts)
        return acts

    def _gain_tick(self, out, now):
        g = self.gain
        if g is None:
            return
        if not self._gain_sent:
            # Put the radio where the controller thinks it is.
            self._gain_sent = True
            out.append(('gain', {'what': 'rx', 'value': g.rx_gain,
                                 'reason': 'start'}))
            out.append(('gain', {'what': 'tx', 'value': g.tx_atten,
                                 'reason': 'start'}))
        reports = []
        for e in self.peers.values():
            x = e.get('ext')
            if not x or not e.get('beacon_at') or \
                    now - e['beacon_at'] > 3.0:
                continue
            # median of its last three reports: one taken while this node
            # was in a channel check must not move the transmitter
            hist = [v for v in e.get('snr_out_hist') or () if v is not None]
            snr = sorted(hist)[len(hist) // 2] if len(hist) * 2 > len(
                e.get('snr_out_hist') or ()) else None
            reports.append({'snr': snr,
                            'impaired': bool(x.get('overloaded')
                                             or x.get('impaired'))})
        for what, value, why in g.step(now, reports):
            out.append(('gain', {'what': what, 'value': value,
                                 'reason': why}))
            self._log(out, '%s -> %d dB (%s)'
                      % ('RX gain' if what == 'rx' else 'TX attenuation',
                         value, why))
        # r6.1: the channel manager's own-level reference moves with the
        # RX gain (automatic or manual change)
        last = getattr(self, '_rx_gain_last', None)
        if last is not None and g.rx_gain != last and \
                hasattr(self.chan, 'gain_changed'):
            self.chan.gain_changed(now, g.rx_gain - last)
        self._rx_gain_last = g.rx_gain

    def _sweep_peers(self, now):
        with self._lock:
            for entry in self.peers.values():
                if entry['present'] and entry['last'] is not None \
                        and now - entry['last'] > PEER_TIMEOUT:
                    self._set_present(entry, False)

    def _sweep_rx_parts(self, now):
        """Drop multi-part messages whose missing part is never coming.

        A part dropped after max_retries leaves the rest of the message parked
        in _rx_parts for ever: it never renders and is never freed. Anything
        untouched for PART_TIMEOUT is abandoned and reported.
        """
        with self._lock:
            stale = [k for k, e in self._rx_parts.items()
                     if (now - e['t']) > PART_TIMEOUT]
            for key in stale:
                entry = self._rx_parts.pop(key)
                self._system('incomplete message from %d abandoned (%d parts '
                             'arrived, one never did)' % (key[0], len(entry['parts'])),
                             'warn', self._pid_for_addr(key[0]))

    def set_radio_metrics(self, snr_db=_UNSET, level_db=_UNSET, evm=_UNSET,
                          locked=_UNSET, channel=None, t=None):
        """Called by the flowgraph shim with physical-layer measurements.

        Passing None clears a reading - that is how the shim reports that the
        receiver has gone quiet, as opposed to simply not measuring it.
        channel: which receive channel (r6); None = the single r5 receiver.
        """
        with self._lock:
            r = self.radio
            if channel is not None and 0 <= channel < self.num_channels:
                r = self.radio_ch[channel]
            if snr_db is not _UNSET:
                r['snr_db'] = snr_db
                if channel is not None and 0 <= channel < self.num_channels:
                    self._snr_hist[channel].append(
                        (time.time() if t is None else t, snr_db))
            if level_db is not _UNSET:
                r['level_db'] = level_db
            if evm is not _UNSET:
                r['evm'] = evm
            if locked is not _UNSET:
                r['locked'] = bool(locked)
            r['ts'] = time.time() if t is None else t

    def set_adc(self, peak_dbfs=None, rms_dbfs=None, clip=None, t=None):
        """Raw receiver statistics from the flowgraph: peak and RMS relative
        to ADC full scale, and the fraction of samples at full scale."""
        with self._lock:
            self.adc = {'peak_dbfs': peak_dbfs, 'rms_dbfs': rms_dbfs,
                        'clip': clip, 'ts': time.time() if t is None else t}
            if self.gain is not None:
                # The own transmitter counts only once it has been on for
                # longer than its echo (its signal fills the receive path).
                # (twice the echo: the ADC sees a silence check's gap after
                # the transmit buffers, which can outlast one echo)
                on = self.link.radio.enabled and self._tx_on_since is not None \
                    and self.adc['ts'] - self._tx_on_since > 2.0 * (
                        max(self.chan.echo) if self.chan.echo else 0.5) + 0.5
                self.gain.observe_adc(self.adc['ts'], peak_dbfs, clip, on)

    def _check_adc(self, out, now):
        with self._lock:
            adc = dict(self.adc)
        if adc['clip'] is None or now - adc['ts'] > 2.0:
            self._adc_bad_since = None
            return
        if adc['clip'] < ADC_CLIP_WARN:
            self._adc_bad_since = None
            return
        if self._adc_bad_since is None:
            self._adc_bad_since = now
        if now - self._adc_bad_since >= 1.0 and \
                now - self._adc_told > ADC_WARN_EVERY:
            self._adc_told = now
            auto = self.gain is not None and self.gain.auto
            text = ('receiver overloaded: %.1f %% of ADC samples at full scale '
                    '(peak %.1f dBFS). Other nodes cannot be decoded like '
                    'this - %s' % (100.0 * adc['clip'], adc['peak_dbfs'] or 0.0,
                                   'gain control is lowering RX gain (and '
                                   'raising TX attenuation if that is not '
                                   'enough).' if auto else
                                   'raise TX attenuation (this node\'s own '
                                   'transmitter is the usual cause) or lower '
                                   'RX gain until the ADC tile is below -6 '
                                   'dBFS, or /agc on.'))
            self._log(out, text)
            self._system(text, 'error')

    def set_channel_levels(self, levels, t=None):
        """Per-channel receive level in dB (None = no reading), all measured
        together at time t. Feeds the channel manager's energy detection."""
        with self._lock:
            self._levels = (time.time() if t is None else t, list(levels))

    # ------------------------------------------------------- r6.1 telemetry
    NODE_COLUMNS = (
        ['time', 'uptime', 'addr', 'mode', 'mac_state', 'channel',
         'tx_enabled', 'rx_gain', 'tx_atten', 'agc', 'adc_peak_dbfs',
         'adc_rms_dbfs', 'adc_clip', 'overflows', 'underflows', 'o_per_s',
         'u_per_s', 'tx_backlog_ms', 'tx_dropped', 'model_error_ms',
         'echo_ms', 'lagging', 'cpu_cores', 'cpu_top', 'rx_valid', 'rx_bad',
         'retx', 'dropped', 'live_fault', 'rss_mb']
        + ['%s%d' % (c, k) for k in range(4)
           for c in ('lvl', 'snr', 'evm', 'cfo', 'ok', 'bad', 'rej', 'live')]
        + ['gain_reason'])
    PEER_COLUMNS = ['time', 'nick', 'id', 'addr', 'channel', 'present',
                    'state', 'snr_in', 'level_in', 'snr_out', 'level_out',
                    'fer_ch', 'ok_ch', 'bad_ch', 'retx', 'tx_bytes',
                    'rx_bytes', 'security', 'reason']

    def _telemetry(self, now):
        """One row per second into <telemetry_dir>/node_<addr>_<start>.csv
        and ..._peers.csv. Opened on the first tick, never in __init__."""
        if not self.telemetry_dir or now < self._tele_next:
            return
        self._tele_next = math.floor(now) + 1.0
        try:
            if self._tele is None:
                import csv
                os.makedirs(self.telemetry_dir, exist_ok=True)
                stamp = time.strftime('%Y%m%d_%H%M%S', time.localtime(now))
                base = os.path.join(self.telemetry_dir, 'node_%s_%s'
                                    % (self.nick.replace(' ', '_')[:16]
                                       or self.my_addr, stamp))
                fn, fp = open(base + '.csv', 'w', newline=''), \
                    open(base + '_peers.csv', 'w', newline='')
                self._tele = (fn, csv.writer(fn), fp, csv.writer(fp), base)
                self._tele[1].writerow(self.NODE_COLUMNS)
                self._tele[3].writerow(self.PEER_COLUMNS)
                self._system('telemetry: one row per second in %s.csv' % base)
            node, peers = self.telemetry_rows(now)
            self._tele[1].writerow([node.get(c, '') for c in
                                    self.NODE_COLUMNS])
            for row in peers:
                self._tele[3].writerow([row.get(c, '') for c in
                                        self.PEER_COLUMNS])
            self._tele[0].flush()
            self._tele[2].flush()
        except (OSError, ValueError) as exc:
            self.telemetry_dir = None
            self._system('telemetry stopped: %s' % exc, 'warn')

    def telemetry_rows(self, now):
        """(node row, [peer rows]) as dicts - also used by tests."""
        with self._lock:
            st = self.link.stats
            prev = self._tele_prev or {}
            r = lambda v, d=1: '' if v is None else round(v, d)  # noqa: E731
            phy = self.phy
            ou = phy.get('ou') or {}
            txq = phy.get('txq') or {}
            cpu = phy.get('cpu') or {}
            live = phy.get('live') or {}
            g = self.gain
            row = {'time': round(now, 3), 'uptime': round(now - self.started, 1),
                   'addr': self.my_addr,
                   'mode': 'mesh' if self.mesh else 'static',
                   'mac_state': self.chan.state, 'channel': self.chan.channel,
                   'tx_enabled': int(bool(self.link.radio.enabled)),
                   'rx_gain': g.rx_gain if g else '',
                   'tx_atten': g.tx_atten if g else '',
                   'agc': int(g.auto) if g else '',
                   'gain_reason': g.last_reason if g else '',
                   'adc_peak_dbfs': r(self.adc.get('peak_dbfs')),
                   'adc_rms_dbfs': r(self.adc.get('rms_dbfs')),
                   'adc_clip': r(self.adc.get('clip'), 5),
                   'overflows': ou.get('overflows', ''),
                   'underflows': ou.get('underflows', ''),
                   'tx_backlog_ms': r(1e3 * txq['backlog'])
                   if txq.get('backlog') is not None else '',
                   'tx_dropped': txq.get('dropped', ''),
                   'model_error_ms': r(1e3 * self.link.radio.model_error),
                   'echo_ms': r(1e3 * max(self.chan.echo))
                   if self.chan.echo else '',
                   'lagging': int(bool(self.chan.slow)),
                   'cpu_cores': r(cpu.get('process'), 2),
                   'cpu_top': ' '.join('%s:%.2f' % (n, c) for n, c in
                                       (cpu.get('top') or [])[:8]),
                   'rx_valid': st.get('rx_valid'), 'rx_bad': st.get('rx_bad'),
                   'retx': st.get('retx'), 'dropped': st.get('dropped'),
                   'live_fault': live.get('fault') or '',
                   'rss_mb': _rss_mb()}
            for key in ('overflows', 'underflows'):
                if ou.get(key) is not None and prev.get(key) is not None:
                    row['o_per_s' if key == 'overflows' else 'u_per_s'] = \
                        ou[key] - prev[key]
            afc = phy.get('afc') or []
            dfr = phy.get('deframer') or []
            lch = live.get('channels') or []
            for k in range(min(4, self.num_channels)):
                rc = self.radio_ch[k]
                fresh = rc.get('ts') and now - rc['ts'] < FRESH
                row['lvl%d' % k] = r(rc.get('level_db')) if fresh else ''
                row['snr%d' % k] = r(rc.get('snr_db')) if fresh else ''
                row['evm%d' % k] = r(rc.get('evm'), 2) if fresh else ''
                row['cfo%d' % k] = (round(afc[k]['cfo'])
                                    if k < len(afc) and afc[k].get('cfo')
                                    is not None else '')
                row['ok%d' % k] = self.ch_frames[k] - prev.get('ok', [0] * 4)[k]
                row['bad%d' % k] = self.ch_bad[k] - prev.get('bad', [0] * 4)[k]
                rej = ((dfr[k].get('hdr_mismatch', 0) + dfr[k].get(
                    'hdr_bad_len', 0)) if k < len(dfr) and dfr[k] else None)
                row['rej%d' % k] = (rej - prev.get('rej', {}).get(k, rej)
                                    if rej is not None else '')
                row['live%d' % k] = (int(bool(lch[k])) if k < len(lch)
                                     else '')
            peers = []
            links = self.link.snapshot(now).get('links', {})
            for e in self.peers.values():
                addr = e.get('addr')
                k = e.get('channel')
                ok = bad = None
                if k is not None and 0 <= k < self.num_channels:
                    ok = self.ch_frames[k] - prev.get('ok', [0] * 4)[k]
                    bad = self.ch_bad[k] - prev.get('bad', [0] * 4)[k]
                snr_in, lvl_in = self._hear(addr, now) if addr is not None \
                    else (None, None)
                state, why = self.link_state(e, now)
                lk = links.get(addr) or {}
                peers.append({
                    'time': round(now, 3), 'nick': e.get('nick'),
                    'id': e['id'], 'addr': addr, 'channel': k,
                    'present': int(bool(e.get('present'))), 'state': state,
                    'snr_in': r(snr_in), 'level_in': r(lvl_in),
                    'snr_out': r(e.get('snr_out')),
                    'level_out': r(e.get('level_out')),
                    'fer_ch': (round(bad / float(ok + bad), 4)
                               if ok is not None and ok + bad else ''),
                    'ok_ch': ok if ok is not None else '',
                    'bad_ch': bad if bad is not None else '',
                    'retx': lk.get('retx', ''), 'tx_bytes': lk.get('tx_bytes', ''),
                    'rx_bytes': lk.get('rx_bytes', ''),
                    'security': (self.sec.peers[addr].state if self.sec
                                 is not None and addr in self.sec.peers
                                 else ('off' if self.sec is None else 'idle')),
                    'reason': why or ''})
            self._tele_prev = {
                'ok': list(self.ch_frames), 'bad': list(self.ch_bad),
                'overflows': ou.get('overflows'),
                'underflows': ou.get('underflows'),
                'rej': {k: (dfr[k].get('hdr_mismatch', 0)
                            + dfr[k].get('hdr_bad_len', 0))
                        for k in range(min(4, len(dfr))) if dfr[k]}}
            return row, peers

    def _best_radio(self):
        """Status-bar reading: the single r5 receiver, or else the best
        locked channel other than our own (which only hears ourselves)."""
        if self.radio['ts']:
            return dict(self.radio)
        best = None
        for k, r in enumerate(self.radio_ch):
            if k == self.chan.channel or r.get('snr_db') is None:
                continue
            if best is None or (r['locked'], r['snr_db']) > \
                    (best['locked'], best['snr_db']):
                best = r
        return dict(best) if best is not None else dict(self.radio)

    # --------------------------------------------------------- event fan-in
    def _consume(self, out, actions, now):
        """Split link-layer actions: frames go out, events update the model."""
        for kind, value in actions:
            if kind == 'tx':
                if self.sec is not None:
                    value = self.sec.protect(value, now)
                    if value is None:
                        continue            # no session: never sent in clear
                elif self.sec_error is not None:
                    frm = link.parse(value)
                    if not (frm['type'] == link.FT_IDLE
                            and frm['dst'] == link.BROADCAST):
                        continue
                if not self.mesh and getattr(self.link, 'filler',
                                             None) == 'keepalive':
                    # r6.1: a static node has no beacons; its keep-alives
                    # measure the echo instead (ACK timers follow it)
                    try:
                        f = link.parse(value)
                        if f['type'] == link.FT_IDLE and \
                                f['dst'] == link.BROADCAST:
                            self._beacons_sent[bytes(value)] = now
                            while len(self._beacons_sent) > 8:
                                self._beacons_sent.popitem(last=False)
                    except link.BadFrame:
                        pass
                out.append((kind, value))
            elif kind == 'log':
                out.append((kind, value))
                stamp = time.time()
                with self._lock:
                    for line in str(value).splitlines():
                        self.logs.append((round(stamp, 3), line))
                        self._emit('log', line=line, t=round(stamp, 3))
            elif kind == 'evt':
                self._on_link_event(out, value, now)
            else:
                out.append((kind, value))

    def _sec_actions(self, out, actions, now):
        """Actions from SecureLink. Its 'tx' frames are already sealed."""
        for kind, value in actions:
            if kind == 'rx':
                self._consume(out, self.link.on_frame(value, now), now)
            elif kind == 'tx':
                if not self.link.radio.enabled:
                    # Listening or checking the channel: nothing goes out.
                    # Handshakes retry on their own.
                    self.link.radio.suppressed += 1
                    continue
                self.link.note_tx(len(value), now)
                out.append(('tx', value))
            elif kind == 'log':
                self._consume(out, [('log', value)], now)
            elif kind == 'evt':
                self._on_sec_event(out, value, now)

    def _on_sec_event(self, out, ev, now):
        name = ev.get('e')
        peer = ev.get('peer')
        pid = self._pid_for_addr(peer)
        if name == 'sec_state':
            state = ev.get('state')
            with self._lock:
                self._peers_dirty = True
                self._emit('security', state=state, peer=peer, id=pid)
                p = self.sec.peers.get(peer)
                if state == 'secure':
                    self._system('end-to-end encrypted with node %d - its '
                                 'key fingerprint is %s. Compare it with '
                                 '/secure on that node.' % (peer, ev.get('fp')),
                                 'info', pid)
                elif state == 'handshaking' and p is not None \
                        and p.established:
                    self._system('secure session with %d lost - re-keying; '
                                 'messages wait until it is back' % peer,
                                 'warn', pid)
                elif state == 'no-key' and not self.mesh:
                    self._system('cannot encrypt to %d: its public key is not '
                                 'in %s. Nothing is sent to it until it is '
                                 'added (python3 bpsk_keys.py add ...).'
                                 % (peer, os.path.join(self.keys_dir,
                                                       'roster.json')),
                                 'error', pid)
        elif name == 'sec_session':
            self._consume(out, self.link.kick(now), now)
        elif name == 'peer_restarted':
            self._consume(out, self.link.reset_peer(peer), now)
            with self._lock:
                for key in [k for k in self._rx_parts if k[0] == peer]:
                    del self._rx_parts[key]
                self._system('peer %d restarted' % peer, 'warn', pid)
        elif name == 'sec_untrusted':
            self._peers_dirty = True

    def _on_link_event(self, out, ev, now):
        name = ev.get('e')
        if name == 'peer':
            if self.mesh:
                return          # presence comes from beacons and the sweep
            with self._lock:
                entry = self._by_addr.get(ev.get('addr'))
                if entry is not None:
                    entry['present'] = bool(ev.get('up'))
                    entry['last'] = now
                    self._peers_dirty = True
                self._emit('peer', up=bool(ev.get('up')),
                           addr=ev.get('addr'))
                self._system('peer %d is %s' % (ev.get('addr', 0),
                                                'reachable' if ev.get('up')
                                                else 'unreachable'),
                             'info' if ev.get('up') else 'warn',
                             self._pid_for_addr(ev.get('addr')))
            if ev.get('up'):
                self._announce(out, now)
        elif name == 'tx_state':
            self._on_tx_state(ev)
        elif name == 'rx_data':
            self._on_rx_data(ev, now)
        elif name == 'rx_file_start':
            self._on_rx_file_start(ev, now)
        elif name == 'rx_file_progress':
            self._on_rx_file_progress(ev)
        elif name == 'rx_file_repair':
            with self._lock:
                self._system('%s: %d fragments missing, requesting %d again'
                             % (ev.get('name'), ev.get('missing', 0),
                                ev.get('asked', 0)), 'warn',
                             self._pid_for_addr(ev.get('src')))
        elif name == 'rx_file_done':
            self._on_rx_file_done(ev)
        elif name == 'rx_file_abandoned':
            with self._lock:
                msg = self._rx_msgs.pop(ev.get('src'), None)
                if msg is not None:
                    self._touch(msg, state='incomplete',
                                note='the sender stopped after %d of %d '
                                     'fragments' % (ev.get('got', 0),
                                                    ev.get('total', 0)))
        elif name == 'tx_file_start':
            with self._lock:
                msg = self._by_mid.get(ev.get('mid'))
                if msg is not None:
                    self._touch(msg, progress={'done': 0,
                                               'total': ev.get('total', 0)},
                                state='sending')
        elif name == 'tx_file_progress':
            with self._lock:
                msg = self._by_mid.get(ev.get('mid'))
                if msg is not None:
                    self._touch(msg, progress={'done': ev.get('acked', 0),
                                               'total': ev.get('total', 0)},
                                state='sending')
        elif name == 'tx_file_round':
            with self._lock:
                msg = self._by_mid.get(ev.get('mid'))
                if msg is not None:
                    self._touch(msg, note='repair round %d, %d fragments'
                                % (ev.get('round', 0), ev.get('missing', 0)))
        elif name == 'tx_file_done':
            self._on_tx_file_done(ev, out, now)

    # ----------------------------------------------------- delivery receipts
    def _rcpt(self, msg, peer_addr):
        rcpt = msg.get('rcpt') or {}
        r = rcpt.get(self._pid_for_addr(peer_addr))
        if r is None and len(rcpt) == 1:
            r = next(iter(rcpt.values()))
        return r

    def _refresh(self, msg):
        """Recompute a message's overall state from its recipients."""
        rcpt = list((msg.get('rcpt') or {}).values())
        if not rcpt:
            return
        if len(rcpt) == 1:
            r = rcpt[0]
            self._touch(msg, state=r['state'], tries=r['tries'],
                        acked=r['acked'], note=r['note'])
            return
        state = _aggregate(rcpt)
        done = sum(1 for r in rcpt if r['state'] == 'delivered')
        failed = [r['nick'] or '?' for r in rcpt if r['state'] == 'failed']
        note = 'delivered to %d of %d' % (done, len(rcpt))
        if failed:
            note += ' - not to %s' % ', '.join(failed)
        self._touch(msg, state=state, tries=max(r['tries'] for r in rcpt),
                    acked=sum(r['acked'] for r in rcpt), note=note)

    def _rcpt_failed(self, mid, pid, why):
        with self._lock:
            msg = self._by_mid.get(mid)
            if msg is None:
                return
            r = (msg.get('rcpt') or {}).get(pid)
            if r is None or r['state'] in ('delivered', 'failed'):
                return
            r['state'], r['note'] = 'failed', why
            if msg.get('kind') == 'file':
                self._touch(msg, state='failed', note=why)
            else:
                self._refresh(msg)

    def _on_tx_state(self, ev):
        with self._lock:
            msg = self._by_mid.get(ev.get('mid'))
            if msg is None:
                return
            state = ev.get('state')
            tries = ev.get('tries', 1)
            if msg.get('kind') == 'file':
                # Per-fragment detail would flicker; the progress bar and the
                # retry counter carry the story instead.
                if state == 'retry':
                    self._touch(msg, tries=max(msg.get('tries', 1), tries),
                                note='retransmitting fragment %s'
                                % ev.get('frag'))
                return
            r = self._rcpt(msg, ev.get('peer'))
            if r is None:
                return
            if r['state'] == 'failed':
                # One part of a multi-frame message was dropped. The receiver
                # can never reassemble it, so later parts being acknowledged
                # must not turn the entry back into 'sent' - in r5.1 it sat at
                # a single tick for ever.
                if state == 'acked':
                    r['acked'] += 1
                return
            if state == 'sent':
                r['state'], r['tries'] = 'sent', tries
            elif state == 'retry':
                r['state'], r['tries'] = 'sent', tries
                r['note'] = 'retry %d of %d' % (tries - 1,
                                                self.link.max_retries)
            elif state == 'acked':
                r['acked'] += 1
                r['tries'] = tries
                if r['acked'] >= msg.get('frames', 1):
                    r['state'], r['note'] = 'delivered', None
                else:
                    r['state'] = 'sent'
            elif state == 'failed':
                total = msg.get('frames', 1)
                note = 'no acknowledgement after %d attempts' % tries
                if total > 1:
                    note = ('one of its %d frames was not acknowledged after '
                            '%d attempts - the message is incomplete at the '
                            'far end' % (total, tries))
                r['state'], r['tries'], r['note'] = 'failed', tries, note
            else:
                return
            self._refresh(msg)

    # ------------------------------------------------------------ inbound
    def _on_rx_data(self, ev, now):
        src = ev.get('src')
        pid = self._pid_for_addr(src)
        payload = ev.get('data') or b''
        env = unpack_envelope(payload)
        if env is None:
            text = payload.decode('utf-8', 'replace')
            with self._lock:
                self._add({'id': 'rx%d' % (self._event_seq + 1), 'dir': 'in',
                           'kind': 'text', 'nick': 'Node %s' % src,
                           'addr': src, 'conv': pid, 'text': text,
                           'state': 'received', 'legacy': True})
            return
        with self._lock:
            entry = self._by_addr.get(src)
            if env['nick']:
                if not self.mesh and env['nick'] != self.peer_nick:
                    self.peer_nick = env['nick']
                    self._emit('identity', nick=self.nick,
                               peer_nick=self.peer_nick)
                if entry is not None and entry['nick'] != env['nick']:
                    entry['nick'] = env['nick']
                    self._peers_dirty = True
            if env['type'] == AT_PRESENCE:
                return
            key = (src, env['mid'])
            slot_entry = self._rx_parts.setdefault(key, {'t': now, 'parts': {}})
            slot_entry['t'] = now
            slot = slot_entry['parts']
            slot[env['part']] = env['body']
            if len(slot) < env['parts']:
                return
            del self._rx_parts[key]
            body = b''.join(slot[i] for i in sorted(slot))
            self._add({'id': 'rx%d' % (self._event_seq + 1), 'dir': 'in',
                       'kind': 'text', 'nick': env['nick'] or ('Node %s' % src),
                       'addr': src, 'from': pid,
                       'conv': GROUP if env['type'] == AT_GROUP else pid,
                       'text': body.decode('utf-8', 'replace'),
                       'state': 'received',
                       'frames': env['parts'],
                       'secure': self.sec is not None})

    def _on_rx_file_start(self, ev, now):
        meta = {}
        try:
            if ev.get('meta'):
                meta = json.loads(ev['meta'].decode('utf-8', 'replace'))
        except (ValueError, UnicodeDecodeError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}           # valid JSON but not an object: r5.1 raised here
        name = ev.get('name', 'file')
        mime = meta.get('m') or mimetypes.guess_type(name)[0] \
            or 'application/octet-stream'
        src = ev.get('src')
        entry = self._by_addr.get(src)
        nick = meta.get('n') or (entry['nick'] if entry else self.peer_nick)
        with self._lock:
            if not self.mesh and nick and nick != self.peer_nick:
                self.peer_nick = nick
                self._emit('identity', nick=self.nick, peer_nick=self.peer_nick)
            msg = self._add({'id': 'rxf%d' % (self._event_seq + 1), 'dir': 'in',
                             'kind': 'file', 'nick': nick, 'addr': src,
                             'conv': self._pid_for_addr(src),
                             'from': self._pid_for_addr(src),
                             'state': 'receiving',
                             'secure': self.sec is not None,
                             'file': {'name': name, 'mime': mime,
                                      'size': ev.get('size', 0), 'url': None},
                             'progress': {'done': 0,
                                          'total': ev.get('total', 0)}})
            self._rx_msgs[src] = msg

    def _on_rx_file_progress(self, ev):
        with self._lock:
            msg = self._rx_msgs.get(ev.get('src'))
            if msg is None:
                return
            self._touch(msg, progress={'done': ev.get('got', 0),
                                       'total': ev.get('total', 0)})

    def _on_rx_file_done(self, ev):
        with self._lock:
            src = ev.get('src')
            msg = self._rx_msgs.pop(src, None)
            path = ev.get('path')
            mime = ((msg.get('file') or {}).get('mime') if msg is not None
                    else None)
            token = self._register_file(path, mime) if path else None
            fields = {'state': 'received' if ev.get('ok') else 'corrupt',
                      'note': None if ev.get('ok')
                      else 'CRC mismatch - the file is damaged'}
            if msg is None:
                name = ev.get('name', 'file')
                entry = self._by_addr.get(src)
                msg = self._add({'id': 'rxf%d' % (self._event_seq + 1),
                                 'dir': 'in', 'kind': 'file',
                                 'nick': entry['nick'] if entry
                                 else self.peer_nick,
                                 'addr': src,
                                 'conv': self._pid_for_addr(src),
                                 'from': self._pid_for_addr(src),
                                 'file': {'name': name,
                                          'mime': mimetypes.guess_type(name)[0]
                                          or 'application/octet-stream',
                                          'size': ev.get('size', 0),
                                          'url': None},
                                 'progress': {'done': 0, 'total': 0}})
            info = dict(msg.get('file') or {})
            info['size'] = ev.get('size', info.get('size', 0))
            if token:
                info['url'] = '/file/' + token
            fields['file'] = info
            fields['crc'] = '%08x' % (ev.get('crc') or 0)
            prog = msg.get('progress') or {}
            if prog.get('total'):
                prog = dict(prog, done=prog['total'])
                fields['progress'] = prog
            self._touch(msg, **fields)

    def _on_tx_file_done(self, ev, out, now):
        with self._lock:
            msg = self._by_mid.get(ev.get('mid'))
            if msg is not None:
                r = self._rcpt(msg, ev.get('peer'))
                if ev.get('ok'):
                    prog = dict(msg.get('progress') or {})
                    if prog.get('total'):
                        prog['done'] = prog['total']
                    if r is not None:
                        r['state'], r['note'] = 'delivered', None
                    self._touch(msg, state='delivered', note=None,
                                progress=prog)
                else:
                    if r is not None:
                        r['state'] = 'failed'
                        r['note'] = ev.get('reason') or 'transfer failed'
                    self._touch(msg, state='failed',
                                note=ev.get('reason') or 'transfer failed')
            peer = ev.get('peer')
            if self._active_tx.get(peer) == ev.get('mid'):
                del self._active_tx[peer]
        self._start_next_file(out, now)

    # ------------------------------------------------------------ telemetry
    def _push_stats(self, now):
        with self._lock:
            if (now - self._last_stats_push) < STATS_INTERVAL:
                return
            self._last_stats_push = now
            self._emit('stats', stats=self._stats_payload_locked(now))

    def stats_payload(self, now=None):
        now = now if now is not None else time.time()
        with self._lock:
            return self._stats_payload_locked(now)

    def mac_payload(self, now):
        snap = self.chan.snapshot(now)
        known = [x for x in self.ch_levels if x is not None]
        snap.update(levels=list(self.ch_levels),
                    floor=min(known) if known else None)
        snap.update(mode='mesh' if self.mesh else 'static',
                    my_addr=self.my_addr, fp=self.fingerprint,
                    offsets=[self.offset(k) for k in range(self.num_channels)],
                    plan='symmetric' if self.ch_first is None else 'one-sided',
                    spacing=self.ch_spacing, frames=list(self.ch_frames),
                    adc=dict(self.adc),
                    bad=list(self.ch_bad), tx_enabled=self.link.radio.enabled,
                    radio=[dict(r) for r in self.radio_ch])
        return snap

    def _stats_payload_locked(self, now):
        s = self.link.snapshot(now)
        r = self._rate
        dt = now - r['t']
        if r['t'] and dt >= 0.4:
            r['tx_bps'] = max(0.0, (s['tx_payload_bytes'] - r['tx']) * 8.0 / dt)
            r['rx_bps'] = max(0.0, (s['rx_payload_bytes'] - r['rx']) * 8.0 / dt)
            r['t'], r['tx'], r['rx'] = now, s['tx_payload_bytes'], s['rx_payload_bytes']
        elif not r['t']:
            r['t'], r['tx'], r['rx'] = now, s['tx_payload_bytes'], s['rx_payload_bytes']
        s['tx_bps'] = r['tx_bps']
        s['rx_bps'] = r['rx_bps']
        s['uptime'] = now - self.started
        s['nick'] = self.nick
        s['peer_nick'] = self.peer_nick
        s['radio'] = self._best_radio()
        s['tx_freq'] = self.tx_freq
        s['rx_freq'] = self.rx_freq
        s['mac'] = self.mac_payload(now)
        s['peers'] = self.peers_payload(now)
        s['gain'] = self.gain.snapshot() if self.gain is not None else None
        s['phy'] = dict(self.phy)
        s['plan'] = self.plan_note
        if self.sec is not None:
            sec = self.sec.snapshot(now, self.link.peer_addr
                                    if not self.mesh else None)
            sec['approved'] = (len(self.keys.trusted)
                               + len(self.keys.roster_keys)
                               if self.mesh else None)
            s['security'] = sec
        else:
            s['security'] = {'enabled': False, 'error': self.sec_error}
        return s

    def state_payload(self):
        """Everything a freshly loaded page needs."""
        with self._lock:
            return {'nick': self.nick, 'peer_nick': self.peer_nick,
                    'my_addr': self.link.my_addr,
                    'peer_addr': self.link.peer_addr,
                    'mesh': self.mesh, 'fp': self.fingerprint,
                    'group': GROUP,
                    'default_conv': self._default_conv(),
                    'peers': self.peers_payload(),
                    'messages': [dict(m) for m in self.messages],
                    'logs': list(self.logs),
                    'stats': self._stats_payload_locked(time.time()),
                    'seq': self._event_seq,
                    'revision': link.REVISION}


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# HTTP + Server-Sent Events front end
# --------------------------------------------------------------------------

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from urllib.parse import quote, unquote  # noqa: E402


class _Handler(BaseHTTPRequestHandler):
    server_version = 'BPSKChat/1.0'
    protocol_version = 'HTTP/1.1'

    # The GNU Radio console is busy enough without one line per request.
    def log_message(self, fmt, *args):
        pass

    @property
    def app(self):
        return self.server.app

    # ---------------------------------------------------------------- utils
    def _send(self, code, body=b'', ctype='application/json', extra=None):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for key, val in (extra or {}).items():
            self.send_header(key, val)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, default=str), 'application/json')

    def _body(self):
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            return b''
        if length <= 0:
            return b''
        if length > MAX_UPLOAD:
            # Leaving unread bytes in the socket would desynchronise the next
            # request on this connection, so drop it instead.
            self.close_connection = True
            return b''
        return self.rfile.read(length)

    # ------------------------------------------------------------ guarding
    # The server binds 127.0.0.1, but the browser that talks to it also runs
    # every other web page the operator opens. Two holes in r5.1:
    #   * any page could POST to /api/send - a text/plain body is a 'simple'
    #     request with no CORS preflight, and the body was parsed as JSON
    #     regardless of Content-Type - so a web page could transmit on air;
    #   * with no Host check, a DNS-rebinding page could READ /api/state, i.e.
    #     the whole transcript. Once the link is encrypted, that would be the
    #     one place the plaintext leaks.
    _LOOPBACK = ('127.0.0.1', 'localhost', '[::1]', '::1')

    @classmethod
    def _loopback(cls, hostport):
        host = hostport.strip().lower()
        if host.startswith('['):
            host = host.split(']')[0] + ']'
        else:
            host = host.rsplit(':', 1)[0] if host.count(':') == 1 else host
        return host in cls._LOOPBACK

    def _allowed(self, write=False):
        host = self.headers.get('Host')
        if host is not None and not self._loopback(host):
            self._send(403, b'forbidden host', 'text/plain')
            return False
        if write:
            origin = self.headers.get('Origin')
            if origin is not None:
                ok = origin.startswith('http://') and self._loopback(
                    origin[len('http://'):].split('/', 1)[0])
                if not ok:
                    self._send(403, b'cross-origin request refused',
                               'text/plain')
                    return False
        return True

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        if not self._allowed():
            return
        path = self.path.split('?', 1)[0]
        if path in ('/', '/index.html'):
            return self._serve_ui()
        if path == '/api/state':
            return self._json(self.app.state_payload())
        if path == '/api/stats':
            return self._json(self.app.stats_payload())
        if path == '/api/peers':
            return self._json(self.app.peers_payload())
        if path == '/events':
            return self._serve_events()
        if path.startswith('/file/'):
            return self._serve_file(path[6:])
        self._send(404, b'not found', 'text/plain')

    def _serve_ui(self):
        here = os.path.dirname(os.path.abspath(__file__))
        for candidate in (os.path.join(here, 'chat_ui.html'),
                          os.path.join(os.getcwd(), 'chat_ui.html')):
            if os.path.isfile(candidate):
                with open(candidate, 'rb') as fh:
                    return self._send(200, fh.read(), 'text/html; charset=utf-8')
        self._send(500, b'chat_ui.html is missing - it must sit next to '
                        b'bpsk_app.py', 'text/plain')

    def _serve_file(self, token):
        path, mime = self.app.file_entry(token.split('/')[0])
        if not path or not os.path.isfile(path):
            return self._send(404, b'no such attachment', 'text/plain')
        ctype = (mimetypes.guess_type(path)[0] or mime
                 or 'application/octet-stream')
        with open(path, 'rb') as fh:
            data = fh.read()
        name = os.path.basename(path)
        # http.server encodes headers as Latin-1, so a non-Latin name has to
        # travel as RFC 5987 filename*; the plain filename is an ASCII fallback.
        ascii_name = re.sub(r'[^A-Za-z0-9._-]', '_', name) or 'file'
        self._send(200, data, ctype, {
            'Content-Disposition': "inline; filename=\"%s\"; filename*=UTF-8''%s"
                                   % (ascii_name, quote(name, safe='')),
            # The file came off the air. Opened directly, an HTML or SVG
            # attachment must not run script in this page's origin.
            'Content-Security-Policy': 'sandbox',
            'X-Content-Type-Options': 'nosniff'})

    def _serve_events(self):
        # A fresh page gets the whole model in the hello frame, so it starts
        # from the current sequence number; only a reconnecting page replays
        # the events it missed.
        try:
            seq = int(self.headers.get('Last-Event-ID'))
        except (TypeError, ValueError):
            seq = self.app.current_seq()
        # No Content-Length and no chunking: the stream is delimited by the
        # connection closing, so keep-alive has to be off or the browser will
        # sit waiting for a body length that never comes.
        self.close_connection = True
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'close')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()
        try:
            self.wfile.write(b': connected\n\n')
            self.wfile.flush()
            hello = json.dumps({'type': 'hello',
                                'state': self.app.state_payload()},
                               default=str)
            self.wfile.write(('data: %s\n\n' % hello).encode('utf-8'))
            self.wfile.flush()
            while not self.server.stopping:
                seq, batch = self.app.events_since(seq, timeout=10.0)
                if not batch:
                    self.wfile.write(b': keepalive\n\n')
                    self.wfile.flush()
                    continue
                chunk = []
                for i, ev in enumerate(batch):
                    chunk.append('id: %d\ndata: %s\n\n'
                                 % (seq - len(batch) + 1 + i,
                                    json.dumps(ev, default=str)))
                self.wfile.write(''.join(chunk).encode('utf-8'))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ValueError, OSError):
            return

    # ----------------------------------------------------------------- POST
    def do_POST(self):
        if not self._allowed(write=True):
            return
        path = self.path.split('?', 1)[0]
        if path == '/api/send':
            try:
                data = json.loads(self._body().decode('utf-8') or '{}')
            except ValueError:
                return self._json({'error': 'bad json'}, 400)
            if not isinstance(data, dict):
                return self._json({'error': 'bad json'}, 400)
            text = str(data.get('text') or '').strip()
            if not text:
                return self._json({'error': 'empty'}, 400)
            kind = 'command' if text.startswith('/') else 'text'
            to = data.get('to')
            to = str(to) if to else None
            mid = self.app.submit(kind, text=text, to=to)
            return self._json({'ok': True, 'id': mid})
        if path == '/api/trust':
            try:
                data = json.loads(self._body().decode('utf-8') or '{}')
            except ValueError:
                return self._json({'error': 'bad json'}, 400)
            if not isinstance(data, dict) or not data.get('peer'):
                return self._json({'error': 'bad json'}, 400)
            self.app.submit('trust', peer=str(data['peer']),
                            trust=bool(data.get('trust', True)))
            return self._json({'ok': True})
        if path == '/api/gain':
            try:
                data = json.loads(self._body().decode('utf-8') or '{}')
            except ValueError:
                return self._json({'error': 'bad json'}, 400)
            if not isinstance(data, dict):
                return self._json({'error': 'bad json'}, 400)
            kw = {}
            try:
                for key in ('rx', 'tx'):
                    if data.get(key) is not None:
                        kw[key] = float(data[key])
            except (TypeError, ValueError):
                return self._json({'error': 'bad value'}, 400)
            if data.get('auto') is not None:
                kw['auto'] = bool(data['auto'])
            self.app.submit('gain', **kw)
            return self._json({'ok': True})
        if path == '/api/nick':
            try:
                data = json.loads(self._body().decode('utf-8') or '{}')
            except ValueError:
                return self._json({'error': 'bad json'}, 400)
            if not isinstance(data, dict):
                return self._json({'error': 'bad json'}, 400)
            self.app.submit('nick', nick=str(data.get('nick') or ''))
            return self._json({'ok': True})
        if path == '/api/upload':
            # The page percent-encodes the name: fetch() refuses any header
            # value outside Latin-1, so in r5.1 a file named in Sinhala, Tamil,
            # CJK or with an emoji could not be attached at all.
            name = unquote(self.headers.get('X-Filename') or 'file')
            mime = self.headers.get('X-Filetype') \
                or self.headers.get('Content-Type') or ''
            to = unquote(self.headers.get('X-To') or '') or None
            data = self._body()
            if not data:
                return self._json({'error': 'empty or oversized upload'}, 400)
            mid = self.app.submit('file', name=name, mime=mime, data=data,
                                  to=to)
            return self._json({'ok': True, 'id': mid, 'size': len(data)})
        self._send(404, b'not found', 'text/plain')


class ChatServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, app):
        ThreadingHTTPServer.__init__(self, addr, _Handler)
        self.app = app
        self.stopping = False


def serve(app, port=8088, host='127.0.0.1'):
    """Start the UI server on a daemon thread. Returns (server, thread)."""
    srv = ChatServer((host, port), app)
    thread = threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.2},
                              daemon=True, name='bpsk-chat-http')
    thread.start()
    return srv, thread


def shutdown(srv):
    if srv is None:
        return
    srv.stopping = True
    try:
        srv.shutdown()
        srv.server_close()
    except Exception:
        pass
