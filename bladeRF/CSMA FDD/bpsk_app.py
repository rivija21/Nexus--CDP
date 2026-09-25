"""
Application layer for the BPSK full-duplex PlutoSDR link.

Layering
--------
    chat_ui.html          browser: transcript, telemetry, composer
        | HTTP + Server-Sent Events on 127.0.0.1
    bpsk_app.py           this file: envelope, chat model, receipts, server
        | ('tx', bytes) / ('evt', dict)
    bpsk_link.py          addressing, stop-and-wait ARQ, file segmentation
        | PDU
    GNU Radio flowgraph   BPSK modem, PlutoSDR

Like bpsk_link, this module imports nothing from GNU Radio, so the whole
application can be exercised without a radio (see bpsk_offline_demo.py).
Only the standard library is used - no web framework, no websocket package -
because a lab machine cannot be assumed to have either.

Application envelope, carried inside the payload of a link DATA frame:

    byte  0        : 0x9A magic
    byte  1        : app version (4b) | app frame type (4b)
    bytes 2-5      : message id, uint32 big-endian, sender-scoped
    byte  6        : part index of a multi-frame message
    byte  7        : number of parts
    byte  8        : sender nickname length N
    bytes 9..9+N-1 : nickname, utf-8
    rest           : this part's payload

A DATA frame that does not begin with the magic byte is delivered as plain
text from an unknown sender, so an r4 node - or somebody typing into the raw
GNU Radio edit box - still shows up in the transcript.

File attachments reuse the link layer's file protocol; the envelope fields
travel in the FILE_START metadata blob as compact JSON.
"""

import json
import mimetypes
import os
import posixpath
import re
import struct
import threading
import time
from collections import deque, OrderedDict

import bpsk_link as link

APP_MAGIC = 0x9A
APP_VERSION = 1

AT_TEXT = 0x1          # direct message, one named recipient
AT_PRESENCE = 0x2      # nickname announcement
AT_ROOM = 0x3          # broadcast-room message (fanned out as unicast)

MAX_PARTS = 255
PRESENCE_MIN_INTERVAL = 10.0     # seconds between nickname announcements
STATS_INTERVAL = 0.25            # seconds between telemetry pushes to the UI
EVENT_BACKLOG = 2000             # SSE events retained for reconnecting clients
MAX_UPLOAD = 8 * 1024 * 1024     # bytes accepted from the browser in one POST
MAX_FILE_TOKENS = 1000           # attachment URLs retained; oldest evicted
PART_TIMEOUT = 60.0              # seconds before an incomplete multi-part text is dropped

_SAFE_NAME = re.compile(r'[^A-Za-z0-9._-]+')

# set_radio_metrics() has to be able to say "no reading" as well as "unchanged",
# so an omitted argument and an explicit None mean different things.
_UNSET = object()


def _safe_filename(name):
    name = posixpath.basename((name or '').replace('\\', '/'))
    name = _SAFE_NAME.sub('_', name).strip('._') or 'file'
    return name[:120]


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


class ChatApp(object):
    """Chat model, receipts and telemetry on top of a LinkState.

    The GNU Radio thread drives on_rx / on_tick / on_console and gets the
    usual ('tx', bytes) / ('log', str) action list back. The HTTP threads only
    ever call submit() and the read-only accessors, both of which take the
    same lock, so no radio call can be re-entered from a request handler.
    """

    def __init__(self, my_addr=1, peer_addr=2, nick=None, store_dir=None,
                 ack_timeout=0.5, max_retries=5, frag_size=256,
                 queue_ahead=0.03, sym_rate=250000.0, overhead=452,
                 tx_freq=0.0, rx_freq=0.0, peers=None,
                 beacon_interval=0.0, peer_timeout=3.0):
        self.store_dir = os.path.abspath(store_dir or 'chat_files')
        try:
            os.makedirs(self.store_dir, exist_ok=True)
        except OSError:
            self.store_dir = os.path.abspath('.')
        self.link = link.LinkState(my_addr, peer_addr, ack_timeout,
                                   max_retries, frag_size, queue_ahead,
                                   sym_rate, overhead, rx_dir=self.store_dir,
                                   peers=peers,
                                   beacon_interval=beacon_interval,
                                   peer_timeout=peer_timeout)
        self.nick = (nick or ('Node %d' % my_addr)).strip()[:32]
        # addr -> nickname. A node is known by address the moment it is
        # configured or discovered; the nickname arrives with its first frame.
        self.peer_nicks = {}
        for peer in self.link.peer_list():
            self.peer_nicks[peer.addr] = 'Node %d' % peer.addr
        self.tx_freq = float(tx_freq or 0.0)
        self.rx_freq = float(rx_freq or 0.0)

        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._inbox = deque()            # commands from the HTTP threads
        self._events = deque(maxlen=EVENT_BACKLOG)
        self.logs = deque(maxlen=300)    # (timestamp, line) for the log drawer
        self._event_seq = 0
        self._closed = False

        self.messages = []               # transcript, oldest first
        self._by_mid = {}                # our own message id -> message
        self._rx_parts = {}              # (src, mid) -> {'t': float, 'parts': {}}
        self._files = OrderedDict()      # file token -> absolute path (bounded)
        self._file_seq = 0
        self._next_mid = 1
        self._pending_file = deque()     # attachments waiting for the radio
        self._active_tx_file = {}        # dst -> mid currently transferring
        self._rx_msgs = {}               # src -> inbound transfer being reassembled

        self.radio = {'snr_db': None, 'level_db': None, 'evm': None,
                      'locked': False, 'ts': 0.0}
        self.mac = None                  # r6b: MAC snapshot, None under FDD
        self.started = time.time()
        self._last_stats_push = 0.0
        # None, not 0.0: the throttle compares against this, and a zero
        # would swallow the very first announcement whenever the time
        # base starts near zero - which is exactly what simulated time
        # does. The bug is invisible under time.time() and fatal under a
        # test harness, so the sentinel has to be unambiguous.
        self._last_presence = None
        self._rate = {'t': 0.0, 'tx': 0, 'rx': 0, 'tx_bps': 0.0, 'rx_bps': 0.0}

    # ---------------------------------------------------------- peer roster
    @property
    def peer_nick(self):
        """First peer's nickname. Kept so single-peer callers still work."""
        addr = self.link.peer_addr
        return self.peer_nicks.get(addr, 'Node %d' % addr)

    def _nick_of(self, addr):
        return self.peer_nicks.get(addr, 'Node %s' % addr)

    def _note_nick(self, addr, nick):
        """Record a peer's nickname; announce the roster if it changed."""
        if not nick or addr is None:
            return
        if self.peer_nicks.get(addr) != nick:
            self.peer_nicks[addr] = nick
            self._emit('identity', nick=self.nick, peer_nick=self.peer_nick,
                       peers=self._roster_locked())

    def _roster_locked(self):
        return [{'addr': p.addr, 'nick': self._nick_of(p.addr), 'up': p.up,
                 'conv': 'p%d' % p.addr, 'queued': len(p.txq)}
                for p in self.link.peer_list()]

    def _targets(self, target):
        """Resolve a send target to a list of addresses.

        'room' fans out to every known peer as separate reliable unicast
        frames. An unacknowledged 0xFF broadcast would give room messages
        weaker delivery than direct ones, which is the wrong trade here.
        """
        if target in (None, 'room'):
            return [p.addr for p in self.link.peer_list()]
        try:
            return [int(target) & 0xFF]
        except (TypeError, ValueError):
            return []

    @staticmethod
    def _conv_of(target):
        return 'room' if target in (None, 'room') else 'p%d' % (int(target) & 0xFF)

    # ------------------------------------------------------------ event bus
    def _emit(self, kind, **payload):
        """Queue one UI event. Safe to call with or without the lock held."""
        with self._cv:
            self._event_seq += 1
            payload['type'] = kind
            self._events.append((self._event_seq, payload))
            self._cv.notify_all()

    def events_since(self, seq, timeout=25.0):
        """Block until an event newer than seq exists; return (seq, [events])."""
        deadline = time.time() + timeout
        with self._cv:
            while not self._closed:
                if self._events and self._events[-1][0] > seq:
                    out = [e for e in self._events if e[0] > seq]
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

    def _register_file(self, path):
        self._file_seq += 1
        token = 'f%d' % self._file_seq
        self._files[token] = path
        while len(self._files) > MAX_FILE_TOKENS:
            self._files.popitem(last=False)      # oldest attachment URL expires
        return token

    def file_path(self, token):
        with self._lock:
            return self._files.get(token)

    def _add(self, msg):
        with self._lock:
            msg.setdefault('ts', time.time())
            msg.setdefault('state', 'info')
            msg.setdefault('conv', 'room')
            self.messages.append(msg)
            if len(self.messages) > 5000:
                del self.messages[:1000]
            self._emit('message', message=msg)
        return msg

    def _touch(self, msg, **fields):
        with self._lock:
            msg.update(fields)
            self._emit('message', message=dict(msg))

    def _system(self, text, level='info'):
        with self._lock:
            return self._add({'id': 'sys%d' % (self._event_seq + 1),
                              'dir': 'sys', 'kind': 'system', 'text': text,
                              'level': level})

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
            target = kw.get('target')
            kw = self._prepare_attachment(mid, kw.get('name', 'file'),
                                          kw.get('mime', ''),
                                          kw.get('data', b''))
            if kw is None:
                return mid
            kw['target'] = target
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

    def _sendfile_worker(self, path):
        """Read and fragment a /sendfile target off the radio thread."""
        try:
            with open(path, 'rb') as fh:
                data = fh.read()
        except OSError as exc:
            self._system('could not read %s: %s' % (path, exc), 'error')
            return
        self.submit('file', name=os.path.basename(path), mime='', data=data)

    def _drain_inbox(self, out, now):
        while True:
            with self._lock:
                if not self._inbox:
                    return
                job = self._inbox.popleft()
            kind = job['kind']
            if kind == 'text':
                self._send_text(out, job['mid'], job.get('text', ''), now,
                                job.get('target'))
            elif kind == 'file':
                self._queue_attachment(out, job, now)
            elif kind == 'command':
                self._console(out, job.get('text', ''), now)
            elif kind == 'nick':
                with self._lock:
                    self.nick = (job.get('nick') or self.nick).strip()[:32] \
                        or self.nick
                    self._emit('identity', nick=self.nick,
                               peer_nick=self.peer_nick)
                self._announce(out, now, force=True)

    def _send_text(self, out, mid, text, now, target=None):
        """Queue one message to the room or to a single peer.

        A room message becomes one reliable unicast per peer, all sharing this
        message id, so the receipt only reads 'delivered' once every recipient
        has acknowledged every part.
        """
        text = (text or '').strip()
        if not text:
            return
        with self._lock:
            targets = self._targets(target)
            conv = self._conv_of(target)
            nick_len = len(self.nick.encode('utf-8', 'replace')[:32])
            chunk = max(32, self.link.frag_size - 9 - nick_len)
            raw = text.encode('utf-8')
            parts = [raw[i:i + chunk] for i in range(0, len(raw), chunk)] or [b'']
            if len(parts) > MAX_PARTS:
                parts = parts[:MAX_PARTS]
                self._system('message truncated to %d frames' % MAX_PARTS,
                             'warn')
            atype = AT_ROOM if conv == 'room' else AT_TEXT
            msg = self._add({'id': mid, 'dir': 'out', 'kind': 'text',
                             'nick': self.nick, 'addr': self.link.my_addr,
                             'conv': conv, 'to': list(targets),
                             'text': text, 'state': 'queued', 'tries': 1,
                             'frames': len(parts) * max(1, len(targets)),
                             'acked': 0})
            self._by_mid[mid] = msg
            frames = [pack_envelope(atype, mid, i, len(parts), self.nick, p)
                      for i, p in enumerate(parts)]
        if not targets:
            self._touch(msg, state='failed', note='no peers are known yet')
            return
        for dst in targets:
            for frame in frames:
                self._consume(out, self.link.send_data(frame, now, mid=mid,
                                                       dst=dst), now)

    def _queue_attachment(self, out, job, now):
        """Radio thread: register an already-prepared attachment. No bulk work."""
        mid = job['mid']
        with self._lock:
            targets = self._targets(job.get('target'))
            conv = self._conv_of(job.get('target'))
            token = self._register_file(job['path'])
            msg = self._add({'id': mid, 'dir': 'out', 'kind': 'file',
                             'nick': self.nick, 'addr': self.link.my_addr,
                             'conv': conv, 'to': list(targets),
                             'state': 'queued', 'tries': 1,
                             'file': {'name': job['name'], 'mime': job['mime'],
                                      'size': job['size'],
                                      'url': '/file/' + token},
                             'progress': {'done': 0, 'total': 0}})
            self._by_mid[mid] = msg
            if not targets:
                self._touch(msg, state='failed', note='no peers are known yet')
                return
            for dst in targets:                 # one queued transfer per peer
                self._pending_file.append(dict(job, dst=dst))
        self._start_next_file(out, now)

    def _start_next_file(self, out, now):
        """Start as many transfers as there are idle peers.

        One transfer per peer at a time - that is a link-layer invariant, each
        Peer holds a single tx_file - but transfers to different peers run
        concurrently, interleaved by the round-robin pump.
        """
        started = []
        with self._lock:
            remaining = deque()
            while self._pending_file:
                job = self._pending_file.popleft()
                dst = job.get('dst')
                peer = self.link.peers.get(dst)
                if (dst in self._active_tx_file or peer is None
                        or peer.tx_file is not None):
                    remaining.append(job)
                    continue
                self._active_tx_file[dst] = job['mid']
                meta = job.get('meta') or json.dumps(
                    {'n': self.nick, 'm': job['mime'], 'i': job['mid'],
                     't': round(time.time(), 3)},
                    separators=(',', ':')).encode('utf-8')[:link.MAX_FILE_META]
                started.append((job, meta))
            self._pending_file = remaining
        for job, meta in started:
            prepared = ((job['frags'], job['crc'])
                        if job.get('frags') is not None else None)
            self._consume(out, self.link.send_file(
                job['name'], job['data'], now, meta=meta, mid=job['mid'],
                prepared=prepared, items=job.get('items'),
                dst=job.get('dst')), now)

    def _announce(self, out, now, force=False):
        """Tell the peer our nickname, at most once every few seconds."""
        with self._lock:
            if (not force and self._last_presence is not None
                    and (now - self._last_presence) < PRESENCE_MIN_INTERVAL):
                return
            self._last_presence = now
            frame = pack_envelope(AT_PRESENCE, 0, 0, 1, self.nick, b'')
            targets = [p.addr for p in self.link.peer_list()]
        for dst in targets:
            self._consume(out, self.link.send_data(frame, now,
                                                   label='presence', dst=dst),
                          now)

    def _console(self, out, text, now):
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
            threading.Thread(target=self._sendfile_worker, args=(path,),
                             daemon=True, name='bpsk-sendfile').start()
        elif cmd == '/peers':
            roster = self._roster_locked()
            if not roster:
                self._system('no peers known yet', 'warn')
            for entry in roster:
                self._system('  %d  %-16s %s  queued=%d'
                             % (entry['addr'], entry['nick'],
                                'up  ' if entry['up'] else 'down',
                                entry['queued']))
        elif cmd == '/dm':
            bits = arg.split(None, 1)
            if len(bits) < 2 or not bits[0].isdigit():
                self._system('usage: /dm <address> <message>', 'warn')
                return
            self._send_text(out, self._new_mid(), bits[1], now,
                            target=int(bits[0]))
        elif cmd in ('/stats', '/help', '/ping'):
            for action in self.link.on_user(text, now):
                if action[0] == 'log':
                    self._system(action[1])
                elif action[0] == 'evt':
                    self._on_link_event(out, action[1], now)
                else:
                    out.append(action)
        else:
            self._system('unknown command %s' % cmd, 'warn')

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
            self._send_text(out, self._new_mid(), text, now)
        return out

    def on_rx(self, raw, now):
        out = []
        self._consume(out, self.link.on_rx(raw, now), now)
        return out

    def on_tick(self, now):
        out = []
        self._drain_inbox(out, now)
        self._consume(out, self.link.on_tick(now), now)
        self._start_next_file(out, now)
        self._sweep_rx_parts(now)
        self._push_stats(now)
        return out

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
                             'warn')

    def set_radio_metrics(self, snr_db=_UNSET, level_db=_UNSET, evm=_UNSET,
                          locked=_UNSET):
        """Called by the flowgraph shim with physical-layer measurements.

        Passing None clears a reading - that is how the shim reports that the
        receiver has gone quiet, as opposed to simply not measuring it.
        """
        with self._lock:
            if snr_db is not _UNSET:
                self.radio['snr_db'] = snr_db
            if level_db is not _UNSET:
                self.radio['level_db'] = level_db
            if evm is not _UNSET:
                self.radio['evm'] = evm
            if locked is not _UNSET:
                self.radio['locked'] = bool(locked)
            self.radio['ts'] = time.time()

    def set_mac_state(self, snap):
        """Called by the CSMA shim with the medium-access snapshot."""
        with self._lock:
            self.mac = dict(snap) if snap else None

    # --------------------------------------------------------- event fan-in
    def _consume(self, out, actions, now):
        """Split link-layer actions: frames go out, events update the model."""
        for kind, value in actions:
            if kind == 'tx':
                out.append((kind, value))
            elif kind == 'log':
                out.append((kind, value))
                stamp = time.time()
                with self._lock:
                    for line in str(value).splitlines():
                        self.logs.append((round(stamp, 3), line))
                        self._emit('log', line=line, t=round(stamp, 3))
            else:
                self._on_link_event(out, value, now)

    def _on_link_event(self, out, ev, now):
        name = ev.get('e')
        if name == 'peer_new':
            with self._lock:
                addr = ev.get('addr')
                self.peer_nicks.setdefault(addr, 'Node %s' % addr)
                self._system('discovered node %s' % addr)
                self._emit('identity', nick=self.nick,
                           peer_nick=self.peer_nick,
                           peers=self._roster_locked())
        elif name == 'peer':
            with self._lock:
                self._emit('peer', up=bool(ev.get('up')), addr=ev.get('addr'),
                           peers=self._roster_locked())
                self._system('peer %d is %s' % (ev.get('addr', 0),
                                                'reachable' if ev.get('up')
                                                else 'unreachable'),
                             'info' if ev.get('up') else 'warn')
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
                                ev.get('asked', 0)), 'warn')
        elif name == 'rx_file_done':
            self._on_rx_file_done(ev)
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
            if state == 'sent':
                self._touch(msg, state='sent', tries=tries)
            elif state == 'retry':
                self._touch(msg, state='sent', tries=tries,
                            note='retry %d of %d'
                                 % (tries - 1, self.link.max_retries))
            elif state == 'acked':
                total = msg.get('frames', 1)
                acked = msg.get('acked', 0) + 1
                if acked >= total:
                    self._touch(msg, state='delivered', acked=acked,
                                tries=tries, note=None)
                else:
                    self._touch(msg, state='sent', acked=acked, tries=tries)
            elif state == 'failed':
                self._touch(msg, state='failed', tries=tries,
                            note='no acknowledgement after %d attempts' % tries)

    def _on_rx_data(self, ev, now):
        src = ev.get('src')
        payload = ev.get('data') or b''
        env = unpack_envelope(payload)
        if env is None:
            text = payload.decode('utf-8', 'replace')
            with self._lock:
                self._add({'id': 'rx%d' % (self._event_seq + 1), 'dir': 'in',
                           'kind': 'text', 'nick': self._nick_of(src),
                           'addr': src, 'conv': 'p%s' % src, 'text': text,
                           'state': 'received', 'legacy': True})
            return
        with self._lock:
            self._note_nick(src, env['nick'])
            if env['type'] == AT_PRESENCE:
                return
            conv = 'room' if env['type'] == AT_ROOM else 'p%s' % src
            key = (src, env['mid'])
            entry = self._rx_parts.setdefault(key, {'t': now, 'parts': {}})
            entry['t'] = now
            slot = entry['parts']
            slot[env['part']] = env['body']
            if len(slot) < env['parts']:
                return
            del self._rx_parts[key]
            body = b''.join(slot[i] for i in sorted(slot))
            self._add({'id': 'rx%d' % (self._event_seq + 1), 'dir': 'in',
                       'kind': 'text', 'nick': env['nick'] or self._nick_of(src),
                       'addr': src, 'conv': conv,
                       'text': body.decode('utf-8', 'replace'),
                       'state': 'received', 'frames': env['parts']})

    def _on_rx_file_start(self, ev, now):
        meta = {}
        try:
            if ev.get('meta'):
                meta = json.loads(ev['meta'].decode('utf-8', 'replace'))
        except (ValueError, UnicodeDecodeError):
            meta = {}
        name = ev.get('name', 'file')
        mime = meta.get('m') or mimetypes.guess_type(name)[0] \
            or 'application/octet-stream'
        src = ev.get('src')
        nick = meta.get('n') or self._nick_of(src)
        with self._lock:
            self._note_nick(src, meta.get('n'))
            msg = self._add({'id': 'rxf%d' % (self._event_seq + 1), 'dir': 'in',
                             'kind': 'file', 'nick': nick,
                             'addr': src, 'conv': 'p%s' % src,
                             'state': 'receiving',
                             'file': {'name': name, 'mime': mime,
                                      'size': ev.get('size', 0), 'url': None},
                             'progress': {'done': 0,
                                          'total': ev.get('total', 0)}})
            self._rx_msgs[ev.get('src')] = msg

    def _on_rx_file_progress(self, ev):
        with self._lock:
            msg = self._rx_msgs.get(ev.get('src'))
            if msg is None:
                return
            self._touch(msg, progress={'done': ev.get('got', 0),
                                       'total': ev.get('total', 0)})

    def _on_rx_file_done(self, ev):
        with self._lock:
            msg = self._rx_msgs.pop(ev.get('src'), None)
            path = ev.get('path')
            token = self._register_file(path) if path else None
            fields = {'state': 'received' if ev.get('ok') else 'corrupt',
                      'note': None if ev.get('ok')
                      else 'CRC mismatch - the file is damaged'}
            if msg is None:
                name = ev.get('name', 'file')
                msg = self._add({'id': 'rxf%d' % (self._event_seq + 1),
                                 'dir': 'in', 'kind': 'file',
                                 'nick': self._nick_of(ev.get('src')),
                                 'addr': ev.get('src'),
                                 'conv': 'p%s' % ev.get('src'),
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
                if ev.get('ok'):
                    prog = dict(msg.get('progress') or {})
                    if prog.get('total'):
                        prog['done'] = prog['total']
                    self._touch(msg, state='delivered', note=None,
                                progress=prog)
                else:
                    self._touch(msg, state='failed',
                                note=ev.get('reason') or 'transfer failed')
            dst = ev.get('dst')
            if self._active_tx_file.get(dst) == ev.get('mid'):
                self._active_tx_file.pop(dst, None)
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
        for entry in s.get('peers', []):
            entry['nick'] = self._nick_of(entry['addr'])
            entry['conv'] = 'p%d' % entry['addr']
        s['radio'] = dict(self.radio)
        if self.mac is not None:
            s['mac'] = dict(self.mac)
        s['tx_freq'] = self.tx_freq
        s['rx_freq'] = self.rx_freq
        return s

    def state_payload(self):
        """Everything a freshly loaded page needs."""
        with self._lock:
            return {'nick': self.nick, 'peer_nick': self.peer_nick,
                    'my_addr': self.link.my_addr,
                    'peer_addr': self.link.peer_addr,
                    'peers': self._roster_locked(),
                    'messages': [dict(m) for m in self.messages],
                    'logs': list(self.logs),
                    'stats': self._stats_payload_locked(time.time()),
                    'seq': self._event_seq,
                    'revision': link.REVISION}


# --------------------------------------------------------------------------
# HTTP + Server-Sent Events front end
# --------------------------------------------------------------------------

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402


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

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path in ('/', '/index.html'):
            return self._serve_ui()
        if path == '/api/state':
            return self._json(self.app.state_payload())
        if path == '/api/stats':
            return self._json(self.app.stats_payload())
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
        path = self.app.file_path(token.split('/')[0])
        if not path or not os.path.isfile(path):
            return self._send(404, b'no such attachment', 'text/plain')
        ctype = mimetypes.guess_type(path)[0] or 'application/octet-stream'
        with open(path, 'rb') as fh:
            data = fh.read()
        name = os.path.basename(path)
        self._send(200, data, ctype,
                   {'Content-Disposition': 'inline; filename="%s"' % name})

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
        path = self.path.split('?', 1)[0]
        if path == '/api/send':
            try:
                data = json.loads(self._body().decode('utf-8') or '{}')
            except ValueError:
                return self._json({'error': 'bad json'}, 400)
            text = (data.get('text') or '').strip()
            if not text:
                return self._json({'error': 'empty'}, 400)
            kind = 'command' if text.startswith('/') else 'text'
            mid = self.app.submit(kind, text=text, target=data.get('target'))
            return self._json({'ok': True, 'id': mid})
        if path == '/api/nick':
            try:
                data = json.loads(self._body().decode('utf-8') or '{}')
            except ValueError:
                return self._json({'error': 'bad json'}, 400)
            self.app.submit('nick', nick=data.get('nick') or '')
            return self._json({'ok': True})
        if path == '/api/upload':
            name = self.headers.get('X-Filename') or 'file'
            mime = self.headers.get('X-Filetype') \
                or self.headers.get('Content-Type') or ''
            target = self.headers.get('X-Target') or None
            data = self._body()
            if not data:
                return self._json({'error': 'empty or oversized upload'}, 400)
            mid = self.app.submit('file', name=name, mime=mime, data=data,
                                  target=target)
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
