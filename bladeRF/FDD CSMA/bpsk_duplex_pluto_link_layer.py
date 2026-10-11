"""GNU Radio shim for the BPSK duplex link layer and chat application.

Three files must sit in the same directory as the flowgraph:

    bpsk_link.py    framing, addressing, stop-and-wait ARQ, file segmentation
    bpsk_app.py     chat model, delivery receipts, HTTP + SSE server
    chat_ui.html    the browser front end

This block owns none of that logic. It converts PMT to bytes, drives the
state machine, and measures the recovered constellation so the UI can show
signal quality. The chat UI is served on 127.0.0.1:<http_port> and starts on
the first tick rather than in __init__, because GRC instantiates this block
every time it validates the flowgraph and must not be left holding a socket.

`peers` is a comma-separated list of the addresses this node talks to,
for example "2,3,4". Leave it empty for a two-node link and peer_addr is
used instead. Unlisted nodes are discovered on first contact.

Input port 0 takes the recovered symbol stream (the Costas loop output). It
is used only for the EVM and level estimates in the status bar; the data path
does not run through this block.
"""

import os
import sys
import threading
import time

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
    bpsk_app = _load('bpsk_app') if bpsk_link is not None else None
    if bpsk_app is None:
        _load_error = 'bpsk_link.py / bpsk_app.py not found next to the flowgraph'
except Exception as _err:            # noqa: BLE001 - reported, never raised
    bpsk_link = bpsk_app = None
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


class blk(gr.sync_block):
    """BPSK chat node: ARQ link layer, application layer and browser UI."""

    def __init__(self, my_addr=1, peer_addr=2, ack_timeout=0.5, max_retries=5,
                 frag_size=256, queue_ahead=0.03, sym_rate=250000.0,
                 overhead=460, rx_dir='.', nickname='', http_port=8088,
                 open_ui=True, tx_freq=0.0, rx_freq=0.0, peers=''):
        gr.sync_block.__init__(self, name='BPSK Chat Node',
                               in_sig=[np.complex64], out_sig=[])
        self.http_port = int(http_port)
        self.open_ui = bool(open_ui)
        self.app = None
        self.link = None
        if bpsk_app is None:
            print('[chat] ERROR: could not load the protocol modules: %s'
                  % _load_error, flush=True)
        else:
            # "2,3,4" from the flowgraph; empty falls back to the single
            # peer_addr so a two-node setup needs no extra configuration.
            roster = [int(tok) for tok in str(peers).replace(';', ',').split(',')
                      if tok.strip().isdigit()] or None
            store = os.path.join(os.path.abspath(rx_dir or '.'), 'chat_files')
            self.app = bpsk_app.ChatApp(
                my_addr=my_addr, peer_addr=peer_addr,
                nick=(nickname or ('Node %d' % my_addr)), store_dir=store,
                ack_timeout=ack_timeout, max_retries=max_retries,
                frag_size=frag_size, queue_ahead=queue_ahead,
                sym_rate=sym_rate, overhead=overhead,
                tx_freq=tx_freq, rx_freq=rx_freq, peers=roster)
            self.link = self.app.link

        for port in ('chat_in', 'rx_frame', 'tick'):
            self.message_port_register_in(pmt.intern(port))
        for port in ('tx_frame', 'log'):
            self.message_port_register_out(pmt.intern(port))
        self.set_msg_handler(pmt.intern('chat_in'), self._on_chat)
        self.set_msg_handler(pmt.intern('rx_frame'), self._on_rx)
        self.set_msg_handler(pmt.intern('tick'), self._on_tick)

        self._lock = threading.RLock()
        self._last_strobe = 0.0
        self._watchdog = None
        self._server = None
        self._meas_t = 0.0
        self._opened = False
        # Set by stop(). Everything that can touch a message port or acquire a
        # resource checks it first: after stop() the runtime is tearing the
        # block down, and publishing to a port from a Python thread at that
        # point is a call into freed C++.
        self._halt = threading.Event()
        # Interned once. _dispatch runs up to 100 times a second.
        self._port_log = pmt.intern('log')
        self._port_tx = pmt.intern('tx_frame')
        if self.link is not None:
            self._say(self.link.banner())

    # ------------------------------------------------------------ plumbing
    def _say(self, text):
        for line in str(text).splitlines():
            print('[link] %s' % line, flush=True)
        if self._halt.is_set():
            return
        self.message_port_pub(self._port_log, pmt.string_to_symbol(str(text)))

    def _dispatch(self, actions):
        if self._halt.is_set():
            return
        for kind, value in actions:
            if kind == 'tx':
                vec = pmt.init_u8vector(len(value), list(bytearray(value)))
                self.message_port_pub(self._port_tx, pmt.cons(pmt.PMT_NIL, vec))
            elif kind == 'log':
                self._say(value)

    # -------------------------------------------------------------- startup
    def _ensure_running(self):
        """Start the UI server and the fallback timer on first traffic.

        Deferred out of __init__ so that GRC, which instantiates this block
        every time it validates the flowgraph, never binds the port or spawns
        a thread.
        """
        if self.app is None or self._halt.is_set():
            return
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
        if self.app is not None:
            self.app.close()
        return True

    # -------------------------------------------------------- message ports
    def _watchdog_loop(self):
        """Drive on_tick if the Message Strobe stops delivering.

        Every ARQ timer and the transmit filler hang off the tick port. If that
        port goes quiet the link silently wedges: nothing retransmits, the
        queue never drains, and the Pluto TX buffer starves. This takes over
        after 250 ms of silence and stands down if the strobe returns.
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
                                  'block - driving ARQ timers and TX filler '
                                  'from the fallback thread instead')
                    self._dispatch(self.app.on_tick(time.time()))
            except Exception as exc:
                print('[chat] watchdog error: %r' % (exc,), flush=True)

    def _on_chat(self, msg):
        if self.app is None or self._halt.is_set():
            return
        text = _to_text(msg)
        if text is None:
            return
        with self._lock:
            self._ensure_running()
            self._dispatch(self.app.on_console(text, time.time()))

    def _on_rx(self, msg):
        if self.app is None or self._halt.is_set():
            return
        raw = _to_bytes(msg)
        if raw is None:
            return
        with self._lock:
            self._ensure_running()
            self._dispatch(self.app.on_rx(raw, time.time()))

    def _on_tick(self, msg):
        if self.app is None or self._halt.is_set():
            return
        with self._lock:
            self._last_strobe = time.time()
            self._ensure_running()
            self._dispatch(self.app.on_tick(time.time()))

    # ------------------------------------------------ constellation quality
    def work(self, input_items, output_items):
        """Estimate EVM and level from the recovered symbols.

        The samples are not part of the data path - they are only measured.
        Decisions are taken on the real axis, which is what a locked
        differential BPSK receiver produces after the Costas loop, so the
        residual is everything the modem failed to clean up: noise, timing
        jitter, and any phase the loop has not tracked out.
        """
        sym = input_items[0]
        now = time.time()
        if self._halt.is_set():
            return len(sym)
        if self.app is not None and (now - self._meas_t) >= 0.2 and len(sym):
            self._meas_t = now
            block = np.asarray(sym[:4096], dtype=np.complex64)
            power = float(np.mean(np.abs(block) ** 2))
            amp = float(np.mean(np.abs(block.real)))
            if amp > 1e-4:
                err = block - np.sign(block.real) * amp
                evm = float(np.sqrt(np.mean(np.abs(err) ** 2)) / amp)
                evm = min(max(evm, 1e-3), 10.0)
                snr = -20.0 * np.log10(evm)
                self.app.set_radio_metrics(
                    snr_db=round(snr, 2),
                    level_db=round(10.0 * np.log10(power + 1e-12), 1),
                    evm=round(100.0 * evm, 2),
                    locked=bool(snr > 5.0))
            else:
                self.app.set_radio_metrics(snr_db=None, level_db=None,
                                           evm=None, locked=False)
        return len(sym)
