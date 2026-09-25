#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: BPSK CSMA Chat Node (shared channel, TDD)
# Author: Rivija Pesara (framing from Barry Duggan BPSK example)
# Description: Four-node shared-channel BPSK link: CSMA/CA medium access, per-peer ARQ, browser chat (r6b)
# GNU Radio version: 3.10.12.0

from PyQt5 import Qt
from gnuradio import qtgui
from PyQt5 import QtCore
from gnuradio import blocks
import pmt
from gnuradio import digital
from gnuradio import filter
from gnuradio.filter import firdes
from gnuradio import gr, pdu
from gnuradio import iio
import bpsk_csma_link_layer as link_layer  # embedded python block
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




class bpsk_csma_pluto(gr.top_block, Qt.QWidget):

    def __init__(self, beacon_interval=2.0, busy_threshold_db=-55.0, cw_max=128, cw_min=8, http_port=8088, mac_slot=0.003, my_addr=1, nickname='Node A', peer_addr=2, peer_timeout=8.0, peers='2,3,4', rf_freq=915.0e6, uri='ip:192.168.1.10'):
        gr.top_block.__init__(self, "BPSK CSMA Chat Node", catch_exceptions=False)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("BPSK CSMA Chat Node (shared channel)")
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

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "bpsk_csma_pluto")

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
        self.beacon_interval = beacon_interval
        self.busy_threshold_db = busy_threshold_db
        self.cw_max = cw_max
        self.cw_min = cw_min
        self.http_port = http_port
        self.mac_slot = mac_slot
        self.my_addr = my_addr
        self.nickname = nickname
        self.peer_addr = peer_addr
        self.peer_timeout = peer_timeout
        self.peers = peers
        self.rf_freq = rf_freq
        self.uri = uri
        # One shared carrier: this is TDD, not FDD. Both the sink and the
        # source sit on rf_freq, which is what lets a node hear the others.
        self.tx_freq = tx_freq = rf_freq
        self.rx_freq = rx_freq = rf_freq

        ##################################################
        # Variables
        ##################################################
        self.tx_atten = tx_atten = 20
        self.sps = sps = 4
        self.samp_rate = samp_rate = 1000000
        self.rx_gain = rx_gain = 45
        self.queue_ahead = queue_ahead = 0   # no idle filler on a shared channel
        self.preamble_size = preamble_size = 384
        self.postamble_size = postamble_size = 64
        self.max_retries = max_retries = 5
        self.hdr = hdr = digital.header_format_default(digital.packet_utils.default_access_code, 0)
        self.frag_size = frag_size = 256
        self.constel = constel = digital.constellation_bpsk().base()
        self.constel.set_npwr(1.0)
        self.ack_timeout = ack_timeout = 0.5

        ##################################################
        # Blocks
        ##################################################

        self._tx_atten_range = qtgui.Range(0, 89, 1, 20, 200)
        self._tx_atten_win = qtgui.RangeWidget(self._tx_atten_range, self.set_tx_atten, "Tx Attenuation (dB)", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_layout.addWidget(self._tx_atten_win)
        self._rx_gain_range = qtgui.Range(0, 73, 1, 45, 200)
        self._rx_gain_win = qtgui.RangeWidget(self._rx_gain_range, self.set_rx_gain, "Rx Gain (dB)", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_layout.addWidget(self._rx_gain_win)
        self.qtgui_time_sink_tx = qtgui.time_sink_c(
            1024, #size
            samp_rate, #samp_rate
            "Transmit waveform", #name
            1, #number of inputs
            None # parent
        )
        self.qtgui_time_sink_tx.set_update_time(0.10)
        self.qtgui_time_sink_tx.set_y_axis(-1, 1)

        self.qtgui_time_sink_tx.set_y_label('Amplitude', "")

        self.qtgui_time_sink_tx.enable_tags(True)
        self.qtgui_time_sink_tx.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.qtgui_time_sink_tx.enable_autoscale(False)
        self.qtgui_time_sink_tx.enable_grid(True)
        self.qtgui_time_sink_tx.enable_axis_labels(True)
        self.qtgui_time_sink_tx.enable_control_panel(False)
        self.qtgui_time_sink_tx.enable_stem_plot(False)


        labels = ['Real', 'Imag', 'Signal 3', 'Signal 4', 'Signal 5',
            'Signal 6', 'Signal 7', 'Signal 8', 'Signal 9', 'Signal 10']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ['blue', 'red', 'green', 'black', 'cyan',
            'magenta', 'yellow', 'dark red', 'dark green', 'dark blue']
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]
        styles = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        markers = [-1, -1, -1, -1, -1,
            -1, -1, -1, -1, -1]


        for i in range(2):
            if len(labels[i]) == 0:
                if (i % 2 == 0):
                    self.qtgui_time_sink_tx.set_line_label(i, "Re{{Data {0}}}".format(i/2))
                else:
                    self.qtgui_time_sink_tx.set_line_label(i, "Im{{Data {0}}}".format(i/2))
            else:
                self.qtgui_time_sink_tx.set_line_label(i, labels[i])
            self.qtgui_time_sink_tx.set_line_width(i, widths[i])
            self.qtgui_time_sink_tx.set_line_color(i, colors[i])
            self.qtgui_time_sink_tx.set_line_style(i, styles[i])
            self.qtgui_time_sink_tx.set_line_marker(i, markers[i])
            self.qtgui_time_sink_tx.set_line_alpha(i, alphas[i])

        self._qtgui_time_sink_tx_win = sip.wrapinstance(self.qtgui_time_sink_tx.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._qtgui_time_sink_tx_win, 5, 0, 2, 1)
        for r in range(5, 7):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 1):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.qtgui_time_sink_rx = qtgui.time_sink_c(
            256, #size
            samp_rate/sps, #samp_rate
            "Recovered Symbols", #name
            1, #number of inputs
            None # parent
        )
        self.qtgui_time_sink_rx.set_update_time(0.1)
        self.qtgui_time_sink_rx.set_y_axis(-1.0, 1.0)

        self.qtgui_time_sink_rx.set_y_label('Amplitude', "")

        self.qtgui_time_sink_rx.enable_tags(True)
        self.qtgui_time_sink_rx.set_trigger_mode(qtgui.TRIG_MODE_AUTO, qtgui.TRIG_SLOPE_POS, 0.01, 0, 0, "")
        self.qtgui_time_sink_rx.enable_autoscale(False)
        self.qtgui_time_sink_rx.enable_grid(True)
        self.qtgui_time_sink_rx.enable_axis_labels(True)
        self.qtgui_time_sink_rx.enable_control_panel(False)
        self.qtgui_time_sink_rx.enable_stem_plot(False)


        labels = ['Real', 'Imag', 'Signal 3', 'Signal 4', 'Signal 5',
            'Signal 6', 'Signal 7', 'Signal 8', 'Signal 9', 'Signal 10']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ['blue', 'red', 'green', 'black', 'cyan',
            'magenta', 'yellow', 'dark red', 'dark green', 'dark blue']
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]
        styles = [1, 3, 1, 1, 1,
            1, 1, 1, 1, 1]
        markers = [-1, -1, -1, -1, -1,
            -1, -1, -1, -1, -1]


        for i in range(2):
            if len(labels[i]) == 0:
                if (i % 2 == 0):
                    self.qtgui_time_sink_rx.set_line_label(i, "Re{{Data {0}}}".format(i/2))
                else:
                    self.qtgui_time_sink_rx.set_line_label(i, "Im{{Data {0}}}".format(i/2))
            else:
                self.qtgui_time_sink_rx.set_line_label(i, labels[i])
            self.qtgui_time_sink_rx.set_line_width(i, widths[i])
            self.qtgui_time_sink_rx.set_line_color(i, colors[i])
            self.qtgui_time_sink_rx.set_line_style(i, styles[i])
            self.qtgui_time_sink_rx.set_line_marker(i, markers[i])
            self.qtgui_time_sink_rx.set_line_alpha(i, alphas[i])

        self._qtgui_time_sink_rx_win = sip.wrapinstance(self.qtgui_time_sink_rx.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._qtgui_time_sink_rx_win, 3, 0, 2, 2)
        for r in range(3, 5):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 2):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.qtgui_freq_sink_x_0 = qtgui.freq_sink_c(
            1024, #size
            window.WIN_BLACKMAN_hARRIS, #wintype
            rx_freq, #fc
            samp_rate, #bw
            "RX Spectrum", #name
            1,
            None # parent
        )
        self.qtgui_freq_sink_x_0.set_update_time(0.10)
        self.qtgui_freq_sink_x_0.set_y_axis((-120), 0)
        self.qtgui_freq_sink_x_0.set_y_label('Relative Gain', 'dB')
        self.qtgui_freq_sink_x_0.set_trigger_mode(qtgui.TRIG_MODE_FREE, 0.0, 0, "")
        self.qtgui_freq_sink_x_0.enable_autoscale(False)
        self.qtgui_freq_sink_x_0.enable_grid(True)
        self.qtgui_freq_sink_x_0.set_fft_average(0.2)
        self.qtgui_freq_sink_x_0.enable_axis_labels(True)
        self.qtgui_freq_sink_x_0.enable_control_panel(False)
        self.qtgui_freq_sink_x_0.set_fft_window_normalized(False)



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
        self.top_grid_layout.addWidget(self._qtgui_freq_sink_x_0_win, 1, 0, 2, 1)
        for r in range(1, 3):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 1):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.qtgui_edit_box_msg_0 = qtgui.edit_box_msg(qtgui.STRING, "", "Console  (the chat lives in the browser; /stats, /help here)", False, True, "", None)
        self._qtgui_edit_box_msg_0_win = sip.wrapinstance(self.qtgui_edit_box_msg_0.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._qtgui_edit_box_msg_0_win, 0, 0, 1, 2)
        for r in range(0, 1):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 2):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.qtgui_const_sink_x_0 = qtgui.const_sink_c(
            1024, #size
            "Constellation", #name
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
        self.top_grid_layout.addWidget(self._qtgui_const_sink_x_0_win, 1, 1, 2, 1)
        for r in range(1, 3):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(1, 2):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.pdu_tagged_stream_to_pdu_0 = pdu.tagged_stream_to_pdu(gr.types.byte_t, 'packet_len')
        self.pdu_pdu_to_tagged_stream_0 = pdu.pdu_to_tagged_stream(gr.types.byte_t, 'packet_len')
        self.link_layer = link_layer.blk(my_addr=my_addr, peer_addr=peer_addr, ack_timeout=ack_timeout, max_retries=max_retries, frag_size=frag_size, queue_ahead=queue_ahead, sym_rate=samp_rate/sps, overhead=preamble_size+postamble_size+12, rx_dir=".", nickname=nickname, http_port=http_port, open_ui=True, tx_freq=tx_freq, rx_freq=rx_freq, peers=peers, mac_slot=mac_slot, cw_min=cw_min, cw_max=cw_max, busy_threshold_db=busy_threshold_db, beacon_interval=beacon_interval, peer_timeout=peer_timeout)
        self.iio_pluto_source_0 = iio.fmcomms2_source_fc32(uri if uri else iio.get_pluto_uri(), [True, True], 2048)
        self.iio_pluto_source_0.set_len_tag_key('')
        self.iio_pluto_source_0.set_frequency((int)(rx_freq))
        self.iio_pluto_source_0.set_samplerate(samp_rate)
        self.iio_pluto_source_0.set_gain_mode(0, 'manual')
        self.iio_pluto_source_0.set_gain(0, rx_gain)
        self.iio_pluto_source_0.set_quadrature(True)
        self.iio_pluto_source_0.set_rfdc(True)
        self.iio_pluto_source_0.set_bbdc(True)
        self.iio_pluto_source_0.set_filter_params('Auto', '', 0, 0)
        self.iio_pluto_sink_0 = iio.fmcomms2_sink_fc32(uri if uri else iio.get_pluto_uri(), [True, True], 8192, False)
        self.iio_pluto_sink_0.set_len_tag_key('packet_len')   # burst mode
        self.iio_pluto_sink_0.set_bandwidth(1000000)
        self.iio_pluto_sink_0.set_frequency((int)(tx_freq))
        self.iio_pluto_sink_0.set_samplerate(samp_rate)
        self.iio_pluto_sink_0.set_attenuation(0, tx_atten)
        self.iio_pluto_sink_0.set_filter_params('Auto', '', 0, 0)
        self.filter_fft_rrc_filter_0 = filter.fft_filter_ccc(1, firdes.root_raised_cosine(1, samp_rate, (samp_rate/sps), 0.35, (11*sps)), 1)
        self.digital_symbol_sync_xx_0 = digital.symbol_sync_cc(
            digital.TED_SIGNAL_TIMES_SLOPE_ML,
            sps,
            0.045,
            1.0,
            0.1,
            1.5,
            1,
            constel.base(),
            digital.IR_MMSE_8TAP,
            32,
            [])
        self.digital_protocol_formatter_bb_0 = digital.protocol_formatter_bb(hdr, 'packet_len')
        self.digital_fll_band_edge_cc_0 = digital.fll_band_edge_cc(sps, 0.35, 44, 0.03, False)
        self.digital_diff_decoder_bb_0 = digital.diff_decoder_bb(len(constel.points()), digital.DIFF_DIFFERENTIAL)
        self.digital_costas_loop_cc_0 = digital.costas_loop_cc((3.14/100), len(constel.points()), False)
        self.digital_correlate_access_code_xx_ts_0 = digital.correlate_access_code_bb_ts(digital.packet_utils.default_access_code,
          2, 'packet_len')
        self.digital_constellation_modulator_0 = digital.generic_mod(
            constellation=constel,
            differential=True,
            samples_per_symbol=sps,
            pre_diff_code=True,
            excess_bw=0.35,
            verbose=False,
            log=False,
            truncate=False)
        self.digital_constellation_decoder_cb_0 = digital.constellation_decoder_cb(constel)
        self.blocks_vector_source_x_0_0 = blocks.vector_source_b([0xc0, 0xaf], True, 1, [])
        self.blocks_vector_source_x_0 = blocks.vector_source_b([0xc0, 0xaf], True, 1, [])
        self.blocks_tagged_stream_mux_0 = blocks.tagged_stream_mux(gr.sizeof_char*1, 'packet_len', 0)
        self.blocks_stream_to_tagged_stream_pre = blocks.stream_to_tagged_stream(gr.sizeof_char, 1, preamble_size, "packet_len")
        self.blocks_stream_to_tagged_stream_post = blocks.stream_to_tagged_stream(gr.sizeof_char, 1, postamble_size, "packet_len")
        self.blocks_repack_bits_bb_0 = blocks.repack_bits_bb(1, 8, 'packet_len', True, gr.GR_MSB_FIRST)
        self.blocks_multiply_const_vxx_0 = blocks.multiply_const_cc(0.5)
        self.blocks_message_strobe_0 = blocks.message_strobe(pmt.intern("tick"), 10)
        # Port 0 of the chat node is one sample per symbol, so the raw
        # stream is decimated to match before it reaches the carrier-sense
        # port: a sync block consumes equally from all of its inputs.
        self.blocks_keep_one_in_n_0 = blocks.keep_one_in_n(gr.sizeof_gr_complex*1, sps)


        ##################################################
        # Connections
        ##################################################
        self.msg_connect((self.blocks_message_strobe_0, 'strobe'), (self.link_layer, 'tick'))
        self.msg_connect((self.link_layer, 'tx_frame'), (self.pdu_pdu_to_tagged_stream_0, 'pdus'))
        self.msg_connect((self.pdu_tagged_stream_to_pdu_0, 'pdus'), (self.link_layer, 'rx_frame'))
        self.msg_connect((self.qtgui_edit_box_msg_0, 'msg'), (self.link_layer, 'chat_in'))
        self.connect((self.blocks_multiply_const_vxx_0, 0), (self.iio_pluto_sink_0, 0))
        self.connect((self.blocks_multiply_const_vxx_0, 0), (self.qtgui_time_sink_tx, 0))
        self.connect((self.blocks_repack_bits_bb_0, 0), (self.pdu_tagged_stream_to_pdu_0, 0))
        self.connect((self.blocks_stream_to_tagged_stream_post, 0), (self.blocks_tagged_stream_mux_0, 3))
        self.connect((self.blocks_stream_to_tagged_stream_pre, 0), (self.blocks_tagged_stream_mux_0, 0))
        self.connect((self.blocks_tagged_stream_mux_0, 0), (self.digital_constellation_modulator_0, 0))
        self.connect((self.blocks_vector_source_x_0, 0), (self.blocks_stream_to_tagged_stream_pre, 0))
        self.connect((self.blocks_vector_source_x_0_0, 0), (self.blocks_stream_to_tagged_stream_post, 0))
        self.connect((self.digital_constellation_decoder_cb_0, 0), (self.digital_diff_decoder_bb_0, 0))
        self.connect((self.digital_constellation_modulator_0, 0), (self.blocks_multiply_const_vxx_0, 0))
        self.connect((self.digital_correlate_access_code_xx_ts_0, 0), (self.blocks_repack_bits_bb_0, 0))
        self.connect((self.digital_costas_loop_cc_0, 0), (self.digital_constellation_decoder_cb_0, 0))
        self.connect((self.digital_costas_loop_cc_0, 0), (self.link_layer, 0))
        self.connect((self.digital_costas_loop_cc_0, 0), (self.qtgui_const_sink_x_0, 0))
        self.connect((self.digital_costas_loop_cc_0, 0), (self.qtgui_time_sink_rx, 0))
        self.connect((self.digital_diff_decoder_bb_0, 0), (self.digital_correlate_access_code_xx_ts_0, 0))
        self.connect((self.digital_fll_band_edge_cc_0, 0), (self.filter_fft_rrc_filter_0, 0))
        self.connect((self.digital_protocol_formatter_bb_0, 0), (self.blocks_tagged_stream_mux_0, 1))
        self.connect((self.digital_symbol_sync_xx_0, 0), (self.digital_costas_loop_cc_0, 0))
        self.connect((self.filter_fft_rrc_filter_0, 0), (self.digital_symbol_sync_xx_0, 0))
        self.connect((self.iio_pluto_source_0, 0), (self.blocks_keep_one_in_n_0, 0))
        self.connect((self.blocks_keep_one_in_n_0, 0), (self.link_layer, 1))
        self.connect((self.iio_pluto_source_0, 0), (self.digital_fll_band_edge_cc_0, 0))
        self.connect((self.iio_pluto_source_0, 0), (self.qtgui_freq_sink_x_0, 0))
        self.connect((self.pdu_pdu_to_tagged_stream_0, 0), (self.blocks_tagged_stream_mux_0, 2))
        self.connect((self.pdu_pdu_to_tagged_stream_0, 0), (self.digital_protocol_formatter_bb_0, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "bpsk_csma_pluto")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_http_port(self):
        return self.http_port

    def set_http_port(self, http_port):
        self.http_port = http_port

    def get_nickname(self):
        return self.nickname

    def set_nickname(self, nickname):
        self.nickname = nickname

    def get_my_addr(self):
        return self.my_addr

    def set_my_addr(self, my_addr):
        self.my_addr = my_addr

    def get_peers(self):
        return self.peers

    def set_peers(self, peers):
        self.peers = peers

    def get_peer_addr(self):
        return self.peer_addr

    def set_peer_addr(self, peer_addr):
        self.peer_addr = peer_addr
        self.peers = peers

    def get_rx_freq(self):
        return self.rx_freq

    def set_rx_freq(self, rx_freq):
        self.rx_freq = rx_freq
        self.iio_pluto_source_0.set_frequency((int)(self.rx_freq))
        self.qtgui_freq_sink_x_0.set_frequency_range(self.rx_freq, self.samp_rate)

    def get_tx_freq(self):
        return self.tx_freq

    def set_tx_freq(self, tx_freq):
        self.tx_freq = tx_freq
        self.iio_pluto_sink_0.set_frequency((int)(self.tx_freq))

    def get_uri(self):
        return self.uri

    def set_uri(self, uri):
        self.uri = uri

    def get_tx_atten(self):
        return self.tx_atten

    def set_tx_atten(self, tx_atten):
        self.tx_atten = tx_atten
        self.iio_pluto_sink_0.set_attenuation(0,self.tx_atten)

    def get_sps(self):
        return self.sps

    def set_sps(self, sps):
        self.sps = sps
        self.digital_symbol_sync_xx_0.set_sps(self.sps)
        self.filter_fft_rrc_filter_0.set_taps(firdes.root_raised_cosine(1, self.samp_rate, (self.samp_rate/self.sps), 0.35, (11*self.sps)))
        self.qtgui_time_sink_rx.set_samp_rate(self.samp_rate/self.sps)

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.filter_fft_rrc_filter_0.set_taps(firdes.root_raised_cosine(1, self.samp_rate, (self.samp_rate/self.sps), 0.35, (11*self.sps)))
        self.iio_pluto_sink_0.set_samplerate(self.samp_rate)
        self.iio_pluto_source_0.set_samplerate(self.samp_rate)
        self.qtgui_freq_sink_x_0.set_frequency_range(self.rx_freq, self.samp_rate)
        self.qtgui_time_sink_rx.set_samp_rate(self.samp_rate/self.sps)
        self.qtgui_time_sink_tx.set_samp_rate(self.samp_rate)

    def get_rx_gain(self):
        return self.rx_gain

    def set_rx_gain(self, rx_gain):
        self.rx_gain = rx_gain
        self.iio_pluto_source_0.set_gain(0, self.rx_gain)

    def get_queue_ahead(self):
        return self.queue_ahead

    def set_queue_ahead(self, queue_ahead):
        self.queue_ahead = queue_ahead

    def get_preamble_size(self):
        return self.preamble_size

    def set_preamble_size(self, preamble_size):
        self.preamble_size = preamble_size
        self.blocks_stream_to_tagged_stream_pre.set_packet_len(self.preamble_size)
        self.blocks_stream_to_tagged_stream_pre.set_packet_len_pmt(self.preamble_size)

    def get_postamble_size(self):
        return self.postamble_size

    def set_postamble_size(self, postamble_size):
        self.postamble_size = postamble_size
        self.blocks_stream_to_tagged_stream_post.set_packet_len(self.postamble_size)
        self.blocks_stream_to_tagged_stream_post.set_packet_len_pmt(self.postamble_size)

    def get_max_retries(self):
        return self.max_retries

    def set_max_retries(self, max_retries):
        self.max_retries = max_retries

    def get_hdr(self):
        return self.hdr

    def set_hdr(self, hdr):
        self.hdr = hdr
        self.digital_protocol_formatter_bb_0.set_header_format(self.hdr)

    def get_frag_size(self):
        return self.frag_size

    def set_frag_size(self, frag_size):
        self.frag_size = frag_size

    def get_constel(self):
        return self.constel

    def set_constel(self, constel):
        self.constel = constel
        self.digital_constellation_decoder_cb_0.set_constellation(self.constel)

    def get_ack_timeout(self):
        return self.ack_timeout

    def set_ack_timeout(self, ack_timeout):
        self.ack_timeout = ack_timeout



def argument_parser():
    description = 'Full-duplex FDD BPSK link with addressing, stop-and-wait ARQ and a browser chat application layer'
    parser = ArgumentParser(description=description)
    parser.add_argument(
        "--http-port", dest="http_port", type=intx, default=8088,
        help="Set Chat UI port [default=%(default)r]")
    parser.add_argument(
        "--nickname", dest="nickname", type=str, default='Node A',
        help="Set Chat nickname [default=%(default)r]")
    parser.add_argument(
        "--my-addr", dest="my_addr", type=intx, default=1,
        help="Set My address [default=%(default)r]")
    parser.add_argument(
        "--peer-addr", dest="peer_addr", type=intx, default=2,
        help="Set Peer address [default=%(default)r]")
    parser.add_argument(
        "--rf-freq", dest="rf_freq", type=eng_float, default=eng_notation.num_to_str(float(915.0e6)),
        help="Shared carrier, Hz - every node uses the same one [default=%(default)r]")
    parser.add_argument(
        "--mac-slot", dest="mac_slot", type=eng_float, default=eng_notation.num_to_str(float(0.003)),
        help="CSMA slot time, s [default=%(default)r]")
    parser.add_argument(
        "--cw-min", dest="cw_min", type=intx, default=8,
        help="Minimum contention window, slots [default=%(default)r]")
    parser.add_argument(
        "--cw-max", dest="cw_max", type=intx, default=128,
        help="Maximum contention window, slots [default=%(default)r]")
    parser.add_argument(
        "--busy-threshold-db", dest="busy_threshold_db", type=eng_float, default=eng_notation.num_to_str(float(-55.0)),
        help="Carrier-sense threshold, dB [default=%(default)r]")
    parser.add_argument(
        "--beacon-interval", dest="beacon_interval", type=eng_float, default=eng_notation.num_to_str(float(2.0)),
        help="Liveness beacon period, s [default=%(default)r]")
    parser.add_argument(
        "--peer-timeout", dest="peer_timeout", type=eng_float, default=eng_notation.num_to_str(float(8.0)),
        help="Declare a peer down after this long, s [default=%(default)r]")
    parser.add_argument(
        "--peers", dest="peers", type=str, default='2,3,4',
        help="Comma-separated peer addresses, e.g. 2,3,4 [default=%(default)r]")
    parser.add_argument(
        "--uri", dest="uri", type=str, default='ip:192.168.1.10',
        help="Set Pluto URI [default=%(default)r]")
    return parser


def main(top_block_cls=bpsk_csma_pluto, options=None):
    if options is None:
        options = argument_parser().parse_args()

    qapp = Qt.QApplication(sys.argv)

    tb = top_block_cls(beacon_interval=options.beacon_interval, busy_threshold_db=options.busy_threshold_db, cw_max=options.cw_max, cw_min=options.cw_min, http_port=options.http_port, mac_slot=options.mac_slot, my_addr=options.my_addr, nickname=options.nickname, peer_addr=options.peer_addr, peer_timeout=options.peer_timeout, peers=options.peers, rf_freq=options.rf_freq, uri=options.uri)

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
