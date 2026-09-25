#!/usr/bin/env python3
"""Generates qpsk_duplex_phy.grc from the embedded-block sources."""
import json
import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "qpsk_duplex_phy.grc")

io_cache = json.load(open(os.path.join(HERE, "io_cache.json")))
FRAMING_PATH = os.path.join(os.path.dirname(HERE), "qpsk_framing.py")
_framing = open(FRAMING_PATH).read()
# drop the module docstring; keep everything from the first import onwards
FRAMING_BODY = _framing[_framing.index("import zlib"):]


def inline_framing(src):
    """Replace the `from qpsk_framing import ...` shim with the module itself.

    GRC's working directory is wherever gnuradio-companion was launched from,
    not the flowgraph's directory, so an embedded block must not rely on an
    import from disk: if it fails, GRC cannot introspect the block, it shows up
    red with no ports, and every connection to it is silently dropped.
    """
    out = []
    skip_prefixes = ("_here =", "for _p in (os.getcwd()", "    if _p and _p",
                     "        sys.path.insert")
    for line in src.splitlines(True):
        if line.startswith("from qpsk_framing import"):
            out.append("# ---------- inlined from qpsk_framing.py ----------\n")
            out.append(FRAMING_BODY.rstrip() + "\n")
            out.append("# ---------- end of inlined qpsk_framing.py --------\n")
        elif any(line.startswith(p) for p in skip_prefixes):
            continue
        else:
            out.append(line)
    return "".join(out)


SRC_SOURCE = inline_framing(open(os.path.join(HERE, "epy_frame_source.py")).read())
SRC_SINK = inline_framing(open(os.path.join(HERE, "epy_frame_sink.py")).read())


def blk(name, bid, params, x, y, rot=0, state="enabled", io_cache=None):
    params = dict(params)
    states = {
        "bus_sink": False, "bus_source": False, "bus_structure": None,
        "coordinate": [x, y], "rotation": rot, "state": state,
    }
    if io_cache is not None:
        states["_io_cache"] = io_cache
    return {
        "name": name,
        "id": bid,
        "parameters": {k: str(v) for k, v in params.items()},
        "states": states,
    }


def var(name, value, x, y, comment=""):
    return blk(name, "variable", {"comment": comment, "value": value}, x, y)


def rng(name, label, start, stop, step, value, x, y, widget="counter_slider"):
    return blk(name, "variable_qtgui_range", {
        "comment": "", "gui_hint": "", "label": label, "min_len": "200",
        "orient": "QtCore.Qt.Horizontal", "rangeType": "float",
        "start": start, "step": step, "stop": stop, "value": value,
        "widget": widget,
    }, x, y)


blocks = []

# ---- options -------------------------------------------------------------
blocks.append({
    "parameters": {
        "author": "", "catch_exceptions": "True", "category": "[GRC Hier Blocks]",
        "cmake_opt": "", "comment": "",
        "copyright": "", "description":
            "Differential QPSK full-duplex FDD PHY for two Pluto/E200 nodes. "
            "Needs qpsk_framing.py in this directory.",
        "gen_cmake": "On", "gen_linking": "dynamic", "generate_options": "qt_gui",
        "hier_block_src_path": ".:", "id": "qpsk_duplex_phy", "max_nouts": "0",
        "output_language": "python", "placement": "(0,0)",
        "qt_qss_theme": "", "realtime_scheduling": "",
        "run": "True", "run_command": "{python} -u {filename}",
        "run_options": "prompt", "sizing_mode": "fixed",
        "thread_safe_setters": "", "title": "QPSK duplex PHY (Pluto / E200)",
        "window_size": "(1000,1000)",
    },
    "states": {"bus_sink": False, "bus_source": False, "bus_structure": None,
               "coordinate": [8, 8], "rotation": 0, "state": "enabled"},
})

# ---- role / RF parameters -----------------------------------------------
blocks += [
    blk("role", "parameter", {
        "alias": "", "comment": "a: TX 915 / RX 917    b: TX 917 / RX 915",
        "hide": "none", "label": "FDD role (a or b)", "short_id": "r",
        "type": "str", "value": "a"}, 8, 100),
    blk("uri", "parameter", {
        "alias": "", "comment": "", "hide": "none", "label": "Pluto URI",
        "short_id": "u", "type": "str", "value": "ip:192.168.1.10"}, 168, 100),

    var("f_low", "915e6", 8, 180, "FDD pair"),
    var("f_high", "917e6", 88, 180),
    var("tx_freq", "f_low if role == 'a' else f_high", 176, 180),
    var("rx_freq", "f_high if role == 'a' else f_low", 384, 180),

    var("samp_rate", "2e6", 8, 260),
    var("sps", "4", 96, 260, "samples per symbol"),
    var("nfilts", "32", 160, 260),
    var("excess_bw", "0.35", 224, 260, "matched on TX and RX"),
    var("bandwidth", "1.5e6", 320, 260, "AD9361 analog filter"),
    var("buffer_size", "16384", 424, 260),
    var("access_threshold", "3", 536, 260, "bit errors allowed in access code"),
    var("rrc_taps",
        "firdes.root_raised_cosine(nfilts, nfilts, 1.0/float(sps), "
        "excess_bw, 11*sps*nfilts)", 8, 330),
]

blocks += [
    rng("tx_atten", "TX attenuation (dB)", "0", "40", "1", "10", 8, 410),
    rng("tx_amp", "TX amplitude", "0.0", "1.0", "0.01", "0.6", 168, 410),
    rng("timing_bw", "Timing loop BW", "0.0", "0.2", "0.005", "6.28/100.0",
        328, 410, "slider"),
    rng("phase_bw", "Costas loop BW", "0.0", "0.5", "0.005", "6.28/100.0",
        488, 410, "slider"),
]

blocks.append(blk("qpsk", "variable_constellation_rect", {
    "comment": "", "const_points":
        "[0.707+0.707j, -0.707+0.707j, -0.707-0.707j, 0.707-0.707j]",
    "imag_sect": "2", "precision": "8", "real_sect": "2", "rot_sym": "4",
    "soft_dec_lut": "None", "sym_map": "[0, 1, 2, 3]",
    "w_imag_sect": "1", "w_real_sect": "1"}, 656, 410))

blocks.append(blk("eq_alg", "variable_adaptive_algorithm", {
    "comment": "CMA", "cons": "qpsk", "delta": "10.0", "ffactor": "0.99",
    "modulus": "4", "step_size": "1e-4", "type": "cma"}, 816, 410))

blocks.append(blk("import_0", "import", {
    "alias": "", "comment": "", "imports": "from gnuradio.filter import firdes"},
    656, 260))

# ---- transmit ------------------------------------------------------------
TEST_MSG = ("pmt.cons(pmt.PMT_NIL, pmt.init_u8vector(16, "
            "[81,80,83,75,32,108,105,110,107,32,116,101,115,116,32,33]))")

blocks += [
    blk("blocks_message_strobe_0", "blocks_message_strobe", {
        "affinity": "", "alias": "",
        "comment": "'QPSK link test !' every 2 s -- remove once the\\n"
                   "Python chat application drives this port",
        "maxoutbuf": "0", "minoutbuf": "0", "msg": TEST_MSG, "period": "2000"},
        8, 560),

    blk("frame_source", "epy_block", {
        "_source_code": SRC_SOURCE,
        "affinity": "", "alias": "",
        "comment": "PDU in -> framed bytes out,\\nPN filler while idle",
        "idle_chunk": "1024", "maxoutbuf": "4096", "minoutbuf": "0"},
        256, 544, io_cache=io_cache["grc_src/epy_frame_source.py"]),

    blk("digital_constellation_modulator_0", "digital_constellation_modulator", {
        "affinity": "", "alias": "", "comment": "differential QPSK + RRC",
        "constellation": "qpsk", "differential": "True", "excess_bw": "excess_bw",
        "log": "False", "maxoutbuf": "4096", "minoutbuf": "0",
        "samples_per_symbol": "sps", "truncate": "False", "verbose": "False"},
        480, 528),

    blk("blocks_multiply_const_vxx_0", "blocks_multiply_const_vxx", {
        "affinity": "", "alias": "", "comment": "keep the DAC out of clipping",
        "const": "tx_amp", "maxoutbuf": "4096", "minoutbuf": "0",
        "type": "complex", "vlen": "1"}, 728, 544),

    blk("iio_pluto_sink_0", "iio_pluto_sink", {
        "affinity": "", "alias": "", "attenuation1": "tx_atten",
        "bandwidth": "int(bandwidth)", "buffer_size": "buffer_size",
        "comment": "", "cyclic": "False", "filter": "",
        "filter_source": "'Auto'", "fpass": "0", "frequency": "int(tx_freq)",
        "fstop": "0", "len_tag_key": "", "samplerate": "int(samp_rate)",
        "type": "fc32", "uri": "uri"}, 968, 512),

    blk("qtgui_freq_sink_x_0", "qtgui_freq_sink_x", {
        "affinity": "", "alias": "", "autoscale": "False", "average": "0.2",
        "bw": "samp_rate", "comment": "", "ctrlpanel": "False", "fc": "tx_freq",
        "fftsize": "1024", "freqhalf": "True", "grid": "True",
        "gui_hint": "1,0,1,1", "label": "Relative Gain", "legend": "False",
        "name": '"TX spectrum"', "nconnections": "1", "type": "complex",
        "units": "dB", "update_time": "0.10", "wintype":
        "window.WIN_BLACKMAN_hARRIS", "ymax": "10", "ymin": "-140"},
        968, 640),
]

# ---- receive -------------------------------------------------------------
blocks += [
    blk("iio_pluto_source_0", "iio_pluto_source", {
        "affinity": "", "alias": "", "bbdc": "True",
        "bandwidth": "int(bandwidth)", "buffer_size": "buffer_size",
        "comment": "", "filter": "", "filter_source": "'Auto'", "fpass": "0",
        "frequency": "int(rx_freq)", "fstop": "0", "gain1": "'slow_attack'",
        "len_tag_key": "", "manual_gain1": "40", "maxoutbuf": "0",
        "minoutbuf": "0", "quadrature": "True", "rfdc": "True",
        "samplerate": "int(samp_rate)", "type": "fc32", "uri": "uri"},
        8, 800),

    blk("analog_agc2_xx_0", "analog_agc2_xx", {
        "affinity": "", "alias": "", "attack_rate": "1e-1",
        "comment": "normalises the input so the\\nCMA equaliser converges fast",
        "decay_rate": "1e-2", "gain": "1.0", "max_gain": "65536",
        "maxoutbuf": "4096", "minoutbuf": "0", "reference": "1.0",
        "type": "complex"}, 272, 816),

    blk("qtgui_freq_sink_x_1", "qtgui_freq_sink_x", {
        "affinity": "", "alias": "", "autoscale": "False", "average": "0.2",
        "bw": "samp_rate", "comment": "", "ctrlpanel": "False", "fc": "rx_freq",
        "fftsize": "1024", "freqhalf": "True", "grid": "True",
        "gui_hint": "1,1,1,1", "label": "Relative Gain", "legend": "False",
        "name": '"RX spectrum"', "nconnections": "1", "type": "complex",
        "units": "dB", "update_time": "0.10", "wintype":
        "window.WIN_BLACKMAN_hARRIS", "ymax": "10", "ymin": "-140"},
        272, 928),

    blk("digital_pfb_clock_sync_xxx_0", "digital_pfb_clock_sync_xxx", {
        "affinity": "", "alias": "", "comment": "timing recovery, 2 sps out",
        "filter_size": "nfilts", "init_phase": "nfilts/2",
        "loop_bw": "timing_bw", "max_dev": "1.5", "maxoutbuf": "4096",
        "minoutbuf": "0", "osps": "2", "sps": "sps", "taps": "rrc_taps",
        "type": "ccf"}, 496, 784),

    blk("digital_linear_equalizer_0", "digital_linear_equalizer", {
        "adapt_after_training": "True", "affinity": "", "alg": "eq_alg",
        "alias": "", "comment": "CMA multipath compensation",
        "maxoutbuf": "4096", "minoutbuf": "0", "num_taps": "15", "sps": "2",
        "training_sequence": "[ ]", "training_start_tag": "corr_est"},
        760, 800),

    blk("digital_costas_loop_cc_0", "digital_costas_loop_cc", {
        "affinity": "", "alias": "",
        "comment": "carrier phase / fine frequency", "maxoutbuf": "4096",
        "minoutbuf": "0", "order": "4", "use_snr": "False", "w": "phase_bw"},
        1008, 800),

    blk("qtgui_const_sink_x_0", "qtgui_const_sink_x", {
        "affinity": "", "alias": "", "autoscale": "False", "axislabels": "True",
        "comment": "should show four tight clusters when locked",
        "grid": "True", "gui_hint": "2,0,1,2", "legend": "False",
        "name": '"RX constellation"', "nconnections": "1", "size": "1024",
        "type": "complex", "update_time": "0.10", "xmax": "2", "xmin": "-2",
        "ymax": "2", "ymin": "-2"}, 1008, 928),

    blk("digital_constellation_decoder_cb_0", "digital_constellation_decoder_cb", {
        "affinity": "", "alias": "", "comment": "symbols -> constellation index",
        "constellation": "qpsk", "maxoutbuf": "4096", "minoutbuf": "0"},
        1264, 816),

    blk("digital_diff_decoder_bb_0", "digital_diff_decoder_bb", {
        "affinity": "", "alias": "", "coding": "digital.DIFF_DIFFERENTIAL",
        "comment": "removes the QPSK phase ambiguity", "maxoutbuf": "4096",
        "minoutbuf": "0", "modulus": "4"}, 1512, 816),

    blk("blocks_unpack_k_bits_bb_0", "blocks_unpack_k_bits_bb", {
        "affinity": "", "alias": "", "comment": "2 bits per symbol, MSB first",
        "k": "2", "maxoutbuf": "4096", "minoutbuf": "0"}, 1760, 816),

    blk("digital_correlate_access_code_tag_xx_0",
        "digital_correlate_access_code_tag_xx", {
            "affinity": "", "alias": "",
            "access_code":
                "'1010110011011101101001001110001011110010100011000010000011111100'",
            "comment": "GNU Radio default access code",
            "maxoutbuf": "4096", "minoutbuf": "0",
            "tagname": "'pkt_start'", "threshold": "access_threshold",
            "type": "byte"}, 1976, 816),

    blk("frame_sink", "epy_block", {
        "_source_code": SRC_SINK,
        "affinity": "", "alias": "",
        "comment": "header + CRC32 check,\\none PDU per good frame",
        "maxoutbuf": "0", "minoutbuf": "0", "tag_key": "'pkt_start'"},
        2248, 832, io_cache=io_cache["grc_src/epy_frame_sink.py"]),

    blk("blocks_message_debug_0", "blocks_message_debug", {
        "affinity": "", "alias": "", "comment": "received frames print here",
        "en_uvec": "True", "log_level": "info"}, 2472, 832),
]

# ---- notes ---------------------------------------------------------------
blocks.append(blk("note_0", "note", {
    "alias": "", "comment":
        "Bench diagnostic twin of qpsk_chat.py. Run one node with -r a, "
        "the other with -r b. qpsk_framing.py must be in this directory.",
    "note": "read me"}, 328, 100))

connections = [
    ["blocks_message_strobe_0", "strobe", "frame_source", "send"],
    ["frame_source", "0", "digital_constellation_modulator_0", "0"],
    ["digital_constellation_modulator_0", "0", "blocks_multiply_const_vxx_0", "0"],
    ["blocks_multiply_const_vxx_0", "0", "iio_pluto_sink_0", "0"],
    ["blocks_multiply_const_vxx_0", "0", "qtgui_freq_sink_x_0", "0"],
    ["iio_pluto_source_0", "0", "analog_agc2_xx_0", "0"],
    ["analog_agc2_xx_0", "0", "digital_pfb_clock_sync_xxx_0", "0"],
    ["analog_agc2_xx_0", "0", "qtgui_freq_sink_x_1", "0"],
    ["digital_pfb_clock_sync_xxx_0", "0", "digital_linear_equalizer_0", "0"],
    ["digital_linear_equalizer_0", "0", "digital_costas_loop_cc_0", "0"],
    ["digital_costas_loop_cc_0", "0", "digital_constellation_decoder_cb_0", "0"],
    ["digital_costas_loop_cc_0", "0", "qtgui_const_sink_x_0", "0"],
    ["digital_constellation_decoder_cb_0", "0", "digital_diff_decoder_bb_0", "0"],
    ["digital_diff_decoder_bb_0", "0", "blocks_unpack_k_bits_bb_0", "0"],
    ["blocks_unpack_k_bits_bb_0", "0",
     "digital_correlate_access_code_tag_xx_0", "0"],
    ["digital_correlate_access_code_tag_xx_0", "0", "frame_sink", "0"],
    ["frame_sink", "rx", "blocks_message_debug_0", "print"],
]

doc = {
    "options": blocks[0],
    "blocks": blocks[1:],
    "connections": connections,
    "metadata": {"file_format": 1, "grc_version": "3.10.12.0"},
}

with open(OUT, "w") as f:
    yaml.safe_dump(doc, f, default_flow_style=False, sort_keys=False,
                   width=100000)
print("wrote", OUT)
