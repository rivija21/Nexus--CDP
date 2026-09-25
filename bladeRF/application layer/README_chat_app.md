# BPSK Link Terminal — application layer

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
