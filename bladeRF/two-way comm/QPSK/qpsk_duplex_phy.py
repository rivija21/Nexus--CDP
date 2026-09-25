#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: QPSK duplex PHY (Pluto / E200)
# Description: Differential QPSK full-duplex FDD PHY for two Pluto/E200 nodes. Needs qpsk_framing.py in this directory.
# GNU Radio version: 3.10.12.0

from PyQt5 import Qt
from gnuradio import qtgui
from PyQt5 import QtCore
from gnuradio import analog
from gnuradio import blocks
import pmt
from gnuradio import blocks, gr
from gnuradio import digital
from gnuradio import iio
from gnuradio.filter import firdes
import qpsk_duplex_phy_frame_sink as frame_sink  # embedded python block
import qpsk_duplex_phy_frame_source as frame_source  # embedded python block
import sip
import threading
from gnuradio import gr
from gnuradio.filter import firdes
from gnuradio.fft import window
import sys
import signal
from argparse import ArgumentParser
from gnuradio.eng_arg import eng_float, intx
from gnuradio import eng_notation




class qpsk_duplex_phy(gr.top_block, Qt.QWidget):

    def __init__(self, role='a', uri='ip:192.168.1.10'):
        gr.top_block.__init__(self, "QPSK duplex PHY (Pluto / E200)", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("QPSK duplex PHY (Pluto / E200)")
        qtgui.util.check_set_qss()
        try:
            self.setWindowIcon(Qt.QIcon.fromTheme('gnuradio-grc'))
        except BaseException as exc:
            print(f"Qt GUI: Could not set Icon: {str(exc)}", file=sys.stderr)
        self.top_scroll_layout = Qt.QVBoxLayout()
        self.setLayout(self.top_scroll_layout)
        self.top_scroll = Qt.QScrollArea()
        self.top_scroll.setFrameStyle(Qt.QFrame.NoFrame)
        self.top_scroll_layout.addWidget(self.top_scroll)
        self.top_scroll.setWidgetResizable(True)
        self.top_widget = Qt.QWidget()
        self.top_scroll.setWidget(self.top_widget)
        self.top_layout = Qt.QVBoxLayout(self.top_widget)
        self.top_grid_layout = Qt.QGridLayout()
        self.top_layout.addLayout(self.top_grid_layout)

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "qpsk_duplex_phy")

        try:
            geometry = self.settings.value("geometry")
            if geometry:
                self.restoreGeometry(geometry)
        except BaseException as exc:
            print(f"Qt GUI: Could not restore geometry: {str(exc)}", file=sys.stderr)
        self.flowgraph_started = threading.Event()

        ##################################################
        # Parameters
        ##################################################
        self.role = role
        self.uri = uri

        ##################################################
        # Variables
        ##################################################
        self.sps = sps = 4
        self.qpsk = qpsk = digital.constellation_rect([0.707+0.707j, -0.707+0.707j, -0.707-0.707j, 0.707-0.707j], [0, 1, 2, 3],
        4, 2, 2, 1, 1).base()
        self.nfilts = nfilts = 32
        self.f_low = f_low = 915e6
        self.f_high = f_high = 917e6
        self.excess_bw = excess_bw = 0.35
        self.tx_freq = tx_freq = f_low if role == 'a' else f_high
        self.tx_atten = tx_atten = 10
        self.tx_amp = tx_amp = 0.6
        self.timing_bw = timing_bw = 6.28/100.0
        self.samp_rate = samp_rate = 2e6
        self.rx_freq = rx_freq = f_high if role == 'a' else f_low
        self.rrc_taps = rrc_taps = firdes.root_raised_cosine(nfilts, nfilts, 1.0/float(sps), excess_bw, 11*sps*nfilts)
        self.phase_bw = phase_bw = 6.28/100.0
        self.eq_alg = eq_alg = digital.adaptive_algorithm_cma( qpsk, 1e-4, 4).base()
        self.buffer_size = buffer_size = 16384
        self.bandwidth = bandwidth = 1.5e6
        self.access_threshold = access_threshold = 3

        ##################################################
        # Blocks
        ##################################################

        self._tx_atten_range = qtgui.Range(0, 40, 1, 10, 200)
        self._tx_atten_win = qtgui.RangeWidget(self._tx_atten_range, self.set_tx_atten, "TX attenuation (dB)", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_layout.addWidget(self._tx_atten_win)
        self._tx_amp_range = qtgui.Range(0.0, 1.0, 0.01, 0.6, 200)
        self._tx_amp_win = qtgui.RangeWidget(self._tx_amp_range, self.set_tx_amp, "TX amplitude", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_layout.addWidget(self._tx_amp_win)
        self._timing_bw_range = qtgui.Range(0.0, 0.2, 0.005, 6.28/100.0, 200)
        self._timing_bw_win = qtgui.RangeWidget(self._timing_bw_range, self.set_timing_bw, "Timing loop BW", "slider", float, QtCore.Qt.Horizontal)
        self.top_layout.addWidget(self._timing_bw_win)
        self._phase_bw_range = qtgui.Range(0.0, 0.5, 0.005, 6.28/100.0, 200)
        self._phase_bw_win = qtgui.RangeWidget(self._phase_bw_range, self.set_phase_bw, "Costas loop BW", "slider", float, QtCore.Qt.Horizontal)
        self.top_layout.addWidget(self._phase_bw_win)
        self.qtgui_freq_sink_x_1 = qtgui.freq_sink_c(
            1024, #size
            window.WIN_BLACKMAN_hARRIS, #wintype
            rx_freq, #fc
            samp_rate, #bw
            "RX spectrum", #name
            1,
            None # parent
        )
        self.qtgui_freq_sink_x_1.set_update_time(0.10)
        self.qtgui_freq_sink_x_1.set_y_axis((-140), 10)
        self.qtgui_freq_sink_x_1.set_y_label('Relative Gain', 'dB')
        self.qtgui_freq_sink_x_1.set_trigger_mode(qtgui.TRIG_MODE_FREE, 0.0, 0, "")
        self.qtgui_freq_sink_x_1.enable_autoscale(False)
        self.qtgui_freq_sink_x_1.enable_grid(True)
        self.qtgui_freq_sink_x_1.set_fft_average(0.2)
        self.qtgui_freq_sink_x_1.enable_axis_labels(True)
        self.qtgui_freq_sink_x_1.enable_control_panel(False)
        self.qtgui_freq_sink_x_1.set_fft_window_normalized(False)

        self.qtgui_freq_sink_x_1.disable_legend()


        labels = ['', '', '', '', '',
            '', '', '', '', '']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "green", "black", "cyan",
            "magenta", "yellow", "dark red", "dark green", "dark blue"]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.qtgui_freq_sink_x_1.set_line_label(i, "Data {0}".format(i))
            else:
                self.qtgui_freq_sink_x_1.set_line_label(i, labels[i])
            self.qtgui_freq_sink_x_1.set_line_width(i, widths[i])
            self.qtgui_freq_sink_x_1.set_line_color(i, colors[i])
            self.qtgui_freq_sink_x_1.set_line_alpha(i, alphas[i])

        self._qtgui_freq_sink_x_1_win = sip.wrapinstance(self.qtgui_freq_sink_x_1.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._qtgui_freq_sink_x_1_win, 1, 1, 1, 1)
        for r in range(1, 2):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(1, 2):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.qtgui_freq_sink_x_0 = qtgui.freq_sink_c(
            1024, #size
            window.WIN_BLACKMAN_hARRIS, #wintype
            tx_freq, #fc
            samp_rate, #bw
            "TX spectrum", #name
            1,
            None # parent
        )
        self.qtgui_freq_sink_x_0.set_update_time(0.10)
        self.qtgui_freq_sink_x_0.set_y_axis((-140), 10)
        self.qtgui_freq_sink_x_0.set_y_label('Relative Gain', 'dB')
        self.qtgui_freq_sink_x_0.set_trigger_mode(qtgui.TRIG_MODE_FREE, 0.0, 0, "")
        self.qtgui_freq_sink_x_0.enable_autoscale(False)
        self.qtgui_freq_sink_x_0.enable_grid(True)
        self.qtgui_freq_sink_x_0.set_fft_average(0.2)
        self.qtgui_freq_sink_x_0.enable_axis_labels(True)
        self.qtgui_freq_sink_x_0.enable_control_panel(False)
        self.qtgui_freq_sink_x_0.set_fft_window_normalized(False)

        self.qtgui_freq_sink_x_0.disable_legend()


        labels = ['', '', '', '', '',
            '', '', '', '', '']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "green", "black", "cyan",
            "magenta", "yellow", "dark red", "dark green", "dark blue"]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.qtgui_freq_sink_x_0.set_line_label(i, "Data {0}".format(i))
            else:
                self.qtgui_freq_sink_x_0.set_line_label(i, labels[i])
            self.qtgui_freq_sink_x_0.set_line_width(i, widths[i])
            self.qtgui_freq_sink_x_0.set_line_color(i, colors[i])
            self.qtgui_freq_sink_x_0.set_line_alpha(i, alphas[i])

        self._qtgui_freq_sink_x_0_win = sip.wrapinstance(self.qtgui_freq_sink_x_0.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._qtgui_freq_sink_x_0_win, 1, 0, 1, 1)
        for r in range(1, 2):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 1):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.qtgui_const_sink_x_0 = qtgui.const_sink_c(
            1024, #size
            "RX constellation", #name
            1, #number of inputs
            None # parent
        )
        self.qtgui_const_sink_x_0.set_update_time(0.10)
        self.qtgui_const_sink_x_0.set_y_axis((-2), 2)
        self.qtgui_const_sink_x_0.set_x_axis((-2), 2)
        self.qtgui_const_sink_x_0.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, "")
        self.qtgui_const_sink_x_0.enable_autoscale(False)
        self.qtgui_const_sink_x_0.enable_grid(True)
        self.qtgui_const_sink_x_0.enable_axis_labels(True)

        self.qtgui_const_sink_x_0.disable_legend()

        labels = ['', '', '', '', '',
            '', '', '', '', '']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "green", "black", "cyan",
            "magenta", "yellow", "dark red", "dark green", "dark blue"]
        styles = [0, 0, 0, 0, 0,
            0, 0, 0, 0, 0]
        markers = [0, 0, 0, 0, 0,
            0, 0, 0, 0, 0]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.qtgui_const_sink_x_0.set_line_label(i, "Data {0}".format(i))
            else:
                self.qtgui_const_sink_x_0.set_line_label(i, labels[i])
            self.qtgui_const_sink_x_0.set_line_width(i, widths[i])
            self.qtgui_const_sink_x_0.set_line_color(i, colors[i])
            self.qtgui_const_sink_x_0.set_line_style(i, styles[i])
            self.qtgui_const_sink_x_0.set_line_marker(i, markers[i])
            self.qtgui_const_sink_x_0.set_line_alpha(i, alphas[i])

        self._qtgui_const_sink_x_0_win = sip.wrapinstance(self.qtgui_const_sink_x_0.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._qtgui_const_sink_x_0_win, 2, 0, 1, 2)
        for r in range(2, 3):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 2):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.iio_pluto_source_0 = iio.fmcomms2_source_fc32(uri if uri else iio.get_pluto_uri(), [True, True], buffer_size)
        self.iio_pluto_source_0.set_len_tag_key('')
        self.iio_pluto_source_0.set_frequency(int(rx_freq))
        self.iio_pluto_source_0.set_samplerate(int(samp_rate))
        self.iio_pluto_source_0.set_gain_mode(0, 'slow_attack')
        self.iio_pluto_source_0.set_gain(0, 40)
        self.iio_pluto_source_0.set_quadrature(True)
        self.iio_pluto_source_0.set_rfdc(True)
        self.iio_pluto_source_0.set_bbdc(True)
        self.iio_pluto_source_0.set_filter_params('Auto', '', 0, 0)
        self.iio_pluto_sink_0 = iio.fmcomms2_sink_fc32(uri if uri else iio.get_pluto_uri(), [True, True], buffer_size, False)
        self.iio_pluto_sink_0.set_len_tag_key('')
        self.iio_pluto_sink_0.set_bandwidth(int(bandwidth))
        self.iio_pluto_sink_0.set_frequency(int(tx_freq))
        self.iio_pluto_sink_0.set_samplerate(int(samp_rate))
        self.iio_pluto_sink_0.set_attenuation(0, tx_atten)
        self.iio_pluto_sink_0.set_filter_params('Auto', '', 0, 0)
        self.frame_source = frame_source.blk(idle_chunk=1024)
        self.frame_source.set_max_output_buffer(4096)
        self.frame_sink = frame_sink.blk(tag_key='pkt_start')
        self.digital_pfb_clock_sync_xxx_0 = digital.pfb_clock_sync_ccf(sps, timing_bw, rrc_taps, nfilts, (nfilts/2), 1.5, 2)
        self.digital_pfb_clock_sync_xxx_0.set_max_output_buffer(4096)
        self.digital_linear_equalizer_0 = digital.linear_equalizer(15, 2, eq_alg, True, [ ], 'corr_est')
        self.digital_linear_equalizer_0.set_max_output_buffer(4096)
        self.digital_diff_decoder_bb_0 = digital.diff_decoder_bb(4, digital.DIFF_DIFFERENTIAL)
        self.digital_diff_decoder_bb_0.set_max_output_buffer(4096)
        self.digital_costas_loop_cc_0 = digital.costas_loop_cc(phase_bw, 4, False)
        self.digital_costas_loop_cc_0.set_max_output_buffer(4096)
        self.digital_correlate_access_code_tag_xx_0 = digital.correlate_access_code_tag_bb('1010110011011101101001001110001011110010100011000010000011111100', access_threshold, 'pkt_start')
        self.digital_correlate_access_code_tag_xx_0.set_max_output_buffer(4096)
        self.digital_constellation_modulator_0 = digital.generic_mod(
            constellation=qpsk,
            differential=True,
            samples_per_symbol=sps,
            pre_diff_code=True,
            excess_bw=excess_bw,
            verbose=False,
            log=False,
            truncate=False)
        self.digital_constellation_modulator_0.set_max_output_buffer(4096)
        self.digital_constellation_decoder_cb_0 = digital.constellation_decoder_cb(qpsk)
        self.digital_constellation_decoder_cb_0.set_max_output_buffer(4096)
        self.blocks_unpack_k_bits_bb_0 = blocks.unpack_k_bits_bb(2)
        self.blocks_unpack_k_bits_bb_0.set_max_output_buffer(4096)
        self.blocks_multiply_const_vxx_0 = blocks.multiply_const_cc(tx_amp)
        self.blocks_multiply_const_vxx_0.set_max_output_buffer(4096)
        self.blocks_message_strobe_0 = blocks.message_strobe(pmt.cons(pmt.PMT_NIL, pmt.init_u8vector(16, [81,80,83,75,32,108,105,110,107,32,116,101,115,116,32,33])), 2000)
        self.blocks_message_debug_0 = blocks.message_debug(True, gr.log_levels.info)
        self.analog_agc2_xx_0 = analog.agc2_cc((1e-1), (1e-2), 1.0, 1.0, 65536)
        self.analog_agc2_xx_0.set_max_output_buffer(4096)


        ##################################################
        # Connections
        ##################################################
        self.msg_connect((self.blocks_message_strobe_0, 'strobe'), (self.frame_source, 'send'))
        self.msg_connect((self.frame_sink, 'rx'), (self.blocks_message_debug_0, 'print'))
        self.connect((self.analog_agc2_xx_0, 0), (self.digital_pfb_clock_sync_xxx_0, 0))
        self.connect((self.analog_agc2_xx_0, 0), (self.qtgui_freq_sink_x_1, 0))
        self.connect((self.blocks_multiply_const_vxx_0, 0), (self.iio_pluto_sink_0, 0))
        self.connect((self.blocks_multiply_const_vxx_0, 0), (self.qtgui_freq_sink_x_0, 0))
        self.connect((self.blocks_unpack_k_bits_bb_0, 0), (self.digital_correlate_access_code_tag_xx_0, 0))
        self.connect((self.digital_constellation_decoder_cb_0, 0), (self.digital_diff_decoder_bb_0, 0))
        self.connect((self.digital_constellation_modulator_0, 0), (self.blocks_multiply_const_vxx_0, 0))
        self.connect((self.digital_correlate_access_code_tag_xx_0, 0), (self.frame_sink, 0))
        self.connect((self.digital_costas_loop_cc_0, 0), (self.digital_constellation_decoder_cb_0, 0))
        self.connect((self.digital_costas_loop_cc_0, 0), (self.qtgui_const_sink_x_0, 0))
        self.connect((self.digital_diff_decoder_bb_0, 0), (self.blocks_unpack_k_bits_bb_0, 0))
        self.connect((self.digital_linear_equalizer_0, 0), (self.digital_costas_loop_cc_0, 0))
        self.connect((self.digital_pfb_clock_sync_xxx_0, 0), (self.digital_linear_equalizer_0, 0))
        self.connect((self.frame_source, 0), (self.digital_constellation_modulator_0, 0))
        self.connect((self.iio_pluto_source_0, 0), (self.analog_agc2_xx_0, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "qpsk_duplex_phy")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_role(self):
        return self.role

    def set_role(self, role):
        self.role = role
        self.set_tx_freq(self.f_low if self.role == 'a' else self.f_high)
        self.set_rx_freq(self.f_high if self.role == 'a' else self.f_low)

    def get_uri(self):
        return self.uri

    def set_uri(self, uri):
        self.uri = uri

    def get_sps(self):
        return self.sps

    def set_sps(self, sps):
        self.sps = sps
        self.set_rrc_taps(firdes.root_raised_cosine(self.nfilts, self.nfilts, 1.0/float(self.sps), self.excess_bw, 11*self.sps*self.nfilts))

    def get_qpsk(self):
        return self.qpsk

    def set_qpsk(self, qpsk):
        self.qpsk = qpsk
        self.digital_constellation_decoder_cb_0.set_constellation(self.qpsk)

    def get_nfilts(self):
        return self.nfilts

    def set_nfilts(self, nfilts):
        self.nfilts = nfilts
        self.set_rrc_taps(firdes.root_raised_cosine(self.nfilts, self.nfilts, 1.0/float(self.sps), self.excess_bw, 11*self.sps*self.nfilts))

    def get_f_low(self):
        return self.f_low

    def set_f_low(self, f_low):
        self.f_low = f_low
        self.set_tx_freq(self.f_low if self.role == 'a' else self.f_high)
        self.set_rx_freq(self.f_high if self.role == 'a' else self.f_low)

    def get_f_high(self):
        return self.f_high

    def set_f_high(self, f_high):
        self.f_high = f_high
        self.set_tx_freq(self.f_low if self.role == 'a' else self.f_high)
        self.set_rx_freq(self.f_high if self.role == 'a' else self.f_low)

    def get_excess_bw(self):
        return self.excess_bw

    def set_excess_bw(self, excess_bw):
        self.excess_bw = excess_bw
        self.set_rrc_taps(firdes.root_raised_cosine(self.nfilts, self.nfilts, 1.0/float(self.sps), self.excess_bw, 11*self.sps*self.nfilts))

    def get_tx_freq(self):
        return self.tx_freq

    def set_tx_freq(self, tx_freq):
        self.tx_freq = tx_freq
        self.iio_pluto_sink_0.set_frequency(int(self.tx_freq))
        self.qtgui_freq_sink_x_0.set_frequency_range(self.tx_freq, self.samp_rate)

    def get_tx_atten(self):
        return self.tx_atten

    def set_tx_atten(self, tx_atten):
        self.tx_atten = tx_atten
        self.iio_pluto_sink_0.set_attenuation(0,self.tx_atten)

    def get_tx_amp(self):
        return self.tx_amp

    def set_tx_amp(self, tx_amp):
        self.tx_amp = tx_amp
        self.blocks_multiply_const_vxx_0.set_k(self.tx_amp)

    def get_timing_bw(self):
        return self.timing_bw

    def set_timing_bw(self, timing_bw):
        self.timing_bw = timing_bw
        self.digital_pfb_clock_sync_xxx_0.set_loop_bandwidth(self.timing_bw)

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.iio_pluto_sink_0.set_samplerate(int(self.samp_rate))
        self.qtgui_freq_sink_x_0.set_frequency_range(self.tx_freq, self.samp_rate)
        self.iio_pluto_source_0.set_samplerate(int(self.samp_rate))
        self.qtgui_freq_sink_x_1.set_frequency_range(self.rx_freq, self.samp_rate)

    def get_rx_freq(self):
        return self.rx_freq

    def set_rx_freq(self, rx_freq):
        self.rx_freq = rx_freq
        self.iio_pluto_source_0.set_frequency(int(self.rx_freq))
        self.qtgui_freq_sink_x_1.set_frequency_range(self.rx_freq, self.samp_rate)

    def get_rrc_taps(self):
        return self.rrc_taps

    def set_rrc_taps(self, rrc_taps):
        self.rrc_taps = rrc_taps
        self.digital_pfb_clock_sync_xxx_0.update_taps(self.rrc_taps)

    def get_phase_bw(self):
        return self.phase_bw

    def set_phase_bw(self, phase_bw):
        self.phase_bw = phase_bw
        self.digital_costas_loop_cc_0.set_loop_bandwidth(self.phase_bw)

    def get_eq_alg(self):
        return self.eq_alg

    def set_eq_alg(self, eq_alg):
        self.eq_alg = eq_alg

    def get_buffer_size(self):
        return self.buffer_size

    def set_buffer_size(self, buffer_size):
        self.buffer_size = buffer_size

    def get_bandwidth(self):
        return self.bandwidth

    def set_bandwidth(self, bandwidth):
        self.bandwidth = bandwidth
        self.iio_pluto_sink_0.set_bandwidth(int(self.bandwidth))

    def get_access_threshold(self):
        return self.access_threshold

    def set_access_threshold(self, access_threshold):
        self.access_threshold = access_threshold
        self.digital_correlate_access_code_tag_xx_0.set_threshold(self.access_threshold)



def argument_parser():
    description = 'Differential QPSK full-duplex FDD PHY for two Pluto/E200 nodes. Needs qpsk_framing.py in this directory.'
    parser = ArgumentParser(description=description)
    parser.add_argument(
        "-r", "--role", dest="role", type=str, default='a',
        help="Set FDD role (a or b) [default=%(default)r]")
    parser.add_argument(
        "-u", "--uri", dest="uri", type=str, default='ip:192.168.1.10',
        help="Set Pluto URI [default=%(default)r]")
    return parser


def main(top_block_cls=qpsk_duplex_phy, options=None):
    if options is None:
        options = argument_parser().parse_args()

    qapp = Qt.QApplication(sys.argv)

    tb = top_block_cls(role=options.role, uri=options.uri)

    tb.start()
    tb.flowgraph_started.set()

    tb.show()

    def sig_handler(sig=None, frame=None):
        tb.stop()
        tb.wait()

        Qt.QApplication.quit()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    timer = Qt.QTimer()
    timer.start(500)
    timer.timeout.connect(lambda: None)

    qapp.exec_()

if __name__ == '__main__':
    main()
