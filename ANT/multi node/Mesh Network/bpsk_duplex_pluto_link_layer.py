"""GNU Radio shim for the BPSK network node (r6.1: FDMA mesh) and chat app.

These files must sit in the same directory as the flowgraph:

    bpsk_link.py    framing, addressing, Selective Repeat ARQ, file segmentation
    bpsk_mac.py     channel and address self-assignment, beacons
    bpsk_secure.py  end-to-end encryption: Noise KK sessions, sealing
    bpsk_crypto.py  ChaCha20-Poly1305 / X25519 (pure Python or 'cryptography')
    bpsk_gain.py    closed-loop gain staging (RX gain, TX attenuation)
    bpsk_phy.py     deframer, transmit framer, AFC, measurements (numpy)
    bpsk_app.py     peers, conversations, receipts, HTTP + SSE server
    chat_ui.html    the browser front end

Encryption is on by default. Keys live in <rx_dir>/<keys_dir>: this node's
private key (bpsk_keys.py init) and the keys the operator has approved in the
UI. With my_addr = 0 and peer_addr = 0 (the default) the node joins the mesh:
it derives its address from its key, picks a free channel, and lists every
node in range. Setting both gives the r5.4 two-node behaviour.

This block owns none of that logic. It converts PMT to bytes, drives the
state machine and turns its decisions into messages for the radio blocks.
r6.1: it has NO stream inputs. Everything it measures arrives as short
snapshots (Keep M in N -> Stream to Tagged Stream -> Tagged Stream to PDU,
all C++), so nothing in the sample path ever waits for Python: the r6.0.1
node block consumed four symbol streams and the raw receiver samples in
general_work, was called thousands of times a second with a few samples,
held the GIL, and when it fell behind the backlog reached the radio source.

Ports in
    rx0..rx3    PDUs from each channel's deframer: link frames, plus a
                once-a-second statistics PDU (empty payload) - the liveness
                check of that receive chain
    meas0..3    512 recovered symbols (Costas output) of channel k, 10 a
                second: SNR (EVM) and the constellation for the UI
    iq0..3      4096 samples of channel k before the matched filter, 10 a
                second: channel level (dBFS) and the carrier offset (AFC)
    adc         8192 raw receiver samples, 20 a second: ADC peak, clipping,
                spectrum; the input of the gain control
    txstat      the transmit framer's backlog, 20 a second
    rx_frame    decoded frames from a single-receiver flowgraph (r5)
    chat_in     the GNU Radio console edit box
    tick        Message Strobe, 10 ms
Ports out
    tx_frame    link frames for the transmit framer (PDU)
    tx_en       {'enable': bool} for the transmit framer
    tx_ctl      {'inc': rad/sample} for the rotator that puts the
                transmitter on its channel
    afc0..3     {'inc': rad/sample} for each receive channel's rotator
    rx_attr     {'hardwaregain': dB} for the IIO Attribute Sink (RX)
    tx_attr     {'hardwaregain': -dB} for the IIO Attribute Sink (TX)
    log         console lines

The chat UI is served on 127.0.0.1:<http_port> and, like the stderr tap
that counts gr-iio's 'O'/'U' marks and the telemetry files, starts on the
first tick rather than in __init__: GRC instantiates this block every time
it validates the flowgraph and must not be left holding a socket, a thread
or a file.
"""

import math
import os
import sys
import threading
import time
import traceback
from collections import deque

import numpy as np
import pmt
from gnuradio import gr


def _load(name):
    """Load a sibling module by absolute path, never by module name.

    GRC validates this block by running exec() on this source in an empty
    namespace, so there is no __file__ and the working directory is wherever
    GRC was started. Loading the file directly makes both irrelevant, and
    swallowing every failure keeps a missing module from painting the block
    red in the editor - __init__ reports it on the console instead.
    """
    import importlib.util

    if name in sys.modules:
        return sys.modules[name]

    roots = []
    try:
        roots.append(os.path.dirname(os.path.abspath(__file__)))
    except NameError:
        pass
    roots.append(os.getcwd())
    roots.extend([entry for entry in sys.path if entry])

    for root in roots:
        path = os.path.join(root, name + '.py')
        if not os.path.isfile(path):
            continue
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(name, None)
            raise
        return module
    return None


_load_error = ''
try:
    bpsk_link = _load('bpsk_link')
    for _dep in ('bpsk_crypto', 'bpsk_secure', 'bpsk_mac', 'bpsk_gain',
                 'bpsk_phy'):
        if bpsk_link is not None and _load(_dep) is None:
            bpsk_link = None
    bpsk_app = _load('bpsk_app') if bpsk_link is not None else None
    bpsk_phy = sys.modules.get('bpsk_phy')
    if bpsk_app is None:
        _load_error = ('bpsk_link.py / bpsk_crypto.py / bpsk_secure.py / '
                       'bpsk_mac.py / bpsk_gain.py / bpsk_phy.py / '
                       'bpsk_app.py not found next to the flowgraph')
except Exception as _err:            # noqa: BLE001 - reported, never raised
    bpsk_link = bpsk_app = bpsk_phy = None
    _load_error = repr(_err)


def _to_text(msg):
    """Accept whatever the QT edit box emits and return a str."""
    try:
        if pmt.is_symbol(msg):
            return pmt.symbol_to_string(msg)
        if pmt.is_pair(msg):
            val = pmt.cdr(msg)
            if pmt.is_u8vector(val):
                return bytes(bytearray(pmt.u8vector_elements(val))).decode(
                    'utf-8', 'replace')
            if pmt.is_symbol(val):
                return pmt.symbol_to_string(val)
    except Exception:
        pass
    return None


def _to_bytes(msg):
    """Extract the u8vector of a PDU as bytes."""
    try:
        if pmt.is_pair(msg):
            val = pmt.cdr(msg)
            if pmt.is_u8vector(val):
                return bytes(bytearray(pmt.u8vector_elements(val)))
    except Exception:
        pass
    return None


def _to_complex(msg):
    """The c32vector of a PDU as a numpy array (or None)."""
    try:
        if pmt.is_pair(msg):
            val = pmt.cdr(msg)
            if pmt.is_c32vector(val):
                return np.asarray(pmt.c32vector_elements(val),
                                  dtype=np.complex64)
    except Exception:
        pass
    return None


def _meta(msg):
    try:
        if pmt.is_pair(msg) and pmt.is_dict(pmt.car(msg)):
            return pmt.to_python(pmt.car(msg)) or {}
    except Exception:
        pass
    return {}


NUM_CHANNELS = 4          # fixed by the flowgraph: four receive chains
OUT_RATE_SPS = 4          # samples per symbol after the channel filters
STALL = 3.0               # seconds without data that make a chain 'stalled'
# snapshot taps in the flowgraph (keep m in n): one every n items of
TAP_EVERY = {'meas': 25000,     # symbols (after the Costas loop)
             'iq': 100000,      # channel samples (after the AFC rotator)
             'adc': 300000}     # radio samples
CPU_EVERY = 2.0           # seconds between per-thread CPU readings
VIEW_EVERY = 1.0          # seconds between constellation/spectrum pushes


class StderrTap(object):
    """Counts gr-iio's overflow 'O' / underflow 'U' marks.

    gr-iio writes them with fprintf(stderr, ...) from its own overflow and
    underflow threads, at most one per second per direction, so the only
    place to see them is file descriptor 2. A pipe is put there; a thread
    copies everything to the real stderr unchanged and counts the marks
    (bpsk_phy.count_ou). The pipe is enlarged to 1 MiB so writers never
    block even if this thread is briefly kept from the GIL, and any failure
    puts the original stderr back.
    """

    def __init__(self):
        self.overflows = 0
        self.underflows = 0
        self.active = False
        self.error = None
        self._orig = None
        self._r = None
        self._thread = None
        self._state = None

    def start(self):
        if bpsk_phy is None or self.active:
            return
        try:
            sys.stderr.flush()
            self._orig = os.dup(2)
            r, w = os.pipe()
            try:
                import fcntl
                fcntl.fcntl(w, getattr(fcntl, 'F_SETPIPE_SZ', 1031), 1 << 20)
            except Exception:                     # noqa: BLE001
                pass
            os.dup2(w, 2)
            os.close(w)
            self._r = r
            self.active = True
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name='bpsk-stderr-tap')
            self._thread.start()
        except Exception as exc:                  # noqa: BLE001
            self.error = repr(exc)
            self._restore()

    def _run(self):
        try:
            while True:
                data = os.read(self._r, 65536)
                if not data:
                    break
                os.write(self._orig, data)
                o, u, self._state = bpsk_phy.count_ou(
                    data.decode('latin-1'), self._state)
                self.overflows += o
                self.underflows += u
        except Exception as exc:                  # noqa: BLE001
            self.error = repr(exc)
        finally:
            self._restore()

    def _restore(self):
        if self._orig is not None:
            try:
                os.dup2(self._orig, 2)
            except OSError:
                pass
        self.active = False

    def stop(self):
        if not self.active:
            return
        self._restore()               # writers now go to the real stderr
        try:
            os.close(self._r)         # the reader thread sees EOF/EBADF
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=0.5)
        try:
            os.close(self._orig)
        except OSError:
            pass
        self._orig = None


def _thread_cpu():
    """{tid: (name, cpu seconds)} of this process's threads (Linux)."""
    out = {}
    tick = float(os.sysconf('SC_CLK_TCK'))
    base = '/proc/self/task'
    try:
        tids = os.listdir(base)
    except OSError:
        return out
    for tid in tids:
        try:
            with open('%s/%s/stat' % (base, tid)) as fh:
                st = fh.read()
            with open('%s/%s/comm' % (base, tid)) as fh:
                name = fh.read().strip()
        except OSError:
            continue
        f = st[st.rfind(')') + 2:].split()
        out[tid] = (name, (int(f[11]) + int(f[12])) / tick)
    return out


class blk(gr.basic_block):
    """BPSK network node: MAC, ARQ link layer, encryption, chat UI."""

    def __init__(self, my_addr=0, peer_addr=0, ack_timeout=0.5, max_retries=5,
                 frag_size=256, queue_ahead=0.03, sym_rate=250000.0,
                 overhead=140, rx_dir='.', nickname='', http_port=8088,
                 open_ui=True, center_freq=0.0, security=True,
                 keys_dir='keys', window=32, channel=-1, ch_spacing=600e3,
                 ch_first=450e3, radio_rate=6e6, rx_gain=30, tx_atten=50,
                 agc=True, telemetry=True, plan_file='', count_ou=True):
        gr.basic_block.__init__(self, name='BPSK Chat Node', in_sig=None,
                                out_sig=None)
        self.http_port = int(http_port)
        self.open_ui = bool(open_ui)
        self.radio_rate = float(radio_rate)
        self.ch_rate = OUT_RATE_SPS * float(sym_rate)
        # a chain is stalled after STALL s without data, or four snapshot
        # intervals when those are longer (slowed-down bench)
        self.stall = max(STALL, 4.0 * max(
            TAP_EVERY['meas'] / float(sym_rate),
            TAP_EVERY['iq'] / self.ch_rate,
            TAP_EVERY['adc'] / self.radio_rate))
        self.app = None
        self.link = None
        if bpsk_app is None:
            print('[chat] ERROR: could not load the protocol modules: %s'
                  % _load_error, flush=True)
        else:
            root = os.path.abspath(rx_dir or '.')
            store = os.path.join(root, 'chat_files')
            chan = int(channel)
            self.app = bpsk_app.ChatApp(
                my_addr=int(my_addr) or None, peer_addr=int(peer_addr) or None,
                nick=nickname or None, store_dir=store,
                ack_timeout=ack_timeout, max_retries=max_retries,
                frag_size=frag_size, queue_ahead=queue_ahead,
                sym_rate=sym_rate, overhead=overhead,
                tx_freq=center_freq, rx_freq=center_freq,
                security=bool(security),
                keys_dir=os.path.join(root, keys_dir or 'keys'),
                window=int(window),
                channel=chan if 0 <= chan < NUM_CHANNELS else None,
                num_channels=NUM_CHANNELS, ch_spacing=float(ch_spacing),
                ch_first=float(ch_first), filler='keepalive',
                gain=dict(rx_gain=rx_gain, tx_atten=tx_atten,
                          auto=bool(agc)),
                telemetry_dir=(os.path.join(root, 'telemetry')
                               if telemetry else None),
                plan_file=(os.path.join(root, plan_file)
                           if plan_file else None))
            self.link = self.app.link

        # GNU Radio's Python gateway looks a handler up BY NAME on the block
        # (set_msg_handler passes handler.__name__ to C++), so every port
        # needs a real method - a lambda or partial aborts the flowgraph.
        handlers = [('chat_in', self._on_chat), ('rx_frame', self._on_rx),
                    ('tick', self._on_tick), ('adc', self._on_adc),
                    ('txstat', self._on_txstat)]
        for k in range(NUM_CHANNELS):
            handlers += [('rx%d' % k, getattr(self, '_on_rx%d' % k)),
                         ('meas%d' % k, getattr(self, '_on_meas%d' % k)),
                         ('iq%d' % k, getattr(self, '_on_iq%d' % k))]
        for port, fn in handlers:
            self.message_port_register_in(pmt.intern(port))
            self.set_msg_handler(pmt.intern(port), fn)
        outs = ['tx_frame', 'tx_en', 'tx_ctl', 'rx_attr', 'tx_attr', 'log'] \
            + ['afc%d' % k for k in range(NUM_CHANNELS)]
        for port in outs:
            self.message_port_register_out(pmt.intern(port))

        self._lock = threading.RLock()
        self._last_strobe = 0.0
        self._watchdog = None
        self._server = None
        self._opened = False
        # Set by stop(). Everything that can touch a message port or acquire a
        # resource checks it first: after stop() the runtime is tearing the
        # block down, and publishing to a port from a Python thread at that
        # point is a call into freed C++.
        self._halt = threading.Event()
        self._faults = 0
        # Interned once; _dispatch runs up to 100 times a second.
        self._port_log = pmt.intern('log')
        self._port_tx = pmt.intern('tx_frame')
        self._port_en = pmt.intern('tx_en')
        self._port_ctl = pmt.intern('tx_ctl')
        self._port_rxa = pmt.intern('rx_attr')
        self._port_txa = pmt.intern('tx_attr')
        self._port_afc = [pmt.intern('afc%d' % k) for k in range(NUM_CHANNELS)]
        self._k_inc = pmt.intern('inc')
        self._k_en = pmt.intern('enable')
        self._k_gain = pmt.intern('hardwaregain')
        # Physical-layer state (r6.1).
        self._afc = ([bpsk_phy.Afc(self.ch_rate) for _ in range(NUM_CHANNELS)]
                     if bpsk_phy is not None else [])
        self._lvl = [None] * NUM_CHANNELS          # (t, dBFS) per channel
        self._lvl_push = 0.0
        self._seen = {'meas': [0.0] * NUM_CHANNELS, 'iq': [0.0] * NUM_CHANNELS,
                      'dfr': [0.0] * NUM_CHANNELS, 'adc': 0.0}
        self._dfr = [None] * NUM_CHANNELS
        self._dfr_bits = [None] * NUM_CHANNELS
        self._fault = None
        self._fault_told = -1e9
        self._started = None
        self._disp = deque()                       # (serial, airtime) sent
        self._serial = 0
        self._txq = {}
        self._tap = StderrTap() if count_ou else None
        self._cpu_prev = None
        self._cpu_next = 0.0
        self._view = {'const': [None] * NUM_CHANNELS, 'spectrum': None}
        self._view_next = 0.0
        if self.link is not None:
            self._say(self.app.banner())
            if self.app.plan_note:
                self._say(self.app.plan_note)

    # ------------------------------------------------------------ plumbing
    def _say(self, text):
        for line in str(text).splitlines():
            print('[link] %s' % line, flush=True)
        if self._halt.is_set():
            return
        self.message_port_pub(self._port_log, pmt.string_to_symbol(str(text)))

    def _inc(self, offset_hz, rate):
        return pmt.dict_add(pmt.make_dict(), self._k_inc,
                            pmt.from_double(2.0 * math.pi * offset_hz / rate))

    def _dispatch(self, actions):
        if self._halt.is_set():
            return
        for kind, value in actions:
            if kind == 'tx':
                vec = pmt.init_u8vector(len(value), list(bytearray(value)))
                self.message_port_pub(self._port_tx, pmt.cons(pmt.PMT_NIL, vec))
                self._serial += 1
                self._disp.append((self._serial,
                                   self.link.radio.airtime(len(value))))
                while len(self._disp) > 4096:
                    self._disp.popleft()
            elif kind == 'chan':
                # Retune the transmitter: the rotator after the modulator.
                # Receive is untouched (all channels).
                self.message_port_pub(self._port_ctl, self._inc(
                    float(self.app.offset(value)), self.radio_rate))
            elif kind == 'txen':
                self.message_port_pub(self._port_en, pmt.dict_add(
                    pmt.make_dict(), self._k_en, pmt.from_bool(bool(value))))
            elif kind == 'gain':
                # IIO Attribute Sinks; bpsk_gain keeps the values in range,
                # which matters: attr_sink throws on a refused write.
                v = int(value['value'])
                port = self._port_rxa if value['what'] == 'rx' \
                    else self._port_txa
                text = str(v if value['what'] == 'rx' else -v)
                self.message_port_pub(port, pmt.dict_add(
                    pmt.make_dict(), self._k_gain, pmt.intern(text)))
            elif kind == 'log':
                self._say(value)

    def _fault_tb(self, where):
        self._faults += 1
        n = self._faults
        if n <= 5 or n % 1000 == 0:
            print('[chat] INTERNAL ERROR #%d in %s - that input was discarded, '
                  'the node keeps running:' % (n, where), flush=True)
            traceback.print_exc()
            sys.stdout.flush()
            sys.stderr.flush()
            if n == 5:
                print('[chat] further tracebacks are printed every 1000th '
                      'fault only', flush=True)
            try:
                self._say('internal error in %s (#%d) - traceback on the '
                          'terminal' % (where, n))
            except Exception:
                pass

    # -------------------------------------------------------------- startup
    def _ensure_running(self):
        """Start the UI server, the fallback timer and the stderr tap on first
        traffic. Deferred out of __init__ so that GRC, which instantiates this
        block every time it validates the flowgraph, never binds the port,
        spawns a thread or redirects a file descriptor.
        """
        if self.app is None or self._halt.is_set():
            return
        if self._started is None:
            self._started = time.time()
            if self._tap is not None:
                self._tap.start()
            # gr-iio's Pluto source template never applies its RF bandwidth
            # parameter (3.10), so the receiver's analog filter is whatever
            # the device had. Set it for the channel plan: 0.9 x the rate.
            bw = int(max(2e5, min(5.6e7, 0.9 * self.radio_rate)))
            self.message_port_pub(self._port_rxa, pmt.dict_add(
                pmt.make_dict(), pmt.intern('rf_bandwidth'),
                pmt.intern(str(bw))))
        if self._server is None:
            try:
                self._server, _ = bpsk_app.serve(self.app, self.http_port)
                url = 'http://127.0.0.1:%d/' % self.http_port
                self._say('chat UI ready at %s  (files in %s)'
                          % (url, self.app.store_dir))
                if self.open_ui and not self._opened:
                    self._opened = True
                    threading.Thread(target=self._open_browser, args=(url,),
                                     daemon=True).start()
            except OSError as exc:
                self._server = False        # do not retry every 10 ms
                self._say('could not start the chat UI on port %d: %s'
                          % (self.http_port, exc))
        if self._watchdog is None:
            self._watchdog = threading.Thread(target=self._watchdog_loop,
                                              daemon=True)
            self._watchdog.start()

    @staticmethod
    def _open_browser(url):
        import webbrowser
        time.sleep(0.7)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    def stop(self):
        """Stop the watchdog and release the port, in that order.

        Order matters. The watchdog drives on_tick and publishes the frames it
        produces; left running it keeps calling message_port_pub while the
        runtime destroys the block. Because stop() also freezes _last_strobe,
        the watchdog ENGAGES within 250 ms of teardown and then publishes at
        100 Hz, so this is the steady state during shutdown, not a rare race.
        """
        self._halt.set()
        watchdog, self._watchdog = self._watchdog, None
        if watchdog is not None and watchdog.is_alive():
            watchdog.join(timeout=1.0)
        server, self._server = self._server, None
        if server:
            bpsk_app.shutdown(server)
        if self._tap is not None:
            self._tap.stop()
        if self.app is not None:
            self.app.close()
        return True

    # -------------------------------------------------------- message ports
    def _watchdog_loop(self):
        """Drive on_tick if the Message Strobe stops delivering.

        Every ARQ timer and the beacons hang off the tick port. If that port
        goes quiet the link silently wedges: nothing retransmits and the queue
        never drains. This takes over after 250 ms of silence and stands down
        if the strobe returns.
        """
        engaged = False
        while not self._halt.is_set():
            if self._halt.wait(0.01):
                return                      # stop() asked us to leave
            try:
                with self._lock:
                    if self.app is None or self._halt.is_set():
                        continue
                    if (time.time() - self._last_strobe) < 0.25:
                        if engaged:
                            engaged = False
                            self._say('message strobe recovered, watchdog '
                                      'standing down')
                        continue
                    if not engaged:
                        engaged = True
                        self._say('WARNING: no tick from the Message Strobe '
                                  'block - driving ARQ timers and beacons '
                                  'from the fallback thread instead')
                    self._dispatch(self.app.on_tick(time.time()))
            except Exception:
                self._fault_tb('watchdog')

    def _on_chat(self, msg):
        if self.app is None or self._halt.is_set():
            return
        text = _to_text(msg)
        if text is None:
            return
        try:
            with self._lock:
                self._ensure_running()
                self._dispatch(self.app.on_console(text, time.time()))
        except Exception:
            self._fault_tb('chat_in')

    def _on_rx(self, msg, channel=None):
        if self.app is None or self._halt.is_set():
            return
        raw = _to_bytes(msg)
        if raw is None:
            return
        try:
            with self._lock:
                self._ensure_running()
                now = time.time()
                if not raw:
                    meta = _meta(msg)
                    if channel is not None and 'stats' in meta:
                        self._on_deframer_stats(channel, meta['stats'], now)
                    return
                self._dispatch(self.app.on_rx(raw, now, channel=channel))
        except Exception:
            self._fault_tb('rx_frame' if channel is None else 'rx%d' % channel)

    def _on_rx0(self, msg):
        self._on_rx(msg, 0)

    def _on_rx1(self, msg):
        self._on_rx(msg, 1)

    def _on_rx2(self, msg):
        self._on_rx(msg, 2)

    def _on_rx3(self, msg):
        self._on_rx(msg, 3)

    def _on_meas0(self, msg):
        self._on_meas(msg, 0)

    def _on_meas1(self, msg):
        self._on_meas(msg, 1)

    def _on_meas2(self, msg):
        self._on_meas(msg, 2)

    def _on_meas3(self, msg):
        self._on_meas(msg, 3)

    def _on_iq0(self, msg):
        self._on_iq(msg, 0)

    def _on_iq1(self, msg):
        self._on_iq(msg, 1)

    def _on_iq2(self, msg):
        self._on_iq(msg, 2)

    def _on_iq3(self, msg):
        self._on_iq(msg, 3)

    def _on_tick(self, msg):
        if self.app is None or self._halt.is_set():
            return
        try:
            with self._lock:
                now = time.time()
                self._last_strobe = now
                self._ensure_running()
                self._push_levels(now)
                self._dispatch(self.app.on_tick(now))
                self._housekeeping(now)
        except Exception:
            self._fault_tb('tick')

    # ------------------------------------------------- physical layer (r6.1)
    def _on_meas(self, msg, k):
        """Recovered symbols of channel k: SNR from the EVM (decisions on
        the real axis, which is what a locked differential BPSK receiver
        produces after the Costas loop), and the constellation for the UI."""
        if self.app is None or self._halt.is_set() or bpsk_phy is None:
            return
        try:
            x = _to_complex(msg)
            if x is None or not len(x):
                return
            now = time.time()
            self._seen['meas'][k] = now
            snr, evm = bpsk_phy.evm_snr(x)
            with self._lock:
                if snr is None:
                    self.app.set_radio_metrics(snr_db=None, evm=None,
                                               locked=False, channel=k)
                else:
                    self.app.set_radio_metrics(
                        snr_db=round(snr, 2), evm=round(100.0 * evm, 2),
                        locked=bool(snr > 5.0), channel=k)
            if now >= self._view_next - 0.5:
                pts = x[:96]
                self._view['const'][k] = [[round(float(z.real), 2),
                                           round(float(z.imag), 2)]
                                          for z in pts]
        except Exception:
            self._fault_tb('meas%d' % k)

    def _on_iq(self, msg, k):
        """Channel k before the matched filter: its in-band level relative
        to ADC full scale (the channel filters have unity gain; the level is
        taken after a matched filter, so it is what the decoder sees) and
        the AFC."""
        if self.app is None or self._halt.is_set() or bpsk_phy is None:
            return
        try:
            x = _to_complex(msg)
            if x is None or not len(x):
                return
            now = time.time()
            self._seen['iq'][k] = now
            lvl = bpsk_phy.inband_level_db(x)
            self._lvl[k] = (now, round(lvl, 1))
            with self._lock:
                self.app.set_radio_metrics(level_db=round(lvl, 1), channel=k)
            afc = self._afc[k]
            inc = afc.update(now, x)
            if inc is not None and not self._halt.is_set():
                self.message_port_pub(self._port_afc[k], pmt.dict_add(
                    pmt.make_dict(), self._k_inc, pmt.from_double(inc)))
        except Exception:
            self._fault_tb('iq%d' % k)

    def _on_adc(self, msg):
        """Raw receiver samples: ADC peak and clipping (the gain control's
        input), and a spectrum for the UI."""
        if self.app is None or self._halt.is_set() or bpsk_phy is None:
            return
        try:
            x = _to_complex(msg)
            if x is None or not len(x):
                return
            now = time.time()
            self._seen['adc'] = now
            st = bpsk_phy.adc_stats(x)
            with self._lock:
                self.app.set_adc(peak_dbfs=round(st['peak_dbfs'], 1),
                                 rms_dbfs=round(st['rms_dbfs'], 1),
                                 clip=st['clip'], t=now)
            if now >= self._view_next - 0.5 and self._view['spectrum'] is None:
                self._view['spectrum'] = bpsk_phy.spectrum_db(x, 128)
        except Exception:
            self._fault_tb('adc')

    def _on_txstat(self, msg):
        """The transmit framer's report: correct the airtime model."""
        if self.app is None or self._halt.is_set():
            return
        try:
            st = pmt.to_python(msg) if pmt.is_dict(msg) else None
            if not st:
                return
            received = (st.get('accepted', 0) + st.get('dropped', 0)
                        + st.get('rejected', 0))
            while self._disp and self._disp[0][0] <= received:
                self._disp.popleft()
            pending = sum(a for _, a in self._disp)
            now = time.time()
            with self._lock:
                if st.get('enabled') and st.get('backlog') is not None:
                    self.link.radio.observe(
                        now, float(st['t']) + float(st['backlog']) + pending)
                self._txq = {'backlog': st.get('backlog'),
                             'dropped': st.get('dropped', 0),
                             'flushed': st.get('flushed', 0),
                             'enabled': bool(st.get('enabled')),
                             'sent': st.get('sent', 0),
                             'fill_bits': st.get('fill_bits', 0),
                             'model_error': self.link.radio.model_error,
                             'error': st.get('error')}
        except Exception:
            self._fault_tb('txstat')

    def _on_deframer_stats(self, k, stats, now):
        if not isinstance(stats, dict):
            return
        bits = stats.get('bits')
        if bits is not None and bits != self._dfr_bits[k]:
            self._seen['dfr'][k] = now
        self._dfr_bits[k] = bits
        self._dfr[k] = stats

    def _push_levels(self, now):
        """One level per channel, all from the last 0.3 s, ten times a
        second: the MAC's energy detection compares them with each other."""
        if now - self._lvl_push < 0.1:
            return
        self._lvl_push = now
        lv = [(v[1] if v is not None and now - v[0] < 0.3 else None)
              for v in self._lvl]
        if any(x is not None for x in lv):
            self.app.set_channel_levels(lv, now)

    def _housekeeping(self, now):
        """Liveness, counters, CPU and UI snapshots (all cheap; every tick)."""
        started = self._started or now
        warm = now - started > self.stall + 2.0
        adc_age = now - self._seen['adc'] if self._seen['adc'] else None
        chans = []
        for k in range(NUM_CHANNELS):
            last = min(self._seen['meas'][k], self._seen['dfr'][k])
            chans.append(bool(last) and now - last < self.stall)
        fault = None
        if warm and (adc_age is None or adc_age > self.stall):
            fault = ('receiver stalled: no samples from the radio for more '
                     'than %.0f s - check the E200 link (USB/Ethernet) and '
                     'the console' % self.stall)
        elif warm and not all(chans):
            dead = [k for k in range(NUM_CHANNELS) if not chans[k]]
            fault = ('receive chain of channel%s %s stalled (no data for '
                     'more than %.0f s)%s'
                     % ('s' if len(dead) > 1 else '',
                        ', '.join(str(k) for k in dead), self.stall,
                        '; the other channels are unaffected'
                        if len(dead) < NUM_CHANNELS else ''))
        if fault != self._fault:
            self._fault = fault
            if fault:
                self._fault_told = now
                self._say('FAULT: ' + fault)
                self.app._system(fault, 'error')
            elif warm:
                self._say('receive chains running again')
        elif fault and now - self._fault_told > 30.0:
            self._fault_told = now
            self._say('FAULT (continuing): ' + fault)
        self.app.set_phy('live', {'channels': chans, 'fault': fault,
                                  'adc_age': adc_age})
        tap = self._tap
        self.app.set_phy('ou', {'overflows': tap.overflows if tap else None,
                                'underflows': tap.underflows if tap else None,
                                'tap': bool(tap and tap.active),
                                'error': tap.error if tap else None})
        self.app.set_phy('afc', [{'cfo': round(a.cfo, 1),
                                  'residual': (round(a.residual, 1)
                                               if a.residual is not None
                                               else None),
                                  'ratio': round(a.ratio, 1),
                                  'updates': a.updates} for a in self._afc])
        self.app.set_phy('deframer', list(self._dfr))
        self.app.set_phy('txq', dict(self._txq))
        if now >= self._cpu_next:
            self._cpu_next = now + CPU_EVERY
            self._sample_cpu(now)
        if now >= self._view_next:
            self._view_next = now + VIEW_EVERY
            self.app.publish_view(dict(self._view))
            self._view = {'const': [None] * NUM_CHANNELS, 'spectrum': None}

    def _sample_cpu(self, now):
        """CPU per GNU Radio thread (threads carry their block's name)."""
        snap = _thread_cpu()
        proc = sum(os.times()[:2])
        prev, self._cpu_prev = self._cpu_prev, (now, snap, proc)
        if prev is None:
            return
        dt = max(1e-3, now - prev[0])
        agg = {}
        for tid, (name, c) in snap.items():
            old = prev[1].get(tid)
            if old is None:
                continue
            key = name.rstrip('0123456789') or name
            agg[key] = agg.get(key, 0.0) + (c - old[1]) / dt
        top = sorted(agg.items(), key=lambda kv: -kv[1])[:12]
        self.app.set_phy('cpu', {'process': round((proc - prev[2]) / dt, 2),
                                 'top': [[n, round(c, 3)] for n, c in top
                                         if c >= 0.005]})
