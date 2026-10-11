# BPSK Link Terminal — application layer  ·  `r6b-csma`

A chat application layer on top of the existing full-duplex FDD BPSK PlutoSDR
link. The modem, the ARQ and the file protocol are unchanged in behaviour; what
is new is a structured event interface out of the link layer, an application
envelope, and a browser front end served by the flowgraph itself.

```
chat_ui.html          browser: transcript, telemetry, composer
    |  HTTP + Server-Sent Events on 127.0.0.1:<http_port>
bpsk_app.py           envelope, chat model, delivery receipts, HTTP server
    |  ('tx', bytes) / ('log', str) / ('evt', dict)
bpsk_link.py          addressing, stop-and-wait ARQ, file segmentation
    |  PDU
GNU Radio flowgraph   DBPSK modem, PlutoSDR / ANT-SDR E200
```

No third-party Python packages. The server is `http.server` plus Server-Sent
Events, so nothing has to be installed on the lab machine.

## Files

| File | Status | Purpose |
|---|---|---|
| `bpsk_link.py` | modified | link layer, now `r5-applayer`: message ids, structured events, opaque file metadata |
| `bpsk_app.py` | new | application envelope, chat model, receipts, telemetry, HTTP + SSE server |
| `chat_ui.html` | new | the front end, single self-contained file (light "Ledger" theme) |
| `ui/` | new | the sources the front end is built from, and three alternative themes |
| `bpsk_duplex_pluto_link_layer.py` | modified | GRC embedded block: PMT bridge, EVM/SNR estimator, server lifecycle |
| `bpsk_duplex_pluto.grc` | modified | two new parameters, one new connection |
| `bpsk_duplex_pluto.py` | modified | regenerated flowgraph, in step with the `.grc` |
| `bpsk_offline_demo.py` | new | run both nodes with no radio, over a simulated channel |
| `test_bpsk_link.py` | extended | two-node loopback test over a lossy virtual channel |
| `test_shim.py` | new | tests the GRC block with `pmt`/`gr` stubbed out |
| `test_shutdown.py` | new in r5.1 | shutdown-path regression test: the watchdog and the UI server must not outlive `stop()` |
| `test_multinode.py` | new in r6a | four nodes on a shared medium: addressing, room fan-out, direct messages, per-peer ARQ, discovery |
| `bpsk_mac.py` | **new in r6b** | CSMA/CA state machine. Pure Python, no GNU Radio import |
| `bpsk_csma_link_layer.py` | **new in r6b** | GRC block for the shared channel: MAC gating plus a carrier-sense input |
| `bpsk_csma_pluto.grc` / `.py` | **new in r6b** | the shared-channel flowgraph. The FDD one is untouched |
| `bpsk_csma_zmq.py` | **new in r6b** | one node over ZeroMQ instead of a radio |
| `csma_zmq_channel.py` | **new in r6b** | the shared medium: sums overlapping waveforms |
| `test_mac.py` | **new in r6b** | 25 checks on the contention rules |
| `test_csma_shim.py` | **new in r6b** | 16 checks that frames are gated by the MAC |

All of them must sit in the same directory.

## Running it

Node A:

```
python3 bpsk_duplex_pluto.py --my-addr 1 --peer-addr 2 \
    --tx-freq 905.2e6 --rx-freq 910.2e6 --uri ip:192.168.1.10 \
    --nickname "Rivija" --http-port 8088
```

Node B:

```
python3 bpsk_duplex_pluto.py --my-addr 2 --peer-addr 1 \
    --tx-freq 910.2e6 --rx-freq 905.2e6 --uri ip:192.168.1.10 \
    --nickname "Bench B" --http-port 8088
```

The chat opens in the default browser a moment after the flowgraph starts, at
`http://127.0.0.1:8088/`. The GNU Radio window stays up beside it with the
constellation, spectrum and time-domain scopes; its edit box is now a fallback
console for `/stats` and `/help`.

Set `open_ui` to `False` in the *BPSK Chat Node* block to stop it opening a
browser tab on every restart. The server binds `127.0.0.1` only.

Both nodes must run `r5-applayer`. The revision is printed in the banner and
shown in the telemetry panel; a mismatched pair is otherwise indistinguishable
from a bad channel. Text from an r4 node still appears in the transcript,
marked as coming from an unknown sender.

### Without hardware

```
python3 bpsk_offline_demo.py --loss 0.10
```

Two complete nodes on a simulated channel, at `http://127.0.0.1:8088/` and
`:8089/`. Same code path as the radio, so it is a fair rehearsal for a demo —
and useful for developing the UI without occupying the E200s.

Opening `chat_ui.html` directly as a file, with nothing serving it, puts the
page into a clearly marked demo mode with mock traffic. That is for looking at
the layout, not for testing the link.

### Tests

```
python3 test_bpsk_link.py     # two nodes, 15% frame loss, text + image + receipts
python3 test_shim.py          # GRC block, EVM estimator, server lifecycle
```

## What the UI shows

The transcript is set as a ruled log rather than as chat bubbles: each entry
carries the sender, the address, the time and the receipt in a fixed gutter, so
a screenshot of it reads as evidence rather than as a messenger.

- **Status bar** — peer reachability, SNR estimated from the recovered
  constellation, RX level, TX/RX goodput, frame error rate, transmit backlog.
- **Entries** — per message: `queued → sent → delivered`, retry count,
  fragment progress for attachments, and the receiver's CRC verdict.
- **Attachments** — an image is framed as a plate with a caption line giving
  its name, size and a save button; click it for the full-size view. A file
  still arriving shows a proportioned placeholder with the fragment counter
  rather than a broken image, and anything that is not an image becomes a card
  with an extension chip.
- **ARQ panel** — the whole `stats` dict live: frames, retransmissions, CRC
  failures, duplicates, drops, idle filler, repair rounds, queue depth.
- **Link log** — the same lines the GNU Radio console prints.

### Changing the theme

`chat_ui.html` is generated. Everything it does lives in `ui/core.js` and
`ui/body.html`; only the stylesheet distinguishes one theme from another, so
switching cannot change the behaviour or the API.

```
python3 ui/build.py d     # Ledger     - editorial, ruled entries   (current)
python3 ui/build.py a     # Studio     - modern product UI, indigo
python3 ui/build.py b     # Instrument - flat and dense, teal
python3 ui/build.py c     # Aurora     - airy, telemetry in a drawer
python3 ui/build.py       # write all four side by side, to compare
```

Each writes `chat_ui.html` next to the flowgraph. To adjust the current theme,
edit `ui/d_ledger.css` and run the build again.

Composer: Enter sends, Shift+Enter is a newline, files go in by drag-and-drop,
paste, or the clip button. `/nick <name>`, `/stats`, `/ping`, `/help`,
`/sendfile <path>`, `/clear`.

Attachments are kept in `chat_files/` next to the flowgraph — `tx_<id>_<name>`
for what you sent, `rx_<name>` for what arrived.

## Protocol additions

Nothing in the frame header changed. Two payloads gained structure.

**Application envelope**, inside the payload of a `DATA` frame:

| Bytes | Field |
|---|---|
| 0 | `0x9A` magic |
| 1 | app version (4b) \| app type (4b) — 1 = text, 2 = presence |
| 2–5 | message id, uint32 BE, sender-scoped |
| 6 | part index |
| 7 | number of parts |
| 8 | nickname length *N* |
| 9…9+N−1 | nickname, UTF-8 |
| rest | this part's payload |

A payload that does not start with `0x9A` is delivered as plain text from an
unknown sender, which is what keeps an un-upgraded node visible. Messages
longer than one fragment are split across parts and reassembled per
(source, message id).

**FILE\_START payload**, extended:

| Bytes | Field |
|---|---|
| 0–5 | file size (uint32 BE), fragment count (uint16 BE) |
| 6 | `0xA5` marker — absent on r4, which is how the old layout is detected |
| 7 | filename length |
| 8… | filename, UTF-8 |
| then | metadata length (uint16 BE) + JSON `{"n":nick,"m":mime,"i":id,"t":ts}` |

The link layer never parses the metadata blob; it hands it up verbatim.

## Signal quality

Input port 0 of the chat block takes the Costas loop output. Every 200 ms it
takes 4096 symbols, estimates the amplitude as `mean(|Re x|)`, forms the error
against the hard decision on the real axis, and reports

```
EVM = sqrt(mean|x - sgn(Re x)·A|²) / A          SNR ≈ −20·log₁₀(EVM)
```

Against synthetic AWGN this tracks the true SNR to within about 0.1 dB from
6 to 20 dB (`test_shim.py`). It is a receiver-side estimate that includes
timing jitter and residual phase error, not just thermal noise — which is the
number worth showing, since it is what the decoder actually sees. When the
symbol amplitude collapses the reading is cleared rather than left stale.

## Flowgraph changes

- *BPSK Link Layer* → *BPSK Chat Node*: new parameters `nickname`,
  `http_port`, `open_ui`, `tx_freq`, `rx_freq`, and a complex input port.
- New connection: `Costas Loop → BPSK Chat Node` (measurement only; the data
  path is untouched).
- New GRC parameters `nickname` and `http_port`.
- The Edit Box label now says it is a console.

The server and the watchdog thread still start on the first tick, not in
`__init__`, so GRC's validation pass never binds the port. `stop()` releases
it, so the flowgraph can be restarted without an "address already in use".


---

# r5.1-stable — what changed and why

A stabilisation release over `r5-applayer`. **No wire-format change**: `WIRE_COMPAT`
is still `r5`, so an r5.1 node interoperates with an r5 node. The banner will show
different `REVISION` strings — that is expected and is not a mismatch.

## The crash

The symptom was an intermittent GNU Radio crash with the browser showing
`radio process not reachable - reconnecting...` in red. That banner is
`EventSource.onerror` in the UI — it means the HTTP server stopped answering,
which means the flowgraph process died. It was never a radio fault.

**Cause 1 — the watchdog outlived `stop()`.** `_watchdog_loop` ran `while True:`
and `stop()` never signalled it, so it kept calling `message_port_pub` while the
runtime destroyed the block. Worse than a race: `stop()` freezes `_last_strobe`,
so the watchdog *engages* 250 ms into teardown and then publishes at 100 Hz.
Measured before the fix: **38 port publishes in the 600 ms after `stop()`**.
After: **0**.

**Cause 2 — a late message resurrected the block.** `stop()` set
`_server = None`, which `_ensure_running()` could not distinguish from "never
started", so any in-flight `rx_frame`, `tick` or `chat_in` re-bound the TCP port
and restarted the watchdog mid-teardown.

Both are fixed by a single `threading.Event` (`_halt`) that `stop()` sets first
and that every publish, handler and resource acquisition checks. `stop()` now
joins the watchdog before releasing anything, and is idempotent.

## Diagnosis was impossible before

`catch_exceptions` was `True` on the Options block, so **any** Python fault
stopped the flowgraph silently — no traceback, window still open but dead, HTTP
server gone, red banner. Every distinct bug presented identically. It is now
`False`: a fault prints a traceback naming the file and line.

## Full fix list

| # | Fix | Evidence |
|---|---|---|
| A | Watchdog exits on `stop()` and is joined | 38 → 0 publishes after stop |
| B | `_halt` guards `_ensure_running` and every handler | late message no longer re-binds the port |
| C | `catch_exceptions=False` | faults now produce a traceback |
| D | Attachment staging, fragmenting and queue building moved off the GNU Radio thread | 8 MiB file: **38.45 ms → 0.81 ms** on the radio thread |
| E | Incomplete multi-part text expires after `PART_TIMEOUT` (60 s) | `_rx_parts` 1 → 0 entries, operator told |
| F | Attachment token map bounded at `MAX_FILE_TOKENS` (1000) | 1250 registered → 1000 held |
| G | `rx_payload_bytes` counted after the duplicate check | duplicate frame: 200 B → 100 B |
| H | `pmt.intern` results cached; `_dispatch` runs up to 100 Hz | one allocation instead of one per publish |
| I | Inbound transfers keyed by source (`_rx_msgs`) | progress cannot be attributed to the wrong entry |

### D in detail

`send_file()` used to fragment the blob and build every transmit-queue entry on
the GNU Radio thread, inside the block lock. For an 8 MiB attachment that is
33 028 entries and ~38 ms — against a `queue_ahead` cushion of 30 ms and a Pluto
sink buffer of 8.19 ms, so the transmitter starved and the far end lost lock.

Now `ChatApp.submit()` stages to disk, calls `bpsk_link.segment()` and
`bpsk_link.build_file_items()` **on the calling thread** (an HTTP worker), and
hands the radio thread a finished list that goes in with one `deque.extend()`.
`/sendfile` from the console does the same on a one-shot worker thread.
Radio-thread cost is now flat in file size.

| Attachment | r5 radio thread | r5.1 radio thread |
|---|---|---|
| 1 MiB | 5.22 ms | 0.14 ms |
| 4 MiB | 19.33 ms | 0.40 ms |
| 8 MiB | 38.45 ms — **underrun** | 0.81 ms |

## Verification

```
python3 test_bpsk_link.py     # 15 checks - protocol, text, image, receipts
python3 test_shim.py          # 13 checks - GRC block, EVM, server lifecycle
python3 test_shutdown.py      # 10 checks - NEW: teardown must be clean
```

All 38 pass. `test_bpsk_link.py` produces results bit-identical to r5
(25.77 s simulated, 171 fragments, 73 retransmissions, 0 drops), which is the
evidence that none of these fixes changed protocol behaviour.

## Not changed

The modem, the framing, the scrambler, the CRC, the ARQ and the addressing are
untouched. Preamble sizing, multi-node support and windowing are r6 work.


---

# r6a-multipeer — the state refactor

Stage one of two. **r6a makes the link layer talk to many peers; it does not
touch the MAC.** Carrier sense, backoff and burst transmission are r6b. Splitting
them means a regression in either has exactly one candidate cause.

**Wire format is unchanged** — `WIRE_COMPAT` is still `r5`, so an r6a node
interoperates with r5.1 and r5 nodes. Only the revision string differs.

## What moved

Every field that made the link single-peer now lives on a `Peer` record:

| Field | r5.1 | r6a |
|---|---|---|
| `tx_seq`, `pending`, `txq` | scalars on `LinkState` | per `Peer` |
| `last_rx_seq` | `dict[src]` | per `Peer` |
| `peer_seen`, `peer_up` | scalars | per `Peer` |
| `tx_file`, `rx_file` | single slot each | per `Peer` |
| `busy_until` | scalar | **still scalar** — it models *our* transmitter |

`Peer.pending` is deliberately a slot a container can replace: r7 raises it to a
window of *W* without restructuring anything around it.

`_pump()` now round-robins across peers, so one busy correspondent cannot starve
the others. With three peers, up to three frames are outstanding at once — still
transmitted one after another, each ACK deadline anchored to the modelled instant
its own frame leaves the antenna.

## Addressing model

- **Room** — every known peer, as one reliable unicast each. The receipt reads
  *delivered* only once every recipient has acknowledged every part. An
  unacknowledged `0xFF` broadcast would give room messages weaker delivery than
  direct ones, which is the wrong trade for this link; the cost is N× airtime.
- **Direct message** — one address. Other nodes count the frame in `rx_notme`
  and discard it, which is the per-receiver addressing the spec asks for.
- **Discovery** — a valid frame from an unconfigured address adds that peer, so
  a node can join a running network without restarting the others.

App frame type `0x3` (`AT_ROOM`) distinguishes a room message from a direct one
inside the existing envelope. No header field was added.

## Configuration

    python3 bpsk_duplex_pluto.py --my-addr 1 --peers 2,3,4 \
        --tx-freq 905.2e6 --rx-freq 910.2e6 --uri ip:192.168.1.10 \
        --nickname "Alpha" --http-port 8088

`--peers` is comma separated. Leave it empty and `--peer-addr` is used instead,
so an unchanged two-node setup still works.

## The UI

A conversation bar sits under the banner: **Room** first, then one chip per peer
with a liveness dot and an unread badge. The transcript holds every message and
each row carries its conversation, so switching is a filter, not a refetch.
The composer sends to whichever conversation is open; attachments follow the
same target via an `X-Target` header.

## Without hardware

    python3 bpsk_offline_demo.py --nodes 4

Four complete nodes on a shared virtual medium at `:8088` … `:8091`. Every
transmission is offered to every other node and loss is drawn independently per
receiver, so a frame can reach one node and miss another — the case that
actually exercises per-peer ARQ.

## Verification

```
python3 test_bpsk_link.py     # 15 - unchanged 2-node protocol
python3 test_shim.py          # 13 - GRC block, EVM, server lifecycle
python3 test_shutdown.py      # 10 - teardown stays clean
python3 test_multinode.py     # 32 - NEW: four nodes
```

70 checks. The 38 inherited ones pass **unmodified**, which is the evidence that
the refactor did not change single-peer behaviour.

`test_multinode.py` proves: the room reaches all three peers and the receipt
counts all 12 acknowledgements; a direct message reaches only its target and the
other two nodes count it in `rx_notme`; a file transfers byte-identically and is
filed under the sender's conversation; every node builds a 3-peer roster with
nicknames; per-peer ARQ state is genuinely independent; an unconfigured node is
discovered on first contact; and after a 3-second drain nothing anywhere is left
holding an unacknowledged frame.

## A bug this stage found

Multi-node testing exposed a latent defect in r5: `_last_presence` was
initialised to `0.0` and the announcement throttle compares against it, so the
**first** nickname announcement was swallowed whenever the time base started
near zero. Invisible under `time.time()`, fatal under a test harness. The
sentinel is now `None`. That is the argument for simulated time using the same
code path as the radio.

## Next: r6b

- Carrier sense: a power detector on the RX stream gating the TX path.
- Random backoff with a contention window.
- Burst transmission — the idle filler must go, since a node that transmits
  continuously holds a shared channel for ever. Enable the Pluto sink's
  `len_tag_key` for burst mode; it is currently `''`.
- The preamble must **stay at 384 B**: consecutive bursts arrive from different
  transmitters with different crystal offsets, so every burst is a cold
  acquisition. The shortening analysis from the 2-node case does not carry over.
- Slot timing, if any, must come from sample counts, not the Message Strobe.


---

# r6b-csma — medium access

Stage two. **The FDD system is untouched**: `bpsk_duplex_pluto.grc` and its shim
still exist and still run. r6b adds a second, separate flowgraph.

## What is different about this stage

r6a was a pure software refactor and I could verify all of it. r6b changes the
PHY, and two things here have **not been executed anywhere**:

- whether gr-iio's burst mode (`len_tag_key`) behaves on your E200;
- whether the 2048-sample source buffer holds without underruns on your machine.

The MAC itself is tested — 41 checks across `test_mac.py` and
`test_csma_shim.py` — because it is pure Python with an injectable clock.
Everything below the MAC is unverified until it runs on your bench.

## The change that matters most

The FDD flowgraph sets the Pluto **source** `buffer_size = 32768`. A node
cannot detect another's transmission until that buffer delivers:

| Source buffer | Sense latency | Vulnerable period | vs 23.3 ms frame |
|---|---|---|---|
| 32768 (FDD) | 32.8 ms | 65.5 ms | 2.81× — **worse than ALOHA** |
| 8192 | 8.2 ms | 16.4 ms | 0.70× — marginal |
| **2048 (r6b)** | **2.05 ms** | 4.1 ms | 0.18× — workable |

At 32768 a station could start a whole frame *after* another began and still
sense an idle channel, so the backoff would buy nothing. The CSMA flowgraph
uses 2048, and `mac_slot` (3 ms) is set above that latency because a station
cannot react to what it has not yet heard.

## Contention rules

| | |
|---|---|
| slot | 3 ms, ≥ sense latency |
| SIFS | 1 slot — acknowledgements only |
| DIFS | 2 slots — data must see this much idle first |
| CW | 8 … 128 slots, binary exponential |

- **Backoff freezes** while the channel is busy rather than redrawing, so a
  station that has already waited keeps its credit. That is what makes it fair.
- **Acknowledgements never contend.** They wait SIFS and go, so they always beat
  a station serving DIFS plus backoff.
- **NAV**: receiving a data frame holds the station off for SIFS plus one ACK
  duration. Without it the medium looks free in the gap between a frame and its
  acknowledgement, and a third station walks straight into the ACK.
- **Collisions are inferred from the retry flag.** The MAC never sees an
  acknowledgement — that is the link layer's business — so a frame carrying
  `FLAG_RETRY` is taken as an attempt that failed, and the window doubles. It is
  an approximation (a retry can be plain noise) and it errs toward backing off,
  which is the safe direction.

## The idle filler is gone

`queue_ahead = 0`. A station that transmits continuously holds a shared channel
for ever. In its place, `beacon_interval` (2 s) emits one short broadcast so a
silent node still proves liveness, and `peer_timeout` rises to 8 s to match.

**The preamble stays at 384 B.** Consecutive bursts arrive from different
transmitters with different crystal offsets and amplitudes, so no loop state
carries over and every burst is a cold acquisition. The shortening analysis from
the two-node case does not apply here.

## Running it

    python3 bpsk_csma_pluto.py --my-addr 1 --peers 2,3,4 \
        --rf-freq 915e6 --uri ip:192.168.1.10 \
        --nickname "Alpha" --http-port 8088

One carrier for everyone — this is TDD. `--busy-threshold-db` (default −55) is
the first thing to tune: read your noise floor off the RX spectrum with all
nodes idle and set it comfortably above.

## The ZeroMQ testbed

    python3 csma_zmq_channel.py --nodes 4          # the air
    python3 bpsk_csma_zmq.py --node 1 --http-port 8088
    python3 bpsk_csma_zmq.py --node 2 --http-port 8089
    python3 bpsk_csma_zmq.py --node 3 --http-port 8090
    python3 bpsk_csma_zmq.py --node 4 --http-port 8091

Four real flowgraphs with the Pluto blocks swapped for ZeroMQ, and a channel
process that **sums** their sample streams. That summation is the point: the
r6a harness passes whole frames, so it can only deliver or drop them, and a
collision is neither — it is waveform overlap. The channel excludes each
station's own contribution, which models a perfect TX/RX switch.

It prints how much airtime carried energy and what fraction of that had more
than one station transmitting. **That second number is what CSMA exists to
drive toward zero.** Run once with a very small `--mac-slot` to see it without
effective contention, then at the default, and compare.

The channel's plumbing is verified here: a transmitter hears itself at 0.000,
others hear 1.000, two simultaneous carriers sum to 2.000, and equal-and-
opposite ones cancel to 0.0001.

`gr-zeromq` convention is that a `push_sink` binds and a `pull_source`
connects, so the channel connects to the transmit side and binds the receive
side. If your build is the other way round, `--swap-bind` flips it.

## Verification

```
python3 test_bpsk_link.py     # 15
python3 test_shim.py          # 13
python3 test_shutdown.py      # 10
python3 test_multinode.py     # 30
python3 test_mac.py           # 25  NEW
python3 test_csma_shim.py     # 16  NEW
```

109 checks. The 68 inherited pass unmodified.

## Bench order

1. Confirm r5.1 and r6a on the FDD flowgraph — unchanged files, so a failure
   there is not r6b's.
2. Run the ZeroMQ testbed. It needs no radios and exercises the real modem.
3. Only then the CSMA flowgraph on hardware, **two nodes first**. If burst mode
   is going to misbehave it will do so with two, and two is far easier to read.
4. Then four.
