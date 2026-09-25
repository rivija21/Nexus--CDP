# BPSK full-duplex link — PlutoSDR / ANTSDR

FDD full-duplex extension of the working `Bpsk_file_transfer_transmit/receive_pluto`
flowgraphs. Same PHY (BPSK, 250 ksym/s, RRC α=0.35, differential, 64-bit access code,
16-bit length header); a new link layer adds addressing, CRC, stop-and-wait ARQ,
text chat and file transfer.

## Files

| File | Role |
|---|---|
| `bpsk_duplex_pluto.grc` | The flowgraph. One file, both nodes, parameterised at launch. |
| `bpsk_link.py` | Framing, addressing, ARQ and file segmentation. Pure Python, no GNU Radio import. |
| `test_bpsk_link.py` | Offline two-node simulation over a 15 %-loss channel. No radios needed. |

`bpsk_link.py` **must sit in the same directory** as the flowgraph. The embedded
Python block locates it from its own path as well as the working directory, so GRC
can be launched from anywhere.

If the **BPSK Link Layer** block is outlined in red, GRC hit an exception while
evaluating it. Read the message in the console pane at the bottom of the GRC window,
or run `grcc bpsk_duplex_pluto.grc` in a terminal for the full traceback. The usual
cause is `bpsk_link.py` not being beside the `.grc`.

## Frequency plan

| | TX | RX |
|---|---|---|
| Node A | 905.2 MHz | 910.2 MHz |
| Node B | 910.2 MHz | 905.2 MHz |

5 MHz separation. Wider is better — it is what keeps each node's own transmitter out
of its own receiver.

## Running

    grcc bpsk_duplex_pluto.grc          # or open in GRC and press F5

Node A:

    python3 bpsk_duplex_pluto.py --my-addr 1 --peer-addr 2 \
        --tx-freq 905.2M --rx-freq 910.2M --uri ip:192.168.1.11

Node B:

    python3 bpsk_duplex_pluto.py --my-addr 2 --peer-addr 1 \
        --tx-freq 910.2M --rx-freq 905.2M --uri ip:192.168.1.10

Type into the box at the top of the QT window and press Enter. Received text,
link events and errors print to the terminal.

| Input | Effect |
|---|---|
| any text | sent to the peer as one DATA frame, acknowledged |
| `/sendfile <path>` | segmented into `frag_size`-byte fragments, sent, reassembled as `rx_<name>` |
| `/stats` | link counters: frames, retransmissions, CRC failures, duplicates |
| `/ping` | one-word round-trip probe |

## Frame format

Carried as the PHY payload, after the access code and length header:

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | scrambler offset (clear text) |
| 1 | 1 | version (4b) \| frame type (4b) |
| 2 | 1 | destination address |
| 3 | 1 | source address |
| 4 | 1 | sequence number |
| 5 | 1 | acknowledged sequence number |
| 6 | 1 | flags (bit 0 = retransmission) |
| 7 | 2 | payload length, uint16 BE |
| 9 | N | payload |
| 9+N | 4 | CRC-32 over the header and payload, BE |

Everything from byte 1 onward is scrambled; see **Payload scrambling** below.

Types: `0 IDLE`, `1 DATA`, `2 ACK`, `3 FILE_START`, `4 FILE_DATA`, `5 FILE_END`.
Address `0xFF` is broadcast. Frames whose destination is neither `my_addr` nor
broadcast are counted and discarded.

## Protocol behaviour

- **CRC** is computed and checked in Python (`zlib.crc32`), not by `crc32_bb`, so
  corrupt frames are counted rather than silently dropped.
- **Stop-and-wait ARQ**: one outstanding data frame. Retransmit after `ack_timeout`,
  up to `max_retries`, then drop and advance. ACKs are sent immediately and bypass
  the transmit queue, so an ACK is never stuck behind pending data.
- **Duplicate suppression**: a repeated sequence number from the same source is
  acknowledged again (the previous ACK was lost) but not delivered twice.
- **Idle filler**: the link models its own airtime and emits an `IDLE` frame whenever
  the modelled TX backlog falls below `queue_ahead` (30 ms). This keeps the Pluto TX
  buffer flowing — a burst-only transmitter leaves partial buffers stranded in
  `iio_buffer_push` — and keeps the far end's FLL, symbol sync and Costas loop locked
  between messages. Set `queue_ahead = 0` for a silent-between-frames transmitter.
- **Liveness**: three seconds with no valid frame prints `peer unreachable`.

## FDD self-interference — the thing that will bite you

Both radios transmit continuously on the same board as their own receiver. Three
settings in this flowgraph exist for that reason and should not be reverted to the
values in the half-duplex flowgraphs:

| Setting | Value here | Was | Why |
|---|---|---|---|
| RX `bandwidth` | 1 MHz | 20 MHz | The analog baseband filter is what rejects your own transmitter 5 MHz away. At 20 MHz it lands in the ADC and eats your dynamic range. |
| RX `gain1` | `manual`, 45 dB | `slow_attack` | AGC would track your own TX leakage instead of the peer. |
| TX `attenuation` | 20 dB | 89 dB (slider) | At 1 m you have ~30 dB of path loss; more power only worsens leakage. |

Isolation check, before wiring anything up: start **node A alone**. Its own RX
spectrum (tuned to 910.2 MHz) should show a flat noise floor. A hump means your own
transmitter is getting through — increase `tx_atten`, separate the TX and RX
antennas, or widen the frequency split.

Confirm the AD9363 is actually in FDD with independent LOs:

    iio_attr -u ip:192.168.1.11 -c ad9361-phy altvoltage0 frequency   # RX LO
    iio_attr -u ip:192.168.1.11 -c ad9361-phy altvoltage1 frequency   # TX LO
    iio_attr -u ip:192.168.1.11 -d ad9361-phy ensm_mode               # expect fdd

## Tuning

| Parameter | Default | Notes |
|---|---|---|
| `frag_size` | 256 | Payload bytes per frame. Larger = faster file transfer, more to lose per error. |
| `ack_timeout` | 0.5 s | One frame is ~15–20 ms on air; 0.5 s is generous. Lower it once the link is stable. |
| `max_retries` | 5 | Attempts after the first before the frame is dropped. |
| `queue_ahead` | 0.03 s | Seconds of TX buffer kept full. |
| `threshold` (access code) | 2 | Bit errors tolerated in the 64-bit access code. Raise to 3–4 in a poor link; CRC still guards delivery. |
| FLL `w` | 0.03 | Band-edge FLL ahead of the RRC filter, absorbing the XO offset between the two Plutos. Raise to 0.06 if the constellation rotates; delete the block to return to the exact proven RX chain. |

Throughput is bounded by stop-and-wait: one frame plus one ACK per round trip,
roughly 4–5 kB/s with 256-byte fragments. A sliding window would fix that and is the
natural next step.

## Reading `/stats` when one direction is dead

Compare both nodes' counters side by side. The decisive fields:

| Symptom | Meaning |
|---|---|
| `idle tx=0` | The tick port is not being served. Every ARQ timer and the transmit filler hang off it, so nothing retransmits, the queue never drains, and the Pluto TX buffer starves. |
| `retx=0` while `queued>0` and `ack rx=0` | Same cause: the retransmit timer lives in the same handler as the idle filler. |
| `queued` climbing | One frame is outstanding and unacknowledged; everything behind it waits. |
| `bad=0` on both nodes | The RF link is clean. The fault is above the PHY. |
| `dup` rising on the far node | Your ACKs are not arriving, so the peer keeps retransmitting. |

Why a stalled tick kills the transmitter rather than merely delaying it: the
preamble alone is 384 bytes, 3072 symbols, **12288 samples** at 4 sps, while the
Pluto sink pushes in blocks of `buffer_size` = 8192. Without the idle filler
keeping the stream continuous, the first buffer of a frame is pure preamble and
the header, payload and postamble sit in a half-filled second buffer until more
samples arrive — possibly minutes later. The frame reaches the air split in two,
and the far end usually cannot decode it.

A watchdog thread now covers this: if no tick arrives for 250 ms it drives
`on_tick` itself at 10 ms and prints a warning naming the Message Strobe block.
If you see that warning, the strobe is not connected in the flowgraph you
actually generated — regenerate from the current `.grc`.

## Transmit-queue drift (fixed in this version)

The transmit filler is an open-loop model: the link layer estimates how much
airtime it has handed to the modulator and emits an IDLE frame whenever that
backlog drops below `queue_ahead`. Nothing reports back from the Pluto sink, so
any error in the estimate accumulates.

The PHY header is **12 bytes**, not 4: `header_format_default` writes
`d_access_code_len + 32` bits, and the default access code is 64 bits, so
96 bits = 12 bytes (8 bytes access code, then the 16-bit length twice). An
earlier `overhead` of `preamble + postamble + 4` therefore understated every
frame by 8 bytes in 472, or 1.7%.

That 1.7% is handed out every second and never recovered:

| Elapsed | Queue depth ahead of the Pluto sink |
|---|---|
| 60 s | 1.07 s |
| 300 s | 5.21 s |

Once the queue exceeds `max_retries × ack_timeout` = 3 s, a frame's six attempts
all expire before it has physically been transmitted, so **every** frame drops
while the RF link is still perfect. The signature is a burst of
`dropped after 6 attempts` with `bad=0` on both nodes and the far node still
receiving idle frames normally.

Three changes:

- `overhead` is now `preamble_size + postamble_size + 12`.
- `AIRTIME_MARGIN = 1.05` in `bpsk_link.py` biases the model to under-produce.
  A 5% duty-cycle gap is harmless; an unbounded queue is not.
- The ARQ deadline is anchored to `busy_until`, the modelled instant the frame
  leaves the antenna, instead of to the moment it was queued. The retransmit
  timer is now independent of queue depth whatever the cause.

`/stats` reports `tx backlog` so the condition is visible. It should sit near
zero. A value that climbs steadily means the airtime model is optimistic again.

A dropped file fragment now aborts the remaining transfer and says so, rather
than emitting one drop message per fragment for a file that can no longer
reassemble.

## Payload scrambling (added)

Symptom: text is flawless, most file fragments get through, and one particular
fragment fails all six attempts and aborts the transfer. Neighbouring fragments
of the same size succeed.

A retransmission resends a **bit-identical** frame. ARQ therefore recovers from
random channel errors but is powerless against a failure caused by the payload
content itself — six attempts reproduce the same waveform six times.

Differential BPSK encodes a zero bit as *no phase transition*, so a run of zero
bits is transmitted as unmodulated carrier. That starves the timing error
detector, gives the band-edge FLL a pure tone instead of spectral edges, and
puts energy where the AD9363's `bbdc` tracking will try to remove it. Real files
are full of such runs — JPEG entropy-coded data stuffs a `0x00` after every
`0xFF`, and flat image regions code to long zero sequences.

Measured on a 24 kB JPEG, longest run of consecutive zero bits per frame:

| Payload | Unscrambled | Scrambled |
|---|---|---|
| 254 zero bytes | 2033 symbols | 11 |
| 254 × `0xAA` | 32 | 8 |
| JPEG header region | 71 | 9 |
| Worst of 96 JPEG fragments | 71 | 14 |

Every frame now carries a clear-text scrambler offset in byte 0; everything
after it is XORed with a fixed LFSR keystream rotated by that offset. The
keystream is generated from this source, so nothing has to be exchanged. The
transmitted waveform no longer depends on the payload.

The offset is derived from the sequence number, and **changes on every retry**,
so the six attempts of a frame are six different waveforms rather than one
repeated six times. A frame that is unlucky in its bit pattern now gets a
genuinely different chance on each attempt.

Both nodes must run the same `bpsk_link.py` — the wire format changed. A
mismatched pair shows every frame as a CRC failure (`bad` climbing, `rx ok`
flat). The flowgraph is unchanged.

## File repair and end-to-end feedback

A lost fragment is no longer fatal, and the sender is now told what happened.

Two frame types were added. When the receiver sees `FILE_END` with gaps, it
sends **`FILE_NACK`** listing exactly which fragment indices never arrived and
keeps everything it already has. The sender requeues only those fragments and a
fresh `FILE_END`. When the file is finally complete the receiver writes it and
sends **`FILE_DONE`** carrying the CRC verdict, which is the sender's
confirmation that the transfer actually succeeded:

    [link] peer confirmed test_image.jpg received complete and CRC-correct

Three related changes:

- A dropped fragment is **counted, not logged**, and not treated as fatal. The
  NACK round recovers it. This removes the wall of `dropped after 6 attempts`
  lines and the one-shot abort that replaced them.
- Losing `FILE_START` used to abandon the transfer, since every fragment behind
  it arrives with no file context. It now **restarts the transfer**, because the
  usual reason to lose it is that the link happened to be down at the moment you
  pressed Enter.
- If no report comes back at all, the sender resends `FILE_END` to ask again,
  up to `max_file_rounds` (8), then reports giving up. Silence is no longer a
  possible outcome.

`/stats` gained `frags lost` and `file rounds`.

### Which build am I running

`REVISION` is printed in the startup banner on both consoles:

    [link] link up [r4-scrambled-repair]: my_addr=1 peer_addr=2 ...

If the two machines print different revisions, fix that before debugging
anything else.

### Measured

24 kB JPEG, 96 fragments, against channels with burst outages long enough to
exhaust all six ARQ attempts:

| Channel | Result | Fragments lost | Repair rounds |
|---|---|---|---|
| 2.5 s outage every 6 s, 5% loss | intact, 23 s | 1 | 0 |
| 2.2 s outage every 4 s, 10% loss | intact, 40 s | 3 | 1 (FILE_START restart) |
| 2.0 s outage every 3 s, 25% loss | intact, 120 s | 14 | 1 (6 fragments resent) |

Uniform random loss without outages: intact at 5%, 15%, 30% and 50% frame loss,
the last in 123 s.

## Verifying without radios

    python3 test_bpsk_link.py

Runs both link layers against a simulated 15 %-loss channel with 20 ms propagation:
a file A→B and a chat message B→A concurrently. Expect retransmissions > 0,
duplicates suppressed, and `file transferred intact: True`.

A 58 kB file in 230 fragments over the same channel completes in ~55 s of link
time with 81 retransmissions, zero drops and the backlog pinned at 0.04 s.
