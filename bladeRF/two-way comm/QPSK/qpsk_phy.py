"""
qpsk_phy.py -- differential-QPSK full-duplex PHY for two ANT-SDR E200 / Pluto
nodes, derived from the lab flowgraph (mpsk_stage6.grc).

Chain, TX:
    FrameSource(bytes, continuous) -> generic_mod(QPSK, differential, RRC)
        -> multiply_const -> pluto sink @ tx_freq

Chain, RX:
    pluto source @ rx_freq -> agc2 -> pfb_clock_sync (osps=2)
        -> linear_equalizer (CMA) -> costas_loop (order 4)
        -> constellation_decoder -> diff_decoder(mod 4)
        -> unpack_k_bits(2) -> correlate_access_code_tag -> FrameSink

Differences from the lab flowgraph, and why:
  * The simulated channel model / delay / random source / QT sinks are gone --
    they exist only to demonstrate impairments in simulation.
  * TX is *continuously* fed: when there is nothing to send, the frame source
    emits PN filler.  This keeps the Pluto TX buffer from underflowing and,
    more importantly, keeps the peer's timing/equaliser/Costas loops locked
    between messages, so the first frame after an idle period is not lost.
  * TX and RX excess bandwidth are matched (the lab file used 0.5 on TX and
    0.35 in the RX matched filter).
  * FDD: TX and RX are on different frequencies, so both directions run at
    once on one AD9361 (its TX and RX chains are independent).
"""

import threading
from collections import deque

import numpy as np

import pmt

from gnuradio import gr, blocks, digital, analog
from gnuradio.filter import firdes

try:
    from gnuradio import iio
except ImportError:  # pragma: no cover - only on hosts without gr-iio
    iio = None

from qpsk_framing import ACCESS_CODE, FrameAssembler, build_frame, pn_bytes

TAG_KEY = "pkt_start"


# --------------------------------------------------------------------------
# Python blocks
# --------------------------------------------------------------------------

class FrameSource(gr.sync_block):
    """Byte source that emits queued frames back-to-back and PN filler when
    idle, so the modulator (and therefore the transmitter) never starves."""

    IDLE_CHUNK = 1024  # bytes of filler produced per work() call when idle

    def __init__(self, filler_len=4096):
        gr.sync_block.__init__(
            self, name="frame_source", in_sig=None, out_sig=[np.uint8]
        )
        self._lock = threading.Lock()
        self._q = deque()
        self._cur = None
        self._pos = 0
        self._filler = pn_bytes(filler_len)
        self._filler_pos = 0
        self.n_frames_sent = 0
        self.n_bytes_sent = 0

    # called from any thread
    def send(self, payload, urgent=False):
        frame = np.frombuffer(build_frame(payload), dtype=np.uint8)
        with self._lock:
            if urgent:
                self._q.appendleft(frame)
            else:
                self._q.append(frame)

    def backlog(self):
        with self._lock:
            return len(self._q)

    def _idle(self, n):
        out = np.empty(n, dtype=np.uint8)
        got = 0
        while got < n:
            take = min(n - got, len(self._filler) - self._filler_pos)
            out[got:got + take] = self._filler[
                self._filler_pos:self._filler_pos + take
            ]
            got += take
            self._filler_pos = (self._filler_pos + take) % len(self._filler)
        return out

    def work(self, input_items, output_items):
        out = output_items[0]
        n = len(out)
        produced = 0
        while produced < n:
            if self._cur is None or self._pos >= len(self._cur):
                with self._lock:
                    self._cur = self._q.popleft() if self._q else None
                self._pos = 0
                if self._cur is None:
                    take = min(n - produced, self.IDLE_CHUNK)
                    out[produced:produced + take] = self._idle(take)
                    produced += take
                    break  # short return: pick up new frames promptly
                self.n_frames_sent += 1
            take = min(n - produced, len(self._cur) - self._pos)
            out[produced:produced + take] = self._cur[self._pos:self._pos + take]
            self._pos += take
            produced += take
        self.n_bytes_sent += produced
        return produced


class FrameSink(gr.sync_block):
    """Consumes the tagged bit stream and calls `on_frame(payload)` for every
    header- and CRC-valid frame."""

    def __init__(self, on_frame, tag_key=TAG_KEY):
        gr.sync_block.__init__(
            self, name="frame_sink", in_sig=[np.uint8], out_sig=None
        )
        self.asm = FrameAssembler(on_frame)
        self._tag = pmt.string_to_symbol(tag_key)

    def work(self, input_items, output_items):
        in0 = input_items[0]
        n = len(in0)
        abs0 = self.nitems_read(0)
        tags = self.get_tags_in_window(0, 0, n, self._tag)
        if tags:
            self.asm.add_tags([t.offset for t in tags])
        self.asm.add_bits(abs0, in0)
        self.asm.process()
        return n


# --------------------------------------------------------------------------
# Pluto helpers (gr-iio API differs across 3.10.x point releases)
# --------------------------------------------------------------------------

def make_pluto_sink(uri, freq, samp_rate, bandwidth, atten, buffer_size):
    if iio is None:
        raise RuntimeError("gr-iio is not available in this GNU Radio install")
    if hasattr(iio, "fmcomms2_sink_fc32"):
        snk = iio.fmcomms2_sink_fc32(uri, [True, True], buffer_size, False)
        snk.set_len_tag_key("")
        snk.set_bandwidth(int(bandwidth))
        snk.set_frequency(int(freq))
        snk.set_samplerate(int(samp_rate))
        snk.set_attenuation(0, float(atten))
        snk.set_filter_params("Auto", "", 0, 0)
        return snk
    # legacy signature
    return iio.pluto_sink(
        uri, int(freq), int(samp_rate), int(bandwidth), buffer_size,
        False, float(atten), "", True
    )


def make_pluto_source(uri, freq, samp_rate, bandwidth, gain_mode, gain,
                      buffer_size):
    if iio is None:
        raise RuntimeError("gr-iio is not available in this GNU Radio install")
    if hasattr(iio, "fmcomms2_source_fc32"):
        src = iio.fmcomms2_source_fc32(uri, [True, True], buffer_size)
        src.set_len_tag_key("")
        src.set_frequency(int(freq))
        src.set_samplerate(int(samp_rate))
        src.set_gain_mode(0, gain_mode)
        src.set_gain(0, float(gain))
        src.set_quadrature(True)
        src.set_rfdc(True)
        src.set_bbdc(True)
        src.set_filter_params("Auto", "", 0, 0)
        return src
    return iio.pluto_source(
        uri, int(freq), int(samp_rate), int(bandwidth), buffer_size,
        True, True, True, gain_mode, float(gain), "", True
    )


# --------------------------------------------------------------------------
# DSP chains (shared by the radio application and the offline loopback test)
# --------------------------------------------------------------------------

NFILTS = 32


def make_constellation():
    return digital.constellation_rect(
        [0.707 + 0.707j, -0.707 + 0.707j, -0.707 - 0.707j, 0.707 - 0.707j],
        [0, 1, 2, 3], 4, 2, 2, 1, 1
    ).base()


def make_rrc_taps(cfg, nfilts=NFILTS):
    return firdes.root_raised_cosine(
        nfilts, nfilts, 1.0 / float(cfg.sps), cfg.excess_bw,
        11 * cfg.sps * nfilts
    )


def _cap_buffer(blk, nitems):
    """Bound a block's output buffer.  Pipeline latency is the sum of the data
    sitting in every buffer; with a continuously-fed transmitter those buffers
    would otherwise fill with idle filler and delay ACKs by hundreds of ms."""
    try:
        blk.set_max_output_buffer(int(nitems))
    except Exception:
        pass


class TxChain:
    """FrameSource -> generic_mod -> amplitude scaling.  `blocks` is in
    connection order; feed the last one to the sink of your choice."""

    def __init__(self, cfg, constellation=None):
        self.cfg = cfg
        self.constellation = constellation or make_constellation()
        self.frame_source = FrameSource()
        self.mod = digital.generic_mod(
            constellation=self.constellation,
            differential=True,
            samples_per_symbol=cfg.sps,
            pre_diff_code=True,
            excess_bw=cfg.excess_bw,
            verbose=False,
            log=False,
            truncate=False,
        )
        self.scale = blocks.multiply_const_cc(cfg.tx_amplitude)
        self.blocks = [self.frame_source, self.mod, self.scale]
        for b in self.blocks:
            _cap_buffer(b, cfg.max_buf_items)


class RxChain:
    """AGC -> timing recovery -> CMA equaliser -> Costas -> symbol decode ->
    differential decode -> bits -> access-code correlation -> FrameSink."""

    def __init__(self, cfg, on_frame, constellation=None):
        self.cfg = cfg
        self.constellation = constellation or make_constellation()
        rrc_taps = make_rrc_taps(cfg)

        self.agc = analog.agc2_cc(1e-1, 1e-2, 1.0, 1.0)
        self.agc.set_max_gain(65536.0)
        self.clock_sync = digital.pfb_clock_sync_ccf(
            cfg.sps, cfg.timing_bw, rrc_taps, NFILTS, NFILTS / 2, 1.5, 2
        )
        self.eq_alg = digital.adaptive_algorithm_cma(
            self.constellation, cfg.eq_step, 4
        ).base()
        self.equalizer = digital.linear_equalizer(
            cfg.eq_taps, 2, self.eq_alg, True, [], "corr_est"
        )
        self.costas = digital.costas_loop_cc(cfg.phase_bw, 4, False)
        self.decoder = digital.constellation_decoder_cb(self.constellation)
        try:
            self.diff_decoder = digital.diff_decoder_bb(
                4, digital.DIFF_DIFFERENTIAL)
        except TypeError:      # gnuradio < 3.10.2
            self.diff_decoder = digital.diff_decoder_bb(4)
        self.unpack = blocks.unpack_k_bits_bb(2)
        self.correlator = digital.correlate_access_code_tag_bb(
            ACCESS_CODE, cfg.access_threshold, TAG_KEY
        )
        self.frame_sink = FrameSink(on_frame)

        self.blocks = [
            self.agc, self.clock_sync, self.equalizer, self.costas,
            self.decoder, self.diff_decoder, self.unpack, self.correlator,
            self.frame_sink,
        ]
        for b in self.blocks[:-1]:
            _cap_buffer(b, cfg.max_buf_items)


# --------------------------------------------------------------------------
# top block
# --------------------------------------------------------------------------

class QpskPhy(gr.top_block):
    """Full-duplex FDD differential-QPSK modem on one AD9361."""

    def __init__(self, cfg, on_frame):
        gr.top_block.__init__(self, "qpsk_duplex_phy", catch_exceptions=True)
        self.cfg = cfg
        self.qpsk = make_constellation()

        self.tx = TxChain(cfg, self.qpsk)
        self.frame_source = self.tx.frame_source
        self.pluto_sink = make_pluto_sink(
            cfg.uri, cfg.tx_freq, cfg.samp_rate, cfg.bandwidth,
            cfg.tx_atten, cfg.buffer_size
        )
        self.connect(*(self.tx.blocks + [self.pluto_sink]))

        self.pluto_source = make_pluto_source(
            cfg.uri, cfg.rx_freq, cfg.samp_rate, cfg.bandwidth,
            cfg.rx_gain_mode, cfg.rx_gain, cfg.buffer_size
        )
        self.rx = RxChain(cfg, on_frame, self.qpsk)
        self.frame_sink = self.rx.frame_sink
        self.connect(*([self.pluto_source] + self.rx.blocks))

    # -- runtime API used by the link layer -------------------------------
    def send_frame(self, payload, urgent=False):
        self.frame_source.send(payload, urgent=urgent)

    def tx_backlog(self):
        return self.frame_source.backlog()

    def phy_stats(self):
        s = dict(self.frame_sink.asm.stats())
        s["tx_frames"] = self.frame_source.n_frames_sent
        s["tx_bytes"] = self.frame_source.n_bytes_sent
        return s


class PhyConfig:
    """All tunables in one place."""

    def __init__(self, **kw):
        self.uri = "ip:192.168.1.10"
        self.tx_freq = 915e6
        self.rx_freq = 917e6
        self.samp_rate = 2e6
        self.sps = 4
        self.excess_bw = 0.35
        self.bandwidth = 1.5e6      # AD9361 analog filter, TX and RX
        self.buffer_size = 16384
        self.tx_atten = 10.0        # dB of TX attenuation (0 = max power)
        self.tx_amplitude = 0.6     # keep the DAC out of clipping
        self.rx_gain_mode = "slow_attack"
        self.rx_gain = 40.0         # only used when rx_gain_mode == "manual"
        self.timing_bw = 6.28 / 100.0
        self.phase_bw = 6.28 / 100.0
        self.eq_taps = 15
        self.eq_step = 1e-4
        self.access_threshold = 3   # bit errors tolerated in the access code
        self.max_buf_items = 4096   # per-block output buffer cap -> low latency
        for k, v in kw.items():
            if not hasattr(self, k):
                raise AttributeError("unknown PHY option %r" % k)
            setattr(self, k, v)

    @property
    def symbol_rate(self):
        return self.samp_rate / self.sps

    @property
    def bit_rate(self):
        return self.symbol_rate * 2.0

    def describe(self):
        return (
            "uri={uri}  tx={tx:.3f} MHz  rx={rx:.3f} MHz\n"
            "samp_rate={sr:.3f} MSPS  sps={sps}  -> {srate:.1f} ksym/s, "
            "{br:.1f} kbit/s raw\n"
            "excess_bw={ebw}  analog_bw={bw:.2f} MHz  tx_atten={atten} dB  "
            "rx_gain={gm}"
        ).format(
            uri=self.uri, tx=self.tx_freq / 1e6, rx=self.rx_freq / 1e6,
            sr=self.samp_rate / 1e6, sps=self.sps,
            srate=self.symbol_rate / 1e3, br=self.bit_rate / 1e3,
            ebw=self.excess_bw, bw=self.bandwidth / 1e6, atten=self.tx_atten,
            gm=self.rx_gain_mode,
        )
