# QPSK full-duplex chat / file link (ANT-SDR E200 · PlutoSDR)

A GNU Radio 3.10 application derived from your `GNU_Radio_Example_QPSK_tx_rx.grc`
lab flowgraph. Two nodes, FDD full duplex, text and file transfer with CRC and
selective retransmission — plus a GRC twin of the same PHY for bench diagnostics.

```
qpsk_chat.py          link layer (segmentation, ACK/NAK, files) + CLI      <- run this
qpsk_phy.py           GNU Radio top_block: modem + Pluto TX/RX
qpsk_framing.py       frame build/parse, PN9 whitening, bit assembler      (no GNU Radio)

qpsk_duplex_phy.grc   the same PHY as a GRC flowgraph, with QT diagnostics
grc_src/              sources the .grc is generated from (embedded blocks + generator)

test_framing.py       framing tests, offline
test_link.py          link-layer tests over a simulated lossy PHY, offline
test_phy_loopback.py  full modem, both nodes, joined by a channel model, no hardware
test_grc_blocks.py    the .grc's embedded blocks driven through the real modem
```

`qpsk_framing.py` must sit beside `qpsk_chat.py` and `qpsk_phy.py`. The `.grc`
is self-contained: `grc_src/make_grc.py` inlines the framing module into the two
embedded blocks, so the flowgraph runs from any directory and GRC can introspect
the blocks whatever directory it was launched from.

## 1. Run the chat application

Node A and node B, each with its own E200:

```bash
python3 qpsk_chat.py --role a --uri ip:192.168.1.10     # TX 915.0 MHz, RX 917.0 MHz
python3 qpsk_chat.py --role b --uri ip:192.168.1.10     # TX 917.0 MHz, RX 915.0 MHz
```

| input | effect |
|---|---|
| `any text` | send a text message, blocks until ACKed |
| `/send path/to/file` | send a file (received files land in `rx_files/`) |
| `/ping` | round-trip time to the peer |
| `/stats` | PHY and link counters |
| `/config` | current RF settings |
| `/quit` | stop |

Overrides: `--tx-freq`, `--rx-freq`, `--samp-rate`, `--sps`, `--tx-atten`,
`--tx-amplitude`, `--rx-gain-mode manual --rx-gain 40`, `--bandwidth`,
`--buffer-size`, `--out-dir`.

## 2. Run the GRC flowgraph

`qpsk_duplex_phy.grc` is the same PHY as a flowgraph: same constellation, same
loops, same framing blocks, with QT GUI constellation and spectrum displays and a
Message Strobe that transmits `QPSK link test !` every 2 s. Open it in GNU Radio
Companion, or run it headless:

```bash
gnuradio-companion qpsk_duplex_phy.grc      # then set the role in the 'role' parameter
python3 qpsk_duplex_phy.py -r a -u ip:192.168.1.10   # after Generate
```

Frames received from the peer print via Message Debug. Use it to answer "is the
link working at all" before debugging the protocol layers: watch the RX
constellation open into four clusters, then watch the peer's strobe text appear.

The two custom blocks in it (`frame_source`, `frame_sink`) are Embedded Python
Blocks whose readable sources live in `grc_src/`; `grc_src/make_grc.py`
regenerates the `.grc` after you edit them, inlining `qpsk_framing.py` into each
one. Two things matter if you ever hand-edit the `.grc`: an embedded block must
not import anything from the flowgraph's directory (GRC's working directory is
wherever it was launched from, and a failed import leaves the block red with no
ports, silently dropping every connection to it), and `_io_cache` belongs in the
block's `states:` mapping, not `parameters:`. `frame_source` takes a PDU on its `send` port and
`frame_sink` emits one PDU per good frame on `rx`, so the flowgraph can be driven
by anything that speaks PDUs, not only by the strobe.

## 3. RF plan

| parameter | value |
|---|---|
| sample rate | 2 MSPS |
| sps | 4 → 500 ksym/s, **1 Mbit/s raw**, ~800 kbit/s of payload |
| occupied BW | 500 k × (1 + 0.35) ≈ **675 kHz** |
| duplex spacing | 2 MHz (915.0 / 917.0) |
| TX analog filter | 1.5 MHz |
| RX selectivity | from the auto-designed AD9361 FIR at the chosen sample rate — the gr-iio Pluto **source** has no bandwidth setter, so lowering `--samp-rate` is what narrows the receiver |

**Self-interference is the thing that will break this, not the DSP.** The AD9361
transmits and receives simultaneously; at 2 MHz spacing your own TX is 60–80 dB
above the peer's signal at the RX port. On the bench:

* separate TX and RX antennas, as far apart as the bench allows, ideally
  cross-polarised;
* start at `--tx-atten 20` and only reduce it if the peer sees nothing;
* if you cable the nodes together, put **≥ 40 dB** of attenuation in each path —
  a direct SMA connection will saturate the receiver and can damage the RX input;
* if the RX constellation only opens up while the local TX is idle, increase the
  duplex spacing (e.g. 915.0 / 920.0) before touching loop bandwidths.

## 4. What changed relative to the lab flowgraph

| lab `.grc` | here | why |
|---|---|---|
| `analog_random_source_x` | `FrameSource` (framed bytes, PN filler when idle) | real payload; a continuously fed transmitter keeps the peer's loops locked between messages |
| channel model, delay, QT time sinks | removed from the app, kept as `test_phy_loopback.py` | simulation-only impairments, but still useful for testing without radios |
| TX `excess_bw` 0.5 vs RX RRC 0.35 | both 0.35 | matched filter; the mismatch costs SNR |
| RX ends at `unpack_k_bits` → time sink | → `correlate_access_code_tag_bb` → `FrameSink` | framing and delivery |
| no AGC | `analog.agc2_cc` after the Pluto source | the CMA equaliser converges much faster from a normalised input |
| single frequency | FDD pair per `--role` | true full duplex on one AD9361 |
| default buffers | every modem block capped to 4096 items | pipeline latency was adding ~0.7 s to every ACK; see §7 |

Everything else is your chain, unchanged: `pfb_clock_sync_ccf` (osps = 2) →
CMA `linear_equalizer` (15 taps, sps = 2) → `costas_loop_cc` (order 4) →
`constellation_decoder_cb` → `diff_decoder_bb(mod 4)` → `unpack_k_bits_bb(2)`.
Differential encoding is what makes the QPSK phase ambiguity irrelevant, so no
preamble-based phase resolution is needed.

## 5. Frame format

```
+---------------+-------+-------+-------------+--------+
| access code   | len   | ~len  | payload     | crc32  |
| 8 B           | 2 B   | 2 B   | N B         | 4 B    |
+---------------+-------+-------+-------------+--------+
|<- unwhitened->|<--------- PN9 whitened ------------->|
```

* Access code is `digital.packet_utils.default_access_code` (0xACDDA4E2F28C20FC),
  so `correlate_access_code_tag_bb` is used as-is, with 3 bit errors tolerated.
* `~len` rejects essentially every correlator false alarm before the CRC runs
  (measured: 2000 tags on pure noise → 0 frames accepted).
* PN9 whitening prevents long constant-symbol runs, which otherwise stall the
  timing loop on files with large zero regions.
* The assembler probes tag offsets 0, +1 and −1, so it does not matter whether
  your gr-digital build tags the last access-code bit or the first bit after it.

Link layer inside each payload: `type(1) msg_id(2) seq(2)` + body, with
META / DATA / EOM / ACK / NAK. A message goes out as META, all DATA chunks
(200 B each), then EOM; the receiver replies ACK, or NAK listing exactly which
chunks are missing. ACK and NAK frames are queued **ahead** of bulk data, so a
file transfer in one direction never delays the reverse channel.

## 6. Verification

No radios here, so everything below hardware was tested against a real GNU Radio
install (3.10.9.2 — one point release behind your 3.10.12):

| test | result |
|---|---|
| `test_framing.py` | 42 frames per run recovered bit-exactly from a noise-filled stream delivered in irregular chunks, at tag biases 0 / +1 / −1; a flipped payload bit is caught by the CRC; 2000 false tags on noise → 0 frames |
| `test_link.py` | text, 20 kB file and simultaneous bidirectional exchange over a simulated PHY at 0–45 % frame loss; correct at every level, reports failure rather than hanging beyond that |
| `test_phy_loopback.py` | the real modem, both nodes, through `channels.channel_model`: 2 MSPS, 12 dB SNR, 1 kHz carrier offset, 200 ppm clock error, 3-tap multipath → 20 kB file in 0.70 s, **0 bad headers, 0 bad CRCs, 0 retransmissions** |
| `grcc qpsk_duplex_phy.grc` | compiles clean; all 15 stream and 2 message connections generated as intended |
| `test_grc_blocks.py` | the GRC-generated embedded blocks driven through the real modem: 12/12 PDUs recovered byte-identical |

The gr-iio calls in `qpsk_phy.py` were checked against what GRC itself generates
from the Pluto blocks — `iio.fmcomms2_source_fc32(uri, [True, True], buffer_size)`
and `iio.fmcomms2_sink_fc32(uri, [True, True], buffer_size, False)` plus the same
setter sequence — so the two paths agree. A legacy `iio.pluto_*` fallback is in
place for older builds.

What remains genuinely untested is the RF itself: antenna isolation, AGC behaviour
under your own transmitter, and whether 2 MHz of duplex spacing is enough on your
bench.

## 7. Tuning

| symptom | knob |
|---|---|
| constellation rotating slowly | raise `phase_bw` (default 0.0628) |
| constellation smeared radially | raise `timing_bw`, or check `excess_bw` matches on both nodes |
| tags arrive but headers never validate | bit-order or differential-decode mismatch — `/stats` shows `tags` high, `frames` 0, `bad_header` ≈ `tags` |
| headers validate, CRC fails | link is marginal: reduce `--tx-atten`, raise duplex spacing, or drop `--samp-rate` to 1 MSPS (halves the rate, gains ~3 dB and narrows the RX filter) |
| `tags` stays 0 | no lock at all: check the spectrum on the RX frequency, try `--rx-gain-mode manual --rx-gain 60`, confirm the peer is on the mirrored role |
| every message needs several rounds, `naks_received` 0 | ACK latency exceeds the timeout: lower `PhyConfig.max_buf_items` or raise `ACK_TIMEOUT` in `qpsk_chat.py` |
| TX underruns in the console | raise `--buffer-size` to 32768 |

`/stats` fields: `tags` (correlator hits), `bad_header`, `bad_crc`, `frames`,
`payload_bytes`, `tx_frames`, plus `retransmitted_chunks`, `naks_sent`,
`naks_received`.
