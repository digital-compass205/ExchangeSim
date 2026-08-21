# ExchangeSim — detailed documentation

Everything beyond getting it running: how it is built, how the two venues
differ, what each behaviour was transcribed from, and where a specification was
silent and a choice had to be made.

For installing, starting and using it, see **[README.md](README.md)**.

---

## Contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Auctions](#auctions)
- [Price rules](#price-rules)
- [Configuration](#configuration)
- [Static data](#static-data)
- [The control surface](#the-control-surface)
- [The web board](#the-web-board)
- [The message audit](#the-message-audit)
- [Scenarios](#scenarios)
- [Testing a client's unhappy paths](#testing-a-clients-unhappy-paths)
- [Deployment](#deployment)
- [Development](#development)
- [Sources](#sources)
- [Known gaps](#known-gaps)

---

## What it does

| Capability | Detail |
|---|---|
| Order entry | New, amend (price and quantity), cancel, and mass cancel where the venue has it |
| Order types | Whatever the venue supports: limit only at Japannext, limit and market at HKEX; TIF Day / IOC / FOK, post-only, IOC MinQty |
| Matching | Order-driven continuous, price/time priority, executed at the **resting** order's price |
| Auctions | Call auctions that uncross at one price: maximum volume, then lowest imbalance, then surplus direction, then closest to the reference price |
| Crossing | Full book matching plus self-trade prevention, keyed on the MPID (Japannext) or on a client-supplied ID (HKEX) |
| Markets | Japannext: four, addressed by `TargetSubID`. HKEX: one book per market segment, chosen by the security |
| Market states | Pre-open, auctions, lunch break, open, closed, halted — venue-wide, per market, or per instrument |
| Market data | Book, depth, BBO, trade tape and session statistics via CLI, with a live monitor |
| Web board | Browser view over every running venue, live over Server-Sent Events |
| Message audit | Every message in and out, and every mutating command, with each field named and each value explained |
| Recovery | Persistent sequence numbers, ResendRequest, SequenceReset-GapFill, Cancel on Disconnect |
| Negative testing | Forced rejects, delayed reports, dropped reports |

Market state changes are **command-driven only** — there is no scheduler. A CI
job cannot wait six hours for the afternoon session, so the simulator sits in
whatever phase it was told to occupy until told otherwise. It also means a test
outcome never depends on the clock.

## Architecture

```
exchangesim/
  audit.py      the message and command record behind the audit view
  core/         venue-agnostic: book, matching, orders, state machine,
                market data, validation, prices, reactor
  fix/          protocol-generic FIX: codec, dictionary, session layer
                (4.2 and FIXT.1.1), persistent store, acceptor, field naming
  binary/       the second encoding OCG-C publishes: frame, data types,
                presence map, and the codec that turns one into a fix.Message
  venues/
    common_commands.py  every venue-agnostic control command
    japannext/  the Japannext dialect, rules, handlers and reference data
    hkex/       the HKEX OCG-C dialect, likewise
  web/          the browser board: its own process, a client of the venues
  control/      JSON-lines control server and command registry
  cli/          the exsim command-line client
  scenario/     declarative scenario runner
  runner/       process entry point
config/         one JSON file per venue instance, plus the web board's
scenarios/      example scenarios; the CI suite
deploy/         systemd unit and install script
tools/          pdftext.py, a stdlib PDF text extractor for reading venue specs
tests/          1,190 tests, unittest only
```

Three layers with a hard dependency rule — arrows point inwards only, and
`core/` may not import from `venues/` or from any protocol gateway:

```
  FIX gateway            venue-specific wire dialect
      |  commands v          ^ events
  ---------------------------------------------
  Venue module           validation rules, reference data, message mapping
      |
  ---------------------------------------------
  Venue-agnostic core    book, matching, lifecycle, state, market data
```

Gateways turn wire messages into core commands and core events back into wire
messages. The core never builds a FIX field; the gateway never touches the book.
Adding an exchange is a new package under `venues/` plus one line in the
registry — and it inherits the whole control surface, the CLI and the web board
without further work.

**How well that held for the second venue.** HKEX needed four core additions,
and each is a capability the core was missing rather than a Hong Kong special
case: market orders, self-match prevention keyed on the order alongside the
existing per-market mode, an "ignore price check" execution instruction, and a
mass-cancel reason. The session layer gained four options for FIXT.1.1, all
defaulting off, so FIX 4.2 behaviour is byte-identical. Nothing in `core/` knows
the word "HKEX".

### Where the two venues differ

| | Japannext | HKEX |
|---|---|---|
| Session | FIX 4.2, ResendRequest recovery, client may reset at Logon | FIXT.1.1, `NextExpectedMsgSeqNum(789)` negotiation, client reset **refused** |
| Instrument | `Symbol(55)` | `SecurityID(48)`; `Symbol` is not in the dialect at all |
| Market routing | `TargetSubID(57)` per message | the security's segment — no message names a market |
| Price limits | one band table around the nominal price | two rules: the 9-times rule and the quotation rule (see below) |
| Auctions | none — the rules say so outright | POS and CAS |
| Self-trade prevention | per-market mode, keyed on MPID | per-order `SelfMatchPreventionID(2362)` |
| Groups | none | `<Parties>` and `<DisclosureInstructionGrp>` on every business message |
| Encodings | tag=value FIX | tag=value FIX **and** a binary encoding of the same protocol, on separate ports |

### One protocol, two encodings

HKEX publishes OCG-C as tag=value FIX and as a fixed-width binary format, and
entitles a client to one of them. Both are served here, and the second one is a
**codec, not a second gateway**: `binary/` decodes a frame into the same
`fix.Message` everything above it already speaks, and encodes one back.

```
  BROKER1 (tag=value)          BROKER3 (binary)
        |  9012                      |  9011
   FixCodec                     BinaryCodec        <- the only place that
        \____________  ________/                     knows which is which
                     \/
              Session (one per Comp ID)
                     |
              HkexApplication  -> engine -> one book per segment
```

A session names its encoding in the config and gets the matching codec, dialect
table and acceptor; the session table, the books, the control plane and the
audit are shared, so a binary client and a FIX client trade with each other and
appear on one tape. The audit entry records which codec produced it, and reads
it back with that same codec — a binary message opens as a hex dump.

What actually differs, beyond the bytes, is small and each piece has a home:

* **One Comp ID on the wire, the client's**, in both directions. The codec fills
  in the venue's own identity, so the session layer's CompID checks are unchanged.
* **No SendingTime, BeginString or ApplVerID** — the binary header is what
  section 7.2 lists and nothing else.
* **Logon carries no EncryptMethod and no HeartBtInt**, so two `SessionConfig`
  flags say whether a Logon negotiates them. Both default on: FIX is untouched.
* **A Reject is replayed on a resend**, where FIX practice gap-fills it.
* **No OrderCancelReject.** A refused cancel or amend is an Execution Report
  with `ExecType` `X` or `Y`, carrying the order's identity and running totals —
  which 35=9 has no fields for. The gateway builds that form for a binary
  session (`handlers.py:_for_wire`), because the extra fields come from the
  order and a codec that reached for one would be a gateway.
* **`<Parties>` is four flat broker fields and the disclosure group a bitmap**,
  read and written by getters in `venues/hkex/binary.py`.
* **Its required-field table is its own**: a Cancel Request has no OrderQty and
  SecurityExchange is optional throughout, so `dictionary.build_binary()` is a
  separate transcription rather than a flag on the FIX one.
* **Repeating blocks**, which FIX spells as a positional group and binary as a
  count followed by entries carrying their own two-byte presence maps
  (`binary/layout.py:Block`). The Party Entitlement Report is the one message
  that needs them, and it is also where the two encodings diverge structurally:
  FIX nests the broker in `<PartyEntitlementGrp><PartyDetailGrp>`, binary
  carries a flat `Broker ID` and hangs the entitlements off the message.

## Auctions

HKEX runs a Pre-opening Session (POS) and a Closing Auction Session (CAS). Both
accumulate orders without matching and then execute them all at one price, found
by the rule chain the exchange publishes: maximise the matchable quantity, then
take the lowest imbalance, then resolve in the direction of the surplus, then
the price closest to the reference price, then the higher of two.

Because the periods are command-driven, an auction is a sequence of commands:

```bash
exsim --port 9102 call state.set '{"market":"MAIN","state":"CLOSING_AUCTION"}'
#  ... orders accumulate; the book may legitimately cross ...
exsim --port 9102 call auction '{"market":"MAIN","symbol":"00700"}'
#  -> {"iep": "396.400", "iev": 2500, "imbalance": 800,
#      "imbalance_side": "BUY", "reason": "lowest imbalance", ...}
exsim --port 9102 call auction.lock '{"market":"MAIN"}'   # no more cancels
exsim --port 9102 call state.set '{"market":"MAIN","state":"CLOSED"}'  # uncross
```

Two order types matter here. An **at-auction limit order** is an ordinary limit
order entered during the call phase. An **at-auction order** carries no price at
all (`OrdType(40)=1`), takes whatever the auction settles on, and is filled
ahead of every limit order; anything it cannot fill is cancelled rather than
carried into continuous trading. The depth board shows them as a `MARKET` row
above `OVER` and below `UNDER`, since they have no price row of their own.

Price limits come in two stages, which is where most of the venue-specific
detail lives: ±15% of the previous close in POS and ±5% of the reference price
in CAS during the input period, then — once `auction.lock` runs — pinned to the
interval between the highest bid and the lowest ask as they stood at that
moment.

Three implementation points that are easy to get wrong:

- An at-auction order has no price, so it cannot sit in the sorted ladder. It is
  kept in a separate queue and deliberately excluded from the BBO and the depth
  board, which are statements about limit prices — but included in the order
  count and in cancel-all, so those stay honest.
- An auction price is only available when the limit books overlap. A book holding
  nothing but at-auction orders has no price of its own, and the venue then
  matches at the reference price rather than treating "no price" as "no trade".
- Uncrossing happens *before* the state change is applied, because moving to a
  closed state expires resting orders, and an auction run afterwards would find
  an empty book.

## Price rules

**Japannext** applies one band table around the instrument's nominal price, plus
a tick ladder that varies by price and index tier.

**HKEX** applies two rules, and they are separate things:

- The **9-times rule** is multiplicative against the static nominal price: a
  price nine times it or more, or a ninth of it or less, is refused. That is a
  Rule of the Exchange and is not configurable.
- The **quotation rule** is anchored on the live BBO — at most
  `quotation_spreads` (24) ticks behind your own side's best, and at most
  `aggression_spreads` (9) through the other side's. It stands down when the
  market is not matching, since there is no meaningful BBO to measure against.

A *spread* is a row of the SEHK spread table, which is both the tick ladder and
the unit the quotation rule counts in. Boundary semantics matter: the Schedule
publishes bands as "From 0.01 to 0.25 / Over 0.25 to 10.00", so 0.25 belongs to
the **lower** band and the next row starts at 0.251.

## Configuration

One JSON file per venue instance, naming its ports, sessions and markets.
Reference data paths resolve relative to the config file and default to the CSVs
shipped with the venue.

```json
{
  "venue": "japannext",
  "name": "JNX-SIM",
  "control": {"host": "127.0.0.1", "port": 9101, "token": null},
  "audit": {"capacity": 2000},
  "fix": {
    "host": "127.0.0.1", "port": 9001, "sender_comp_id": "JNXSIM",
    "store": "../var/japannext/fix",
    "sessions": [
      {"target_comp_id": "CLIENT1", "default_sub_id": "DAY",
       "markets": ["DAY", "NGHT", "DAYX", "DAYU"],
       "cancel_on_disconnect": true}
    ]
  },
  "markets": [{"name": "DAY", "state": "CLOSED", "stp_mode": "CANCEL_NEWEST"}]
}
```

Several exchanges run side by side as several processes, each with its own
config, ports and store directory. A crash or restart of one leaves the others
untouched.

HKEX is configured the same way, with `"venue": "hkex"`. Two of its keys have no
Japannext counterpart:

```json
{
  "markets": [{"name": "MAIN"}, {"name": "GEM"}],
  "smp": [{"id": "SMP0001", "instruction": "CANCEL_AGGRESSIVE"}],
  "limits": {"price_band_spreads": 24},
  "binary": {"host": "127.0.0.1", "port": 9011}
}
```

A market is a SEHK **market segment** (`MarketSegmentID(1300)`). No order message
names one — a security belongs to exactly one segment, and that decides which
book the order reaches. `smp` pre-registers the self-match prevention
instructions the real exchange holds against each `SelfMatchPreventionID` out of
band; `smp.register` adds more at runtime.

A session states which of the venue's two encodings it speaks, and connects to
that encoding's port:

```json
{"target_comp_id": "BROKER3", "protocol": "binary", "broker_ids": ["1003"],
 "markets": ["MAIN", "GEM"]}
```

`protocol` defaults to `"fix"`, so an existing config is unchanged; the shipped
file puts binary on 9011 and FIX on 9012, that being the encoding this venue is
usually driven with. `broker_ids` are the Broker Numbers that Comp ID may submit
under, which a Party Entitlement Request asks for. The `binary`
listener starts only when a session asks for one — a venue does not open a port
nobody has been given — and a Comp ID belongs to exactly one encoding: offered
the wrong one, the venue refuses the connection with a Logout saying so.

`audit.capacity` is how many messages and commands the audit view keeps, newest
wins. `0` switches recording off entirely, and costs nothing at all on the
message path.

## Static data

The instrument universe is a CSV — `reference/symbols.csv` at Japannext,
`reference/securities.csv` at HKEX, which adds a `segment` column. One row per
security: `symbol,name,lot_size,base_price,tier,shares_outstanding,tradable`.
Price bands and tick ladders are separate threshold tables in the same
directory. `#` lines are comments, so a table can carry its provenance next to
its values.

HKEX ships one such table rather than two: its order price limits are expressed
in *spreads* from the nominal price, so the band ladder is derived from the
spread table at load time rather than tabulated separately, which keeps the two
from ever disagreeing.

None of it requires a restart to change:

| Need | Command |
|---|---|
| Add a ticker | `instrument.add` |
| Drop one | `instrument.remove` — refused while it has live orders |
| Suspend, or move its band | `instrument.set` |
| Re-read the CSVs after editing | `reference.reload` |

`reference.reload` treats the CSV as authoritative for every symbol it contains,
so a runtime `instrument.set` override on such a symbol is discarded. A symbol
that has *disappeared* from the CSV is marked non-tradable rather than deleted,
because live orders may still reference it; use `instrument.remove` to be rid of
one entirely. A malformed CSV is reported as a command error and changes
nothing.

## The control surface

Everything the simulator can be told to do is a control command on a JSON-lines
socket. `exsim` is a client of it, the web board is another, and a scenario is a
third. `exsim call <name> '<json>'` reaches any command, including ones without
bespoke CLI wiring, and `exsim commands` lists them.

```
venue.info  markets  state.set  state.clear  state.get  stp.set
instruments  instrument.set  instrument.add  instrument.remove  reference.reload
book  ladder  bbo  trades  stats  stats.reset
orders  order.new  order.get  order.cancel  orders.cancel_all
behaviour.set  behaviour.list  behaviour.clear
sessions  session.reset  session.kill
audit  audit.entry  audit.types
venue.assumptions                       (both venues)
smp  smp.register  smp.clear  auction  auction.lock  auction.reference  segments   (HKEX)
```

`order.new` is **not** a second order path: it builds the same request a FIX
gateway builds and hands it to the same engine, so band, tick, lot and
market-state checks all apply identically, and an injected order crossing a
resting FIX order sends that client a correct execution report. It is also the
injected-liquidity seam — a script can maintain a book for a client under test
to trade against.

```bash
exsim call order.new '{"market":"DAY","symbol":"7203","side":"BUY",
                       "quantity":100,"price":"2845.5","tif":"DAY","owner":"BOT1"}'
exsim orders --owner BOT1
```

Injected orders carry an `owner` rather than a FIX session, which is what keeps
them addressable and stops the gateway from trying to report to a session that
does not exist. Their events publish on `order:<market>:<owner>`.

## The web board

`exchangesim.web.main` is a **separate process** that connects to each venue's
control port as a client, so the one-process-per-exchange rule is untouched and
nothing the board does can stall a matching engine. It lists its venues in
`config/web.json`, redials any that are down, and serves:

```
GET  /                          the board
GET  /api/venues                configured venues and their connection state
POST /api/<venue>/<command>     any control command, JSON body in and out
GET  /api/<venue>/events        Server-Sent Events, ?topics=trade:*,book:*
```

Every control command is therefore reachable from the browser without
per-command web code — a command added for the CLI is available there the same
day, and the venue remains the only thing that validates it.

Three capability switches gate what a board may do, each enforced server-side
rather than merely hidden:

```json
{ "allow_order_entry": true, "allow_market_control": true, "allow_audit": true }
```

They are separate because they are different powers. Closing a market expires
every resting order on it, other clients' included; the audit shows every
client's traffic whether or not this board may trade.

It binds loopback by default. Where a venue requires a control token, that token
goes in `config/web.json` and stays in that process — the browser never sees it.
Mutating requests must carry `Content-Type: application/json`, which a
cross-site HTML form cannot send.

The depth ladder is built by the venue rather than by the page because **tick
size varies with price**: a client stepping a single remembered tick would
misalign every row the moment it crossed a threshold in the instrument's tick
table, and could not tell. `exsim ladder` renders the same thing in a terminal.

The page is dark or light according to `prefers-color-scheme`, which is one
`@media` block of token values in `static/style.css` because every colour in
that file is a custom property and none is written inline. Dark is what a
browser stating no preference gets. The Japanese convention holds in both: the
`--bid`/`--ask` tokens are red for buying and green for selling, darkened in the
light palette until they carry on white.

## The message audit

`exchangesim/audit.py` holds a bounded ring of everything the simulator said and
was told, under one sequence number so it reads as a single ordered stream:

- **FIX**, both directions, on every session — administrative traffic included,
  messages the dialect rejected, and messages that could not be parsed at all.
- **Control commands** that changed something. Queries are deliberately not
  recorded: the board asks for the ladder, the BBO and the statistics whenever
  the market moves, and recording those would drown the tape.

Field names and value meanings come from the venue's own dictionary — the same
constants it validates against — so `39=1` reads `PARTIALLY_FILLED` because that
is what the dialect calls it, and the two cannot drift apart. A field the
dialect marks as a credential, such as HKEX's `EncryptedPassword(1402)`, is
withheld from the field dump *and* from the wire string, which is rebuilt from
fields rather than echoed from the raw bytes.

A command is recorded *before* it runs and completed when it answers, so it
appears above the execution reports it caused rather than below them.

Three commands read it: `audit` (the filtered list), `audit.entry` (one entry,
field by field, with the wire) and `audit.types` (the filter vocabulary, built
from the venue's dialect). The live tail passes `after=<seq>` so each poll costs
only what is new, and reports `truncated` rather than silently skipping entries
that were overwritten while a reader was paused.

`types` selects message types; `exclude_types` hides them, and hiding wins where
both name the same type. The exclusion is what drops heartbeats, and it is
applied by the venue rather than by the reader so that an idle session's traffic
does not consume the page of entries that was asked for. The tail's cursor
advances past excluded entries all the same, so a filter that hides recent
traffic does not make every poll rescan it. The board's **no HB** checkbox and
`exsim audit -x 0` are the two front ends to it.

## Scenarios

A scenario is a JSON file of steps run against a live simulator over real
sockets. Field maps are raw FIX tags on purpose: a scenario is a statement about
what goes on the wire, and a friendlier vocabulary would hide exactly the
details these tests exist to pin down.

```json
{
  "name": "IOC cancels its remainder",
  "sessions": {"buyer": {"comp_id": "CLIENT1"}},
  "steps": [
    {"control": "state.set", "args": {"market": "DAY", "state": "OPEN"}},
    {"logon": "buyer", "reset": true},
    {"send": "buyer", "fields": {"35": "D", "11": "B1", "55": "7203",
                                 "54": "1", "38": "300", "40": "2",
                                 "44": "2846.0", "59": "3"}},
    {"expect": "buyer", "fields": {"35": "8", "150": "4", "39": "4"}}
  ]
}
```

Step kinds: `control`, `logon`, `logout`, `disconnect`, `send`, `expect`,
`expect_none`, `clear`, `sleep`. A `control` step may assert with
`expect_result` (exact values in the reply), `expect_contains` (some element of
a list in the reply matches) or `expect_error` (the command is refused with a
given code). The runner exits non-zero on the first mismatch and prints what did
arrive.

A scenario may also name the venue and dialect it runs against, so that one
invocation covers every simulator a CI job started:

```json
{
  "begin_string": "FIXT.1.1",
  "fix_port": 9012,
  "control_port": 9102,
  "server_comp_id": "HKEXSIM",
  "logon_reset_flag": false,
  "logon_fields": {"789": "1", "1137": "9", "1400": "101", "1402": "..."}
}
```

`protocol` names the encoding, per scenario or per session — `"binary"` puts a
scripted client on the binary port, and the steps stay written in FIX tags
either way, because that is what the codec turns them into. `logon_fields`
carries whatever the dialect adds to Logon.
`logon_reset_flag: false` is for a venue that refuses a client-initiated
sequence reset, as HKEX does — the scenario restarts its own numbering and asks
the venue to do the same with `session.reset`.

Scenarios share one process per venue, so give each distinct ClOrdIDs — the
order registry remembers every identifier for the lifetime of the process — and
bracket each with `orders.cancel_all`.

## Testing a client's unhappy paths

The behaviours hardest to provoke against a real venue are the ones worth
testing most:

```bash
# Reject the next order regardless of its validity
exsim call behaviour.set '{"action":"reject","count":1,"reason":"OTHER"}'

# Hold the next report for 2 seconds, so timeout logic runs
exsim call behaviour.set '{"action":"delay","count":1,"delay_ms":2000}'

# Drop the next report entirely
exsim call behaviour.set '{"action":"drop","count":1,"symbol":"7203"}'

exsim call behaviour.list
exsim call behaviour.clear
```

Sequence recovery is exercised the same way: `session.kill` drops a connection
under a client, and the messages sent while it was away are still numbered and
persisted, so the client discovers the gap at its next Logon and retrieves them
by resend. That is also what makes Cancel on Disconnect observable.

Injecting PendingNew / PendingCancel reports is deliberately **not** supported:
the Japannext `ExecType(150)` enumeration has no pending value, so such a report
is not something a conformant client could receive from this venue.

## Deployment

```bash
sudo ./deploy/install.sh                 # to /opt/exchangesim
systemctl enable --now exsimd@japannext
```

The unit is templated on the config name, so another exchange is another config
file plus another `systemctl enable`. `ExecStartPre` runs `--check` so a broken
config fails immediately instead of restart-looping.

`--check` returns before the venue's setup, so it does not exercise reference
data loading or port binding; to validate those, start the process.

## Development

```bash
python -m unittest discover -s tests -t .          # 1,092 tests, a few seconds
python -m unittest tests.test_matching             # one module
python -m exchangesim.scenario.runner "scenarios/*.json"   # needs venues running
```

Tests use `unittest` only. The reactor is stepped synchronously, so there are no
sleeps and no timing flakiness; clocks are injected and frozen. There are four
harness layers — core only, the FIX session layer against a fake transport, a
full venue with fake sockets, and the reactor over real loopback sockets — and
the rule is to pick the narrowest that fits.

**A green unit suite is not sufficient evidence.** Three bugs survived 500+
passing tests and were found only by starting the daemon and driving it with a
real client. Before calling work done, run the venue and use it.

Python 3.6 has no `dataclasses`, no `datetime.fromisoformat`, no
`asyncio.run`/`create_task` and no walrus operator. It does have f-strings, PEP
526 annotations and insertion-ordered dicts. Development happens on Windows
against a 3.6.8 virtualenv, which is the same version as the RHEL 8 target, so
local results are representative.

Concurrency is a single-threaded `selectors` reactor rather than `asyncio`,
chosen for deterministic I/O ordering — reproducible matching in CI — and
because it can be stepped synchronously from a test.

## Sources

Every venue behaviour is transcribed from that exchange's public specification.
The PDFs are not committed — they are large binaries the venues publish — but
`docs/specs/README.md` says where each one lives and what it was used for.

Japannext, indexed at <https://www.japannext.co.jp/en/support>:

- `JNX_FIX_Trading_Specification_Equities_3.00` — messages, tags, enumerations,
  field lengths, reject reasons
- `JNX_Trading_Rules_Equities_2.02` — matching rules, order restrictions, price
  bands, tick sizes, trading hours
- `JNX_Self-Trade_Prevention_2.00` — the three STP modes, reproduced as tests
  from the document's own worked examples

HKEX, from <https://www.hkex.com.hk> → Services → Trading → Securities:

- `HKEX_OCGC_FIX_Trading_Protocol_3.12` — messages, repeating groups, session
  establishment, self-match prevention, reject codes
- **Rules of the Exchange, Second Schedule, Part A** — the spread table, which is
  both the tick ladder and the unit the quotation rule counts in
- *Trading Mechanism of the CAS in the Securities Market* — the reference price,
  the two-stage price limits, and the five IEP determination rules

`tools/pdftext.py` extracts text from these PDFs using only the standard
library, since reading them is a recurring need and the project takes no
dependencies.

A protocol specification defines codes, not trading rules. The spread table, the
9-times rule and the quotation rule all come from the Rules of the Exchange, and
transcribing them from the FIX protocol document instead is how three wrong
rules once survived a green test suite.

## Known gaps

Where a specification is silent, the choice is marked `ASSUMPTION` in code and
reported by `exsim assumptions` rather than left buried. Confirm these against
the real test environment.

**Japannext**

- post-only that would cross: rejected with `OrdRejReason=11` vs. cancelled
- tick-size violation reject code
- amend priority rules
- throttle-breach behaviour
- **tick-size tables**: the shipped `reference/tick_sizes_*.csv` ladders are
  monotonic and use only values that appear in the Trading Rules appendices, but
  those appendices are merged-cell tables that text extraction cannot
  reconstruct unambiguously. Correcting the CSV is the entire fix — nothing
  hard-codes ticks.

Price band tables, by contrast, are exact.

**HKEX** — the venue implements board-lot continuous trading plus the POS and
CAS auctions. Deliberately not built, and rejected rather than faked: the
odd/special lot book and its trade-request flow, quotes, trade capture, drop
copy, and the Volatility Control Mechanism. Both published encodings of the
protocol are served, and both answer a Party Entitlement Request (35=CU /
type 27); in the binary one the Lookup service (message types 7 and 8),
on-behalf-of cancels (23, 24) and the throttle queries (25, 26) are unbuilt, and a client sending one gets a Reject naming an
invalid message type rather than a half-answer. Beyond that:

- **auction periods have no timings.** They are driven by `state.set` and
  `auction.lock` rather than by the published schedule, so a test never waits and
  the random closing period cannot make an outcome depend on the clock.
- the **CAS reference price** is the nominal price when the session opens, not
  the median of five snapshots taken over the last minute of continuous trading
  — the median needs a minute of wall clock the simulator does not spend.
  `auction.reference` sets it explicitly.
- `EncryptedPassword(1402)` is required on Logon but **not verified**: it is RSA
  encrypted under the venue's public key, and a simulator holds no private key.
  The binary Logon's `Password` field is treated the same way.
- in the **binary encoding**, three details are read rather than stated: the
  `Gap Fill` value list is a graphic the specification's text layer does not
  carry (taken as `Y`/`N`, which its Byte type supports and its numeric flags
  contradict); `Disclosure Instructions` bit 0 is read as the least significant
  bit of the word; and the Execution Report refusing a cancel for an **unknown**
  order carries no instrument or side, there being no order to describe.
- an unfilled IOC, FOK or market-order balance reports `ExecType=C` (Expired)
  rather than 4 (Cancelled); the specification defines both and says which
  applies to neither.
- a limit price may reach **9 spreads** through the opposite best, which is the
  *enhanced* limit order allowance. OCG-C distinguishes a plain limit order by
  `MaxPriceLevels(1090)=1`, which this venue accepts and ignores, so it never
  refuses an order the real venue would take.

The spread table and both price rules were checked against the published Rules
of the Exchange rather than assumed; `exsim assumptions` lists what remains.
