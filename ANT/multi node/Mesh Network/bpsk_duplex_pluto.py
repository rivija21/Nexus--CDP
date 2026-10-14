#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: BPSK Mesh Node (Pluto, 4-channel FDMA, r6.1)
# Author: Rivija Pesara (framing from Barry Duggan BPSK example)
# Description: BPSK network node: 4-channel FDMA mesh (6 MS/s, one-sided plan), self-assigned channels and addresses, closed-loop gain staging, Selective Repeat ARQ per peer, end-to-end encryption per pair, browser chat (r6.1-mesh)
# GNU Radio version: 3.10.12.0

from PyQt5 import Qt
from gnuradio import qtgui
from gnuradio import analog
from gnuradio import blocks
import pmt
from gnuradio import digital
from gnuradio import filter
from gnuradio.filter import firdes
from gnuradio import gr
from gnuradio.fft import window
import sys
import signal
from PyQt5 import Qt
from argparse import ArgumentParser
from gnuradio.eng_arg import eng_float, intx
from gnuradio import eng_notation
from gnuradio import gr, pdu
from gnuradio import iio
import bpsk_duplex_pluto_deframer_ch0 as deframer_ch0  # embedded python block
import bpsk_duplex_pluto_deframer_ch1 as deframer_ch1  # embedded python block
import bpsk_duplex_pluto_deframer_ch2 as deframer_ch2  # embedded python block
import bpsk_duplex_pluto_deframer_ch3 as deframer_ch3  # embedded python block
import bpsk_duplex_pluto_link_layer as link_layer  # embedded python block
import bpsk_duplex_pluto_tx_framer as tx_framer  # embedded python block
import sip
import threading



class bpsk_duplex_pluto(gr.top_block, Qt.QWidget):

    def __init__(self, agc=1, arq_window=32, center_freq=910.2e6, ch_first=450e3, ch_spacing=600e3, channel=(-1), http_port=8088, keys_dir='keys', my_addr=0, nickname='', peer_addr=0, plan_file='', postamble_size=64, preamble_size=64, radio_rate=6e6, rx_buffer=65536, rx_gain=30, security=1, telemetry=1, tx_atten=50, tx_buffer=49152, uri='ip:192.168.1.10'):
        gr.top_block.__init__(self, "BPSK Mesh Node (Pluto, 4-channel FDMA, r6.1)", catch_exceptions=False)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("BPSK Mesh Node (Pluto, 4-channel FDMA, r6.1)")
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

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "bpsk_duplex_pluto")

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
        self.agc = agc
        self.arq_window = arq_window
        self.center_freq = center_freq
        self.ch_first = ch_first
        self.ch_spacing = ch_spacing
        self.channel = channel
        self.http_port = http_port
        self.keys_dir = keys_dir
        self.my_addr = my_addr
        self.nickname = nickname
        self.peer_addr = peer_addr
        self.plan_file = plan_file
        self.postamble_size = postamble_size
        self.preamble_size = preamble_size
        self.radio_rate = radio_rate
        self.rx_buffer = rx_buffer
        self.rx_gain = rx_gain
        self.security = security
        self.telemetry = telemetry
        self.tx_atten = tx_atten
        self.tx_buffer = tx_buffer
        self.uri = uri

        ##################################################
        # Variables
        ##################################################
        self.tx_interp = tx_interp = 24
        self.sym_rate = sym_rate = radio_rate/tx_interp
        self.sps = sps = 4
        self.tx_scale = tx_scale = 0.35
        self.ch_rate = ch_rate = sps*sym_rate
        self.tx_taps = tx_taps = [tx_scale*t for t in firdes.root_raised_cosine(tx_interp, tx_interp, 1.0, 0.35, 16*tx_interp+1)]
        self.rrc_taps = rrc_taps = firdes.root_raised_cosine(1.0, ch_rate, sym_rate, 0.35, 11*sps)
        self.queue_ahead = queue_ahead = 0.03
        self.max_retries = max_retries = 5
        self.frag_size = frag_size = 256
        self.decim = decim = tx_interp//sps
        self.constel = constel = digital.constellation_bpsk().base()
        self.constel.set_npwr(1.0)
        self.ch_taps = ch_taps = firdes.low_pass_2(1.0, radio_rate, 0.5*ch_spacing, 0.4167*ch_spacing, 65)
        self.ack_timeout = ack_timeout = 0.5

        ##################################################
        # Blocks
        ##################################################

        self.tx_spectrum = qtgui.freq_sink_c(
            1024, #size
            window.WIN_BLACKMAN_hARRIS, #wintype
            center_freq, #fc
            radio_rate, #bw
            "TX spectrum", #name
            1,
            None # parent
        )
        self.tx_spectrum.set_update_time(0.20)
        self.tx_spectrum.set_y_axis((-120), 0)
        self.tx_spectrum.set_y_label('Relative Gain', 'dB')
        self.tx_spectrum.set_trigger_mode(qtgui.TRIG_MODE_FREE, 0.0, 0, "")
        self.tx_spectrum.enable_autoscale(False)
        self.tx_spectrum.enable_grid(True)
        self.tx_spectrum.set_fft_average(0.2)
        self.tx_spectrum.enable_axis_labels(True)
        self.tx_spectrum.enable_control_panel(False)
        self.tx_spectrum.set_fft_window_normalized(False)



        labels = ["", '', '', '', '',
            '', '', '', '', '']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "green", "black", "cyan",
            "magenta", "yellow", "dark red", "dark green", "dark blue"]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.tx_spectrum.set_line_label(i, "Data {0}".format(i))
            else:
                self.tx_spectrum.set_line_label(i, labels[i])
            self.tx_spectrum.set_line_width(i, widths[i])
            self.tx_spectrum.set_line_color(i, colors[i])
            self.tx_spectrum.set_line_alpha(i, alphas[i])

        self._tx_spectrum_win = sip.wrapinstance(self.tx_spectrum.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._tx_spectrum_win, 5, 0, 2, 2)
        for r in range(5, 7):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 2):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.tx_spec_keep = blocks.keep_m_in_n(gr.sizeof_gr_complex, 1024, 61440, 0)
        self.tx_shift = blocks.rotator_cc(0, False)
        self.tx_interp_filter = filter.interp_fir_filter_ccf(tx_interp, tx_taps)
        self.tx_interp_filter.declare_sample_delay(0)
        self.tx_framer = tx_framer.blk(preamble_size=preamble_size, postamble_size=postamble_size, sym_rate=sym_rate, max_backlog=0.5, batch=2560)
        self.tx_framer.set_min_output_buffer(7680)
        self.tx_framer.set_max_output_buffer(7680)
        self.tx_attr = iio.attr_sink(uri, "ad9361-phy", "voltage0", 0, True)
        self.symbol_sync_ch3 = digital.symbol_sync_cc(
            digital.TED_SIGNAL_TIMES_SLOPE_ML,
            sps,
            0.02,
            1.0,
            0.1,
            0.01,
            1,
            constel.base(),
            digital.IR_MMSE_8TAP,
            32,
            [])
        self.symbol_sync_ch2 = digital.symbol_sync_cc(
            digital.TED_SIGNAL_TIMES_SLOPE_ML,
            sps,
            0.02,
            1.0,
            0.1,
            0.01,
            1,
            constel.base(),
            digital.IR_MMSE_8TAP,
            32,
            [])
        self.symbol_sync_ch1 = digital.symbol_sync_cc(
            digital.TED_SIGNAL_TIMES_SLOPE_ML,
            sps,
            0.02,
            1.0,
            0.1,
            0.01,
            1,
            constel.base(),
            digital.IR_MMSE_8TAP,
            32,
            [])
        self.symbol_sync_ch0 = digital.symbol_sync_cc(
            digital.TED_SIGNAL_TIMES_SLOPE_ML,
            sps,
            0.02,
            1.0,
            0.1,
            0.01,
            1,
            constel.base(),
            digital.IR_MMSE_8TAP,
            32,
            [])
        self.rx_spectrum = qtgui.freq_sink_c(
            1024, #size
            window.WIN_BLACKMAN_hARRIS, #wintype
            center_freq, #fc
            radio_rate, #bw
            "RX spectrum", #name
            1,
            None # parent
        )
        self.rx_spectrum.set_update_time(0.20)
        self.rx_spectrum.set_y_axis((-120), 0)
        self.rx_spectrum.set_y_label('Relative Gain', 'dB')
        self.rx_spectrum.set_trigger_mode(qtgui.TRIG_MODE_FREE, 0.0, 0, "")
        self.rx_spectrum.enable_autoscale(False)
        self.rx_spectrum.enable_grid(True)
        self.rx_spectrum.set_fft_average(0.2)
        self.rx_spectrum.enable_axis_labels(True)
        self.rx_spectrum.enable_control_panel(False)
        self.rx_spectrum.set_fft_window_normalized(False)



        labels = ["", '', '', '', '',
            '', '', '', '', '']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "green", "black", "cyan",
            "magenta", "yellow", "dark red", "dark green", "dark blue"]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.rx_spectrum.set_line_label(i, "Data {0}".format(i))
            else:
                self.rx_spectrum.set_line_label(i, labels[i])
            self.rx_spectrum.set_line_width(i, widths[i])
            self.rx_spectrum.set_line_color(i, colors[i])
            self.rx_spectrum.set_line_alpha(i, alphas[i])

        self._rx_spectrum_win = sip.wrapinstance(self.rx_spectrum.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._rx_spectrum_win, 1, 0, 2, 2)
        for r in range(1, 3):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 2):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.rx_spec_keep = blocks.keep_m_in_n(gr.sizeof_gr_complex, 1024, 61440, 0)
        self.rx_attr = iio.attr_sink(uri, "ad9361-phy", "voltage0", 0, False)
        self.rrc_ch3 = filter.fft_filter_ccc(1, rrc_taps, 1)
        self.rrc_ch3.declare_sample_delay(0)
        self.rrc_ch2 = filter.fft_filter_ccc(1, rrc_taps, 1)
        self.rrc_ch2.declare_sample_delay(0)
        self.rrc_ch1 = filter.fft_filter_ccc(1, rrc_taps, 1)
        self.rrc_ch1.declare_sample_delay(0)
        self.rrc_ch0 = filter.fft_filter_ccc(1, rrc_taps, 1)
        self.rrc_ch0.declare_sample_delay(0)
        self.qtgui_edit_box_msg_0 = qtgui.edit_box_msg(qtgui.STRING, "", "Console  (the chat lives in the browser; /stats, /gain, /help here)", False, True, "", None)
        self._qtgui_edit_box_msg_0_win = sip.wrapinstance(self.qtgui_edit_box_msg_0.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._qtgui_edit_box_msg_0_win, 0, 0, 1, 2)
        for r in range(0, 1):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 2):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.meas_pkt_ch3 = blocks.stream_to_tagged_stream(gr.sizeof_gr_complex, 1, 512, "packet_len")
        self.meas_pkt_ch3.set_min_output_buffer(2048)
        self.meas_pkt_ch2 = blocks.stream_to_tagged_stream(gr.sizeof_gr_complex, 1, 512, "packet_len")
        self.meas_pkt_ch2.set_min_output_buffer(2048)
        self.meas_pkt_ch1 = blocks.stream_to_tagged_stream(gr.sizeof_gr_complex, 1, 512, "packet_len")
        self.meas_pkt_ch1.set_min_output_buffer(2048)
        self.meas_pkt_ch0 = blocks.stream_to_tagged_stream(gr.sizeof_gr_complex, 1, 512, "packet_len")
        self.meas_pkt_ch0.set_min_output_buffer(2048)
        self.meas_pdu_ch3 = pdu.tagged_stream_to_pdu(gr.types.complex_t, 'packet_len')
        self.meas_pdu_ch2 = pdu.tagged_stream_to_pdu(gr.types.complex_t, 'packet_len')
        self.meas_pdu_ch1 = pdu.tagged_stream_to_pdu(gr.types.complex_t, 'packet_len')
        self.meas_pdu_ch0 = pdu.tagged_stream_to_pdu(gr.types.complex_t, 'packet_len')
        self.meas_keep_ch3 = blocks.keep_m_in_n(gr.sizeof_gr_complex, 512, 25000, 0)
        self.meas_keep_ch2 = blocks.keep_m_in_n(gr.sizeof_gr_complex, 512, 25000, 0)
        self.meas_keep_ch1 = blocks.keep_m_in_n(gr.sizeof_gr_complex, 512, 25000, 0)
        self.meas_keep_ch0 = blocks.keep_m_in_n(gr.sizeof_gr_complex, 512, 25000, 0)
        self.link_layer = link_layer.blk(my_addr=my_addr, peer_addr=peer_addr, ack_timeout=ack_timeout, max_retries=max_retries, frag_size=frag_size, queue_ahead=queue_ahead, sym_rate=sym_rate, overhead=preamble_size+postamble_size+12, rx_dir=".", nickname=nickname, http_port=http_port, open_ui=True, center_freq=center_freq, security=security, keys_dir=keys_dir, window=arq_window, channel=channel, ch_spacing=ch_spacing, ch_first=ch_first, radio_rate=radio_rate, rx_gain=rx_gain, tx_atten=tx_atten, agc=agc, telemetry=telemetry, plan_file=plan_file, count_ou=True)
        self.iq_pkt_ch3 = blocks.stream_to_tagged_stream(gr.sizeof_gr_complex, 1, 4096, "packet_len")
        self.iq_pkt_ch3.set_min_output_buffer(16384)
        self.iq_pkt_ch2 = blocks.stream_to_tagged_stream(gr.sizeof_gr_complex, 1, 4096, "packet_len")
        self.iq_pkt_ch2.set_min_output_buffer(16384)
        self.iq_pkt_ch1 = blocks.stream_to_tagged_stream(gr.sizeof_gr_complex, 1, 4096, "packet_len")
        self.iq_pkt_ch1.set_min_output_buffer(16384)
        self.iq_pkt_ch0 = blocks.stream_to_tagged_stream(gr.sizeof_gr_complex, 1, 4096, "packet_len")
        self.iq_pkt_ch0.set_min_output_buffer(16384)
        self.iq_pdu_ch3 = pdu.tagged_stream_to_pdu(gr.types.complex_t, 'packet_len')
        self.iq_pdu_ch2 = pdu.tagged_stream_to_pdu(gr.types.complex_t, 'packet_len')
        self.iq_pdu_ch1 = pdu.tagged_stream_to_pdu(gr.types.complex_t, 'packet_len')
        self.iq_pdu_ch0 = pdu.tagged_stream_to_pdu(gr.types.complex_t, 'packet_len')
        self.iq_keep_ch3 = blocks.keep_m_in_n(gr.sizeof_gr_complex, 4096, 100000, 0)
        self.iq_keep_ch2 = blocks.keep_m_in_n(gr.sizeof_gr_complex, 4096, 100000, 0)
        self.iq_keep_ch1 = blocks.keep_m_in_n(gr.sizeof_gr_complex, 4096, 100000, 0)
        self.iq_keep_ch0 = blocks.keep_m_in_n(gr.sizeof_gr_complex, 4096, 100000, 0)
        self.iio_pluto_source_0 = iio.fmcomms2_source_fc32(uri if uri else iio.get_pluto_uri(), [True, True], rx_buffer)
        self.iio_pluto_source_0.set_len_tag_key('')
        self.iio_pluto_source_0.set_frequency((int)(center_freq))
        self.iio_pluto_source_0.set_samplerate((int)(radio_rate))
        self.iio_pluto_source_0.set_gain_mode(0, 'manual')
        self.iio_pluto_source_0.set_gain(0, rx_gain)
        self.iio_pluto_source_0.set_quadrature(True)
        self.iio_pluto_source_0.set_rfdc(True)
        self.iio_pluto_source_0.set_bbdc(True)
        self.iio_pluto_source_0.set_filter_params('Auto', '', 0, 0)
        self.iio_pluto_sink_0 = iio.fmcomms2_sink_fc32(uri if uri else iio.get_pluto_uri(), [True, True], tx_buffer, False)
        self.iio_pluto_sink_0.set_len_tag_key('')
        self.iio_pluto_sink_0.set_bandwidth(((int)(0.9*radio_rate)))
        self.iio_pluto_sink_0.set_frequency((int)(center_freq))
        self.iio_pluto_sink_0.set_samplerate((int)(radio_rate))
        self.iio_pluto_sink_0.set_attenuation(0, tx_atten)
        self.iio_pluto_sink_0.set_filter_params('Auto', '', 0, 0)
        self.diff_decoder_ch3 = digital.diff_decoder_bb(2, digital.DIFF_DIFFERENTIAL)
        self.diff_decoder_ch2 = digital.diff_decoder_bb(2, digital.DIFF_DIFFERENTIAL)
        self.diff_decoder_ch1 = digital.diff_decoder_bb(2, digital.DIFF_DIFFERENTIAL)
        self.diff_decoder_ch0 = digital.diff_decoder_bb(2, digital.DIFF_DIFFERENTIAL)
        self.deframer_ch3 = deframer_ch3.blk(channel=3, max_len=600, batch=8192)
        self.deframer_ch2 = deframer_ch2.blk(channel=2, max_len=600, batch=8192)
        self.deframer_ch1 = deframer_ch1.blk(channel=1, max_len=600, batch=8192)
        self.deframer_ch0 = deframer_ch0.blk(channel=0, max_len=600, batch=8192)
        self.decoder_ch3 = digital.constellation_decoder_cb(constel)
        self.decoder_ch2 = digital.constellation_decoder_cb(constel)
        self.decoder_ch1 = digital.constellation_decoder_cb(constel)
        self.decoder_ch0 = digital.constellation_decoder_cb(constel)
        self.costas_ch3 = digital.costas_loop_cc((3.14/100), 2, False)
        self.costas_ch2 = digital.costas_loop_cc((3.14/100), 2, False)
        self.costas_ch1 = digital.costas_loop_cc((3.14/100), 2, False)
        self.costas_ch0 = digital.costas_loop_cc((3.14/100), 2, False)
        self.const_sink_ch3 = qtgui.const_sink_c(
            512, #size
            "Channel 3", #name
            1, #number of inputs
            None # parent
        )
        self.const_sink_ch3.set_update_time(0.20)
        self.const_sink_ch3.set_y_axis((-2), 2)
        self.const_sink_ch3.set_x_axis((-2), 2)
        self.const_sink_ch3.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, "")
        self.const_sink_ch3.enable_autoscale(False)
        self.const_sink_ch3.enable_grid(True)
        self.const_sink_ch3.enable_axis_labels(True)


        labels = ["ch 3", '', '', '', '',
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
                self.const_sink_ch3.set_line_label(i, "Data {0}".format(i))
            else:
                self.const_sink_ch3.set_line_label(i, labels[i])
            self.const_sink_ch3.set_line_width(i, widths[i])
            self.const_sink_ch3.set_line_color(i, colors[i])
            self.const_sink_ch3.set_line_style(i, styles[i])
            self.const_sink_ch3.set_line_marker(i, markers[i])
            self.const_sink_ch3.set_line_alpha(i, alphas[i])

        self._const_sink_ch3_win = sip.wrapinstance(self.const_sink_ch3.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._const_sink_ch3_win, 4, 1, 1, 1)
        for r in range(4, 5):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(1, 2):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.const_sink_ch2 = qtgui.const_sink_c(
            512, #size
            "Channel 2", #name
            1, #number of inputs
            None # parent
        )
        self.const_sink_ch2.set_update_time(0.20)
        self.const_sink_ch2.set_y_axis((-2), 2)
        self.const_sink_ch2.set_x_axis((-2), 2)
        self.const_sink_ch2.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, "")
        self.const_sink_ch2.enable_autoscale(False)
        self.const_sink_ch2.enable_grid(True)
        self.const_sink_ch2.enable_axis_labels(True)


        labels = ["ch 2", '', '', '', '',
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
                self.const_sink_ch2.set_line_label(i, "Data {0}".format(i))
            else:
                self.const_sink_ch2.set_line_label(i, labels[i])
            self.const_sink_ch2.set_line_width(i, widths[i])
            self.const_sink_ch2.set_line_color(i, colors[i])
            self.const_sink_ch2.set_line_style(i, styles[i])
            self.const_sink_ch2.set_line_marker(i, markers[i])
            self.const_sink_ch2.set_line_alpha(i, alphas[i])

        self._const_sink_ch2_win = sip.wrapinstance(self.const_sink_ch2.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._const_sink_ch2_win, 4, 0, 1, 1)
        for r in range(4, 5):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 1):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.const_sink_ch1 = qtgui.const_sink_c(
            512, #size
            "Channel 1", #name
            1, #number of inputs
            None # parent
        )
        self.const_sink_ch1.set_update_time(0.20)
        self.const_sink_ch1.set_y_axis((-2), 2)
        self.const_sink_ch1.set_x_axis((-2), 2)
        self.const_sink_ch1.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, "")
        self.const_sink_ch1.enable_autoscale(False)
        self.const_sink_ch1.enable_grid(True)
        self.const_sink_ch1.enable_axis_labels(True)


        labels = ["ch 1", '', '', '', '',
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
                self.const_sink_ch1.set_line_label(i, "Data {0}".format(i))
            else:
                self.const_sink_ch1.set_line_label(i, labels[i])
            self.const_sink_ch1.set_line_width(i, widths[i])
            self.const_sink_ch1.set_line_color(i, colors[i])
            self.const_sink_ch1.set_line_style(i, styles[i])
            self.const_sink_ch1.set_line_marker(i, markers[i])
            self.const_sink_ch1.set_line_alpha(i, alphas[i])

        self._const_sink_ch1_win = sip.wrapinstance(self.const_sink_ch1.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._const_sink_ch1_win, 3, 1, 1, 1)
        for r in range(3, 4):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(1, 2):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.const_sink_ch0 = qtgui.const_sink_c(
            512, #size
            "Channel 0", #name
            1, #number of inputs
            None # parent
        )
        self.const_sink_ch0.set_update_time(0.20)
        self.const_sink_ch0.set_y_axis((-2), 2)
        self.const_sink_ch0.set_x_axis((-2), 2)
        self.const_sink_ch0.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, "")
        self.const_sink_ch0.enable_autoscale(False)
        self.const_sink_ch0.enable_grid(True)
        self.const_sink_ch0.enable_axis_labels(True)


        labels = ["ch 0", '', '', '', '',
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
                self.const_sink_ch0.set_line_label(i, "Data {0}".format(i))
            else:
                self.const_sink_ch0.set_line_label(i, labels[i])
            self.const_sink_ch0.set_line_width(i, widths[i])
            self.const_sink_ch0.set_line_color(i, colors[i])
            self.const_sink_ch0.set_line_style(i, styles[i])
            self.const_sink_ch0.set_line_marker(i, markers[i])
            self.const_sink_ch0.set_line_alpha(i, alphas[i])

        self._const_sink_ch0_win = sip.wrapinstance(self.const_sink_ch0.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._const_sink_ch0_win, 3, 0, 1, 1)
        for r in range(3, 4):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 1):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.const_keep_ch3 = blocks.keep_m_in_n(gr.sizeof_gr_complex, 32, 512, 0)
        self.const_keep_ch2 = blocks.keep_m_in_n(gr.sizeof_gr_complex, 32, 512, 0)
        self.const_keep_ch1 = blocks.keep_m_in_n(gr.sizeof_gr_complex, 32, 512, 0)
        self.const_keep_ch0 = blocks.keep_m_in_n(gr.sizeof_gr_complex, 32, 512, 0)
        self.chan_filter_ch3 = filter.freq_xlating_fir_filter_ccf(decim, ch_taps, (ch_first+3*ch_spacing), radio_rate)
        self.chan_filter_ch2 = filter.freq_xlating_fir_filter_ccf(decim, ch_taps, (ch_first+2*ch_spacing), radio_rate)
        self.chan_filter_ch1 = filter.freq_xlating_fir_filter_ccf(decim, ch_taps, (ch_first+1*ch_spacing), radio_rate)
        self.chan_filter_ch0 = filter.freq_xlating_fir_filter_ccf(decim, ch_taps, (ch_first+0*ch_spacing), radio_rate)
        self.blocks_message_strobe_0 = blocks.message_strobe(pmt.intern("tick"), 10)
        self.bits_to_vec_ch3 = blocks.stream_to_vector(gr.sizeof_char*1, 8192)
        self.bits_to_vec_ch3.set_min_output_buffer(128)
        self.bits_to_vec_ch2 = blocks.stream_to_vector(gr.sizeof_char*1, 8192)
        self.bits_to_vec_ch2.set_min_output_buffer(128)
        self.bits_to_vec_ch1 = blocks.stream_to_vector(gr.sizeof_char*1, 8192)
        self.bits_to_vec_ch1.set_min_output_buffer(128)
        self.bits_to_vec_ch0 = blocks.stream_to_vector(gr.sizeof_char*1, 8192)
        self.bits_to_vec_ch0.set_min_output_buffer(128)
        self.agc_ch3 = analog.agc3_cc(1.0, (1e-4), 1.0, 1.0, 1, 1e5)
        self.agc_ch2 = analog.agc3_cc(1.0, (1e-4), 1.0, 1.0, 1, 1e5)
        self.agc_ch1 = analog.agc3_cc(1.0, (1e-4), 1.0, 1.0, 1, 1e5)
        self.agc_ch0 = analog.agc3_cc(1.0, (1e-4), 1.0, 1.0, 1, 1e5)
        self.afc_ch3 = blocks.rotator_cc(0, False)
        self.afc_ch2 = blocks.rotator_cc(0, False)
        self.afc_ch1 = blocks.rotator_cc(0, False)
        self.afc_ch0 = blocks.rotator_cc(0, False)
        self.adc_pkt = blocks.stream_to_tagged_stream(gr.sizeof_gr_complex, 1, 8192, "packet_len")
        self.adc_pkt.set_min_output_buffer(32768)
        self.adc_pdu = pdu.tagged_stream_to_pdu(gr.types.complex_t, 'packet_len')
        self.adc_keep = blocks.keep_m_in_n(gr.sizeof_gr_complex, 8192, 300000, 0)


        ##################################################
        # Connections
        ##################################################
        self.msg_connect((self.adc_pdu, 'pdus'), (self.link_layer, 'adc'))
        self.msg_connect((self.blocks_message_strobe_0, 'strobe'), (self.link_layer, 'tick'))
        self.msg_connect((self.deframer_ch0, 'pdus'), (self.link_layer, 'rx0'))
        self.msg_connect((self.deframer_ch1, 'pdus'), (self.link_layer, 'rx1'))
        self.msg_connect((self.deframer_ch2, 'pdus'), (self.link_layer, 'rx2'))
        self.msg_connect((self.deframer_ch3, 'pdus'), (self.link_layer, 'rx3'))
        self.msg_connect((self.iq_pdu_ch0, 'pdus'), (self.link_layer, 'iq0'))
        self.msg_connect((self.iq_pdu_ch1, 'pdus'), (self.link_layer, 'iq1'))
        self.msg_connect((self.iq_pdu_ch2, 'pdus'), (self.link_layer, 'iq2'))
        self.msg_connect((self.iq_pdu_ch3, 'pdus'), (self.link_layer, 'iq3'))
        self.msg_connect((self.link_layer, 'afc0'), (self.afc_ch0, 'cmd'))
        self.msg_connect((self.link_layer, 'afc1'), (self.afc_ch1, 'cmd'))
        self.msg_connect((self.link_layer, 'afc2'), (self.afc_ch2, 'cmd'))
        self.msg_connect((self.link_layer, 'afc3'), (self.afc_ch3, 'cmd'))
        self.msg_connect((self.link_layer, 'rx_attr'), (self.rx_attr, 'attr'))
        self.msg_connect((self.link_layer, 'tx_attr'), (self.tx_attr, 'attr'))
        self.msg_connect((self.link_layer, 'tx_en'), (self.tx_framer, 'ctl'))
        self.msg_connect((self.link_layer, 'tx_frame'), (self.tx_framer, 'frames'))
        self.msg_connect((self.link_layer, 'tx_ctl'), (self.tx_shift, 'cmd'))
        self.msg_connect((self.meas_pdu_ch0, 'pdus'), (self.link_layer, 'meas0'))
        self.msg_connect((self.meas_pdu_ch1, 'pdus'), (self.link_layer, 'meas1'))
        self.msg_connect((self.meas_pdu_ch2, 'pdus'), (self.link_layer, 'meas2'))
        self.msg_connect((self.meas_pdu_ch3, 'pdus'), (self.link_layer, 'meas3'))
        self.msg_connect((self.qtgui_edit_box_msg_0, 'msg'), (self.link_layer, 'chat_in'))
        self.msg_connect((self.tx_framer, 'status'), (self.link_layer, 'txstat'))
        self.connect((self.adc_keep, 0), (self.adc_pkt, 0))
        self.connect((self.adc_pkt, 0), (self.adc_pdu, 0))
        self.connect((self.afc_ch0, 0), (self.agc_ch0, 0))
        self.connect((self.afc_ch0, 0), (self.iq_keep_ch0, 0))
        self.connect((self.afc_ch1, 0), (self.agc_ch1, 0))
        self.connect((self.afc_ch1, 0), (self.iq_keep_ch1, 0))
        self.connect((self.afc_ch2, 0), (self.agc_ch2, 0))
        self.connect((self.afc_ch2, 0), (self.iq_keep_ch2, 0))
        self.connect((self.afc_ch3, 0), (self.agc_ch3, 0))
        self.connect((self.afc_ch3, 0), (self.iq_keep_ch3, 0))
        self.connect((self.agc_ch0, 0), (self.rrc_ch0, 0))
        self.connect((self.agc_ch1, 0), (self.rrc_ch1, 0))
        self.connect((self.agc_ch2, 0), (self.rrc_ch2, 0))
        self.connect((self.agc_ch3, 0), (self.rrc_ch3, 0))
        self.connect((self.bits_to_vec_ch0, 0), (self.deframer_ch0, 0))
        self.connect((self.bits_to_vec_ch1, 0), (self.deframer_ch1, 0))
        self.connect((self.bits_to_vec_ch2, 0), (self.deframer_ch2, 0))
        self.connect((self.bits_to_vec_ch3, 0), (self.deframer_ch3, 0))
        self.connect((self.chan_filter_ch0, 0), (self.afc_ch0, 0))
        self.connect((self.chan_filter_ch1, 0), (self.afc_ch1, 0))
        self.connect((self.chan_filter_ch2, 0), (self.afc_ch2, 0))
        self.connect((self.chan_filter_ch3, 0), (self.afc_ch3, 0))
        self.connect((self.const_keep_ch0, 0), (self.const_sink_ch0, 0))
        self.connect((self.const_keep_ch1, 0), (self.const_sink_ch1, 0))
        self.connect((self.const_keep_ch2, 0), (self.const_sink_ch2, 0))
        self.connect((self.const_keep_ch3, 0), (self.const_sink_ch3, 0))
        self.connect((self.costas_ch0, 0), (self.const_keep_ch0, 0))
        self.connect((self.costas_ch0, 0), (self.decoder_ch0, 0))
        self.connect((self.costas_ch0, 0), (self.meas_keep_ch0, 0))
        self.connect((self.costas_ch1, 0), (self.const_keep_ch1, 0))
        self.connect((self.costas_ch1, 0), (self.decoder_ch1, 0))
        self.connect((self.costas_ch1, 0), (self.meas_keep_ch1, 0))
        self.connect((self.costas_ch2, 0), (self.const_keep_ch2, 0))
        self.connect((self.costas_ch2, 0), (self.decoder_ch2, 0))
        self.connect((self.costas_ch2, 0), (self.meas_keep_ch2, 0))
        self.connect((self.costas_ch3, 0), (self.const_keep_ch3, 0))
        self.connect((self.costas_ch3, 0), (self.decoder_ch3, 0))
        self.connect((self.costas_ch3, 0), (self.meas_keep_ch3, 0))
        self.connect((self.decoder_ch0, 0), (self.diff_decoder_ch0, 0))
        self.connect((self.decoder_ch1, 0), (self.diff_decoder_ch1, 0))
        self.connect((self.decoder_ch2, 0), (self.diff_decoder_ch2, 0))
        self.connect((self.decoder_ch3, 0), (self.diff_decoder_ch3, 0))
        self.connect((self.diff_decoder_ch0, 0), (self.bits_to_vec_ch0, 0))
        self.connect((self.diff_decoder_ch1, 0), (self.bits_to_vec_ch1, 0))
        self.connect((self.diff_decoder_ch2, 0), (self.bits_to_vec_ch2, 0))
        self.connect((self.diff_decoder_ch3, 0), (self.bits_to_vec_ch3, 0))
        self.connect((self.iio_pluto_source_0, 0), (self.adc_keep, 0))
        self.connect((self.iio_pluto_source_0, 0), (self.chan_filter_ch0, 0))
        self.connect((self.iio_pluto_source_0, 0), (self.chan_filter_ch1, 0))
        self.connect((self.iio_pluto_source_0, 0), (self.chan_filter_ch2, 0))
        self.connect((self.iio_pluto_source_0, 0), (self.chan_filter_ch3, 0))
        self.connect((self.iio_pluto_source_0, 0), (self.rx_spec_keep, 0))
        self.connect((self.iq_keep_ch0, 0), (self.iq_pkt_ch0, 0))
        self.connect((self.iq_keep_ch1, 0), (self.iq_pkt_ch1, 0))
        self.connect((self.iq_keep_ch2, 0), (self.iq_pkt_ch2, 0))
        self.connect((self.iq_keep_ch3, 0), (self.iq_pkt_ch3, 0))
        self.connect((self.iq_pkt_ch0, 0), (self.iq_pdu_ch0, 0))
        self.connect((self.iq_pkt_ch1, 0), (self.iq_pdu_ch1, 0))
        self.connect((self.iq_pkt_ch2, 0), (self.iq_pdu_ch2, 0))
        self.connect((self.iq_pkt_ch3, 0), (self.iq_pdu_ch3, 0))
        self.connect((self.meas_keep_ch0, 0), (self.meas_pkt_ch0, 0))
        self.connect((self.meas_keep_ch1, 0), (self.meas_pkt_ch1, 0))
        self.connect((self.meas_keep_ch2, 0), (self.meas_pkt_ch2, 0))
        self.connect((self.meas_keep_ch3, 0), (self.meas_pkt_ch3, 0))
        self.connect((self.meas_pkt_ch0, 0), (self.meas_pdu_ch0, 0))
        self.connect((self.meas_pkt_ch1, 0), (self.meas_pdu_ch1, 0))
        self.connect((self.meas_pkt_ch2, 0), (self.meas_pdu_ch2, 0))
        self.connect((self.meas_pkt_ch3, 0), (self.meas_pdu_ch3, 0))
        self.connect((self.rrc_ch0, 0), (self.symbol_sync_ch0, 0))
        self.connect((self.rrc_ch1, 0), (self.symbol_sync_ch1, 0))
        self.connect((self.rrc_ch2, 0), (self.symbol_sync_ch2, 0))
        self.connect((self.rrc_ch3, 0), (self.symbol_sync_ch3, 0))
        self.connect((self.rx_spec_keep, 0), (self.rx_spectrum, 0))
        self.connect((self.symbol_sync_ch0, 0), (self.costas_ch0, 0))
        self.connect((self.symbol_sync_ch1, 0), (self.costas_ch1, 0))
        self.connect((self.symbol_sync_ch2, 0), (self.costas_ch2, 0))
        self.connect((self.symbol_sync_ch3, 0), (self.costas_ch3, 0))
        self.connect((self.tx_framer, 0), (self.tx_interp_filter, 0))
        self.connect((self.tx_interp_filter, 0), (self.tx_shift, 0))
        self.connect((self.tx_interp_filter, 0), (self.tx_spec_keep, 0))
        self.connect((self.tx_shift, 0), (self.iio_pluto_sink_0, 0))
        self.connect((self.tx_spec_keep, 0), (self.tx_spectrum, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "bpsk_duplex_pluto")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_agc(self):
        return self.agc

    def set_agc(self, agc):
        self.agc = agc

    def get_arq_window(self):
        return self.arq_window

    def set_arq_window(self, arq_window):
        self.arq_window = arq_window

    def get_center_freq(self):
        return self.center_freq

    def set_center_freq(self, center_freq):
        self.center_freq = center_freq
        self.iio_pluto_sink_0.set_frequency((int)(self.center_freq))
        self.iio_pluto_source_0.set_frequency((int)(self.center_freq))
        self.rx_spectrum.set_frequency_range(self.center_freq, self.radio_rate)
        self.tx_spectrum.set_frequency_range(self.center_freq, self.radio_rate)

    def get_ch_first(self):
        return self.ch_first

    def set_ch_first(self, ch_first):
        self.ch_first = ch_first
        self.chan_filter_ch0.set_center_freq((self.ch_first+0*self.ch_spacing))
        self.chan_filter_ch1.set_center_freq((self.ch_first+1*self.ch_spacing))
        self.chan_filter_ch2.set_center_freq((self.ch_first+2*self.ch_spacing))
        self.chan_filter_ch3.set_center_freq((self.ch_first+3*self.ch_spacing))

    def get_ch_spacing(self):
        return self.ch_spacing

    def set_ch_spacing(self, ch_spacing):
        self.ch_spacing = ch_spacing
        self.set_ch_taps(firdes.low_pass_2(1.0, self.radio_rate, 0.5*self.ch_spacing, 0.4167*self.ch_spacing, 65))
        self.chan_filter_ch0.set_center_freq((self.ch_first+0*self.ch_spacing))
        self.chan_filter_ch1.set_center_freq((self.ch_first+1*self.ch_spacing))
        self.chan_filter_ch2.set_center_freq((self.ch_first+2*self.ch_spacing))
        self.chan_filter_ch3.set_center_freq((self.ch_first+3*self.ch_spacing))

    def get_channel(self):
        return self.channel

    def set_channel(self, channel):
        self.channel = channel

    def get_http_port(self):
        return self.http_port

    def set_http_port(self, http_port):
        self.http_port = http_port
        self.link_layer.http_port = self.http_port

    def get_keys_dir(self):
        return self.keys_dir

    def set_keys_dir(self, keys_dir):
        self.keys_dir = keys_dir

    def get_my_addr(self):
        return self.my_addr

    def set_my_addr(self, my_addr):
        self.my_addr = my_addr

    def get_nickname(self):
        return self.nickname

    def set_nickname(self, nickname):
        self.nickname = nickname

    def get_peer_addr(self):
        return self.peer_addr

    def set_peer_addr(self, peer_addr):
        self.peer_addr = peer_addr

    def get_plan_file(self):
        return self.plan_file

    def set_plan_file(self, plan_file):
        self.plan_file = plan_file

    def get_postamble_size(self):
        return self.postamble_size

    def set_postamble_size(self, postamble_size):
        self.postamble_size = postamble_size

    def get_preamble_size(self):
        return self.preamble_size

    def set_preamble_size(self, preamble_size):
        self.preamble_size = preamble_size

    def get_radio_rate(self):
        return self.radio_rate

    def set_radio_rate(self, radio_rate):
        self.radio_rate = radio_rate
        self.set_ch_taps(firdes.low_pass_2(1.0, self.radio_rate, 0.5*self.ch_spacing, 0.4167*self.ch_spacing, 65))
        self.set_sym_rate(self.radio_rate/self.tx_interp)
        self.iio_pluto_sink_0.set_bandwidth(((int)(0.9*self.radio_rate)))
        self.iio_pluto_sink_0.set_samplerate((int)(self.radio_rate))
        self.iio_pluto_source_0.set_samplerate((int)(self.radio_rate))
        self.link_layer.radio_rate = self.radio_rate
        self.rx_spectrum.set_frequency_range(self.center_freq, self.radio_rate)
        self.tx_spectrum.set_frequency_range(self.center_freq, self.radio_rate)

    def get_rx_buffer(self):
        return self.rx_buffer

    def set_rx_buffer(self, rx_buffer):
        self.rx_buffer = rx_buffer

    def get_rx_gain(self):
        return self.rx_gain

    def set_rx_gain(self, rx_gain):
        self.rx_gain = rx_gain
        self.iio_pluto_source_0.set_gain(0, self.rx_gain)

    def get_security(self):
        return self.security

    def set_security(self, security):
        self.security = security

    def get_telemetry(self):
        return self.telemetry

    def set_telemetry(self, telemetry):
        self.telemetry = telemetry

    def get_tx_atten(self):
        return self.tx_atten

    def set_tx_atten(self, tx_atten):
        self.tx_atten = tx_atten
        self.iio_pluto_sink_0.set_attenuation(0,self.tx_atten)

    def get_tx_buffer(self):
        return self.tx_buffer

    def set_tx_buffer(self, tx_buffer):
        self.tx_buffer = tx_buffer

    def get_uri(self):
        return self.uri

    def set_uri(self, uri):
        self.uri = uri

    def get_tx_interp(self):
        return self.tx_interp

    def set_tx_interp(self, tx_interp):
        self.tx_interp = tx_interp
        self.set_decim(self.tx_interp//self.sps)
        self.set_sym_rate(self.radio_rate/self.tx_interp)
        self.set_tx_taps([self.tx_scale*t for t in firdes.root_raised_cosine(self.tx_interp, self.tx_interp, 1.0, 0.35, 16*self.tx_interp+1)])

    def get_sym_rate(self):
        return self.sym_rate

    def set_sym_rate(self, sym_rate):
        self.sym_rate = sym_rate
        self.set_ch_rate(self.sps*self.sym_rate)
        self.set_rrc_taps(firdes.root_raised_cosine(1.0, self.ch_rate, self.sym_rate, 0.35, 11*self.sps))

    def get_sps(self):
        return self.sps

    def set_sps(self, sps):
        self.sps = sps
        self.set_ch_rate(self.sps*self.sym_rate)
        self.set_decim(self.tx_interp//self.sps)
        self.set_rrc_taps(firdes.root_raised_cosine(1.0, self.ch_rate, self.sym_rate, 0.35, 11*self.sps))
        self.symbol_sync_ch0.set_sps(self.sps)
        self.symbol_sync_ch1.set_sps(self.sps)
        self.symbol_sync_ch2.set_sps(self.sps)
        self.symbol_sync_ch3.set_sps(self.sps)

    def get_tx_scale(self):
        return self.tx_scale

    def set_tx_scale(self, tx_scale):
        self.tx_scale = tx_scale
        self.set_tx_taps([self.tx_scale*t for t in firdes.root_raised_cosine(self.tx_interp, self.tx_interp, 1.0, 0.35, 16*self.tx_interp+1)])

    def get_ch_rate(self):
        return self.ch_rate

    def set_ch_rate(self, ch_rate):
        self.ch_rate = ch_rate
        self.set_rrc_taps(firdes.root_raised_cosine(1.0, self.ch_rate, self.sym_rate, 0.35, 11*self.sps))

    def get_tx_taps(self):
        return self.tx_taps

    def set_tx_taps(self, tx_taps):
        self.tx_taps = tx_taps
        self.tx_interp_filter.set_taps(self.tx_taps)

    def get_rrc_taps(self):
        return self.rrc_taps

    def set_rrc_taps(self, rrc_taps):
        self.rrc_taps = rrc_taps
        self.rrc_ch0.set_taps(self.rrc_taps)
        self.rrc_ch1.set_taps(self.rrc_taps)
        self.rrc_ch2.set_taps(self.rrc_taps)
        self.rrc_ch3.set_taps(self.rrc_taps)

    def get_queue_ahead(self):
        return self.queue_ahead

    def set_queue_ahead(self, queue_ahead):
        self.queue_ahead = queue_ahead

    def get_max_retries(self):
        return self.max_retries

    def set_max_retries(self, max_retries):
        self.max_retries = max_retries

    def get_frag_size(self):
        return self.frag_size

    def set_frag_size(self, frag_size):
        self.frag_size = frag_size

    def get_decim(self):
        return self.decim

    def set_decim(self, decim):
        self.decim = decim

    def get_constel(self):
        return self.constel

    def set_constel(self, constel):
        self.constel = constel
        self.decoder_ch0.set_constellation(self.constel)
        self.decoder_ch1.set_constellation(self.constel)
        self.decoder_ch2.set_constellation(self.constel)
        self.decoder_ch3.set_constellation(self.constel)

    def get_ch_taps(self):
        return self.ch_taps

    def set_ch_taps(self, ch_taps):
        self.ch_taps = ch_taps
        self.chan_filter_ch0.set_taps(self.ch_taps)
        self.chan_filter_ch1.set_taps(self.ch_taps)
        self.chan_filter_ch2.set_taps(self.ch_taps)
        self.chan_filter_ch3.set_taps(self.ch_taps)

    def get_ack_timeout(self):
        return self.ack_timeout

    def set_ack_timeout(self, ack_timeout):
        self.ack_timeout = ack_timeout



def argument_parser():
    description = 'BPSK network node: 4-channel FDMA mesh (6 MS/s, one-sided plan), self-assigned channels and addresses, closed-loop gain staging, Selective Repeat ARQ per peer, end-to-end encryption per pair, browser chat (r6.1-mesh)'
    parser = ArgumentParser(description=description)
    parser.add_argument(
        "--agc", dest="agc", type=intx, default=1,
        help="Set Gain control (1/0) [default=%(default)r]")
    parser.add_argument(
        "--arq-window", dest="arq_window", type=intx, default=32,
        help="Set ARQ window (frames) [default=%(default)r]")
    parser.add_argument(
        "--center-freq", dest="center_freq", type=eng_float, default=eng_notation.num_to_str(float(910.2e6)),
        help="Set Centre frequency (Hz) [default=%(default)r]")
    parser.add_argument(
        "--ch-first", dest="ch_first", type=eng_float, default=eng_notation.num_to_str(float(450e3)),
        help="Set Channel 0 offset (Hz) [default=%(default)r]")
    parser.add_argument(
        "--ch-spacing", dest="ch_spacing", type=eng_float, default=eng_notation.num_to_str(float(600e3)),
        help="Set Channel spacing (Hz) [default=%(default)r]")
    parser.add_argument(
        "--channel", dest="channel", type=intx, default=(-1),
        help="Set TX channel [default=%(default)r]")
    parser.add_argument(
        "--http-port", dest="http_port", type=intx, default=8088,
        help="Set Chat UI port [default=%(default)r]")
    parser.add_argument(
        "--keys-dir", dest="keys_dir", type=str, default='keys',
        help="Set Key directory [default=%(default)r]")
    parser.add_argument(
        "--my-addr", dest="my_addr", type=intx, default=0,
        help="Set My address [default=%(default)r]")
    parser.add_argument(
        "--nickname", dest="nickname", type=str, default='',
        help="Set Chat nickname [default=%(default)r]")
    parser.add_argument(
        "--peer-addr", dest="peer_addr", type=intx, default=0,
        help="Set Peer address [default=%(default)r]")
    parser.add_argument(
        "--plan-file", dest="plan_file", type=str, default='',
        help="Set Static channel plan file [default=%(default)r]")
    parser.add_argument(
        "--postamble-size", dest="postamble_size", type=intx, default=64,
        help="Set Postamble (bytes) [default=%(default)r]")
    parser.add_argument(
        "--preamble-size", dest="preamble_size", type=intx, default=64,
        help="Set Preamble (bytes) [default=%(default)r]")
    parser.add_argument(
        "--radio-rate", dest="radio_rate", type=eng_float, default=eng_notation.num_to_str(float(6e6)),
        help="Set Radio sample rate [default=%(default)r]")
    parser.add_argument(
        "--rx-buffer", dest="rx_buffer", type=intx, default=65536,
        help="Set Pluto RX buffer (samples) [default=%(default)r]")
    parser.add_argument(
        "--rx-gain", dest="rx_gain", type=intx, default=30,
        help="Set RX gain at start (dB) [default=%(default)r]")
    parser.add_argument(
        "--security", dest="security", type=intx, default=1,
        help="Set Encryption (1/0) [default=%(default)r]")
    parser.add_argument(
        "--telemetry", dest="telemetry", type=intx, default=1,
        help="Set Telemetry CSV (1/0) [default=%(default)r]")
    parser.add_argument(
        "--tx-atten", dest="tx_atten", type=intx, default=50,
        help="Set TX attenuation at start (dB) [default=%(default)r]")
    parser.add_argument(
        "--tx-buffer", dest="tx_buffer", type=intx, default=49152,
        help="Set Pluto TX buffer (samples) [default=%(default)r]")
    parser.add_argument(
        "--uri", dest="uri", type=str, default='ip:192.168.1.10',
        help="Set Pluto URI [default=%(default)r]")
    return parser


def main(top_block_cls=bpsk_duplex_pluto, options=None):
    if options is None:
        options = argument_parser().parse_args()

    qapp = Qt.QApplication(sys.argv)

    tb = top_block_cls(agc=options.agc, arq_window=options.arq_window, center_freq=options.center_freq, ch_first=options.ch_first, ch_spacing=options.ch_spacing, channel=options.channel, http_port=options.http_port, keys_dir=options.keys_dir, my_addr=options.my_addr, nickname=options.nickname, peer_addr=options.peer_addr, plan_file=options.plan_file, postamble_size=options.postamble_size, preamble_size=options.preamble_size, radio_rate=options.radio_rate, rx_buffer=options.rx_buffer, rx_gain=options.rx_gain, security=options.security, telemetry=options.telemetry, tx_atten=options.tx_atten, tx_buffer=options.tx_buffer, uri=options.uri)

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
