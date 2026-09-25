# BPSK Link Terminal — application layer  ·  `r5.2-stable`

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
| `test_regressions.py` | **new in r5.2** | one reproduction per defect fixed in r5.2; fails on r5.1, passes on r5.2 |

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
python3 test_shutdown.py      # teardown must be clean
python3 test_regressions.py   # the r5.2 fixes, one scenario each
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

# r5.2-stable — what changed and why

A second stabilisation release. **No wire-format change**: `WIRE_COMPAT` is still
`r5`, so r5.2 interoperates with r5 and r5.1. Every r5.1 fix (A–I above) was
re-verified and holds. The defects below were found by driving scenarios the
existing tests do not cover; each one is reproduced by `test_regressions.py`,
which fails on r5.1 and passes on r5.2.

| # | Defect in r5.1 | Seen as | Fix |
|---|---|---|---|
| J | FILE_NACK / FILE_DONE went to the **back** of the single TX queue | With a file going each way, the smaller transfer's verdict waited behind the peer's whole upload; the sender gave up after 8 × 3 s and marked a file it had delivered as **failed** — on a perfect channel | Two queues: replies and chat in `txq_hi` (replies at the front), bulk fragments in `txq` |
| K | Chat text queued behind file fragments | A line typed during a 200 kB upload was delivered **114 s** later | Chat/presence go through `txq_hi` → **0.2 s** |
| L | Receiver ignored a repeated FILE_END once the file was complete | FILE_DONE lost 6× in a row → sender reports **failed**, file is intact at the receiver | Receiver keeps the last verdict and repeats it |
| M | FILE_DONE names no transfer | A late verdict could complete the **next** file while its fragments were still queued | OK verdict is accepted only if its CRC matches the current transfer |
| N | `_purge_file_queue` removed every `file*` label | Our restart/give-up also deleted the NACK/DONE we owed the peer, stalling the other direction | Purge only our own bulk queue |
| O | In-progress transfer matched on (name, fragment count) only | A new `image.png` after an abandoned one inherited its fragments; one lost fragment became a **CRC failure** instead of a repair round | Match on the metadata blob too (it carries the sender's message id); the abandoned entry is shown as *incomplete* |
| P | Received files always written to `rx_<name>` | Two pasted screenshots (both `image.png`): the first entry silently showed the **second** picture | `rx_<name>`, then `rx_<name>-2`, … |
| Q | A failed middle part of a multi-frame text was overwritten by later ACKs | Entry stuck at a single tick for ever | `failed` is sticky for text |
| R | Untrusted filename / metadata used as-is | A CRC-valid FILE_START with a NUL in the name raised `ValueError` out of `on_rx`; JSON metadata that is not an object raised `AttributeError` | `clean_name()` in the link layer; metadata type-checked |
| S | Any exception escaping a handler now aborts the process | `catch_exceptions=False` turns one malformed frame into `std::terminate` | Handlers catch at the block boundary, print the full traceback, drop that input, keep running (tracebacks rate-limited) |
| T | `fetch()` rejects non-Latin-1 header values | Files named in Sinhala, Tamil, CJK or with an emoji could not be attached; the sanitiser also reduced `ඡායාරූපය.png` to `png` | Name percent-encoded by the page, decoded by the server; sanitiser keeps any script |
| U | Local HTTP server accepted any origin and any Host | Any web page open in the same browser could transmit on air (text/plain POST, no preflight); a DNS-rebinding page could read `/api/state` | Host must be loopback; POSTs with a foreign `Origin` refused; attachments served with `Content-Security-Policy: sandbox` and an RFC 5987 filename |
| V | `/stats` in the GNU Radio edit box printed nothing on the terminal | Fallback console silent when the browser is unavailable | Printed on the terminal as well as in the transcript |

### Notes

- **`catch_exceptions=False` is kept.** It is what surfaces faults outside this
  block. Inside it, the boundary guard (S) gives the same traceback without
  taking the radio down.
- **Queue priority changes behaviour, not protocol.** Stop-and-wait still has
  one frame in flight; the receiver never depended on chat and file frames
  being in order, and the sequence space is shared as before. File throughput
  with no chat traffic is unchanged (the 200 kB transfer in the bidirectional
  test takes the same 116 s on r5.1 and r5.2).
- `test_bpsk_link.py` is **not** bit-reproducible run to run: the FILE_START
  metadata carries a wall-clock timestamp, whose JSON length varies, which
  shifts the modelled airtime slightly. The r5.1 note claiming bit-identical
  results (25.77 s, 73 retransmissions) holds only for some runs.
- Not a defect, noted for r6: after a node restarts, its sequence numbers begin
  again at 0, so if the peer's last-seen sequence from it happened to be 0 the
  first new frame is ACKed and discarded as a duplicate. In practice that frame
  is the presence announcement. A per-boot session identifier removes this;
  the encryption work provides one.
