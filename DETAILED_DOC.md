# ExchangeSim — detailed documentation

Everything beyond getting it running: how it is built, how the four venues
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
- [Process control](#process-control)
- [Deployment](#deployment)
- [Development](#development)
- [Sources](#sources)
- [Known gaps](#known-gaps)

---

## What it does

| Capability | Detail |
|---|---|
| Order entry | New, amend (price and quantity), cancel, and mass cancel where the venue has it |
| Order types | Whatever the venue supports: limit only at Japannext, limit and market at HKEX and NSE; TIF Day / IOC / FOK, post-only, IOC MinQty. At NSE the type and the time in force are bits of a flag word, not fields |
| Matching | Order-driven continuous, price/time priority, executed at the **resting** order's price |
| Auctions | Call auctions that uncross at one price. Which tie-breaks apply is the venue's: HKEX uses five rules, NSE four — its pre-open has no surplus-direction rule |
| Crossing | Full book matching plus self-trade prevention, keyed on the MPID (Japannext) or on a client-supplied ID (HKEX). NSE has none, and refuses a market configured with any mode |
| Markets | Japannext: four, addressed by `TargetSubID`. HKEX: one book per market segment, chosen by the security. NSE: one, the Normal market |
| Market states | Pre-open, auctions, lunch break, open, closed, halted — venue-wide, per market, or per instrument |
| Market data | Book, depth, BBO, trade tape and session statistics via CLI, with a live monitor |
| Web board | Browser view over every running venue, live over Server-Sent Events |
| Message audit | Every message in and out, and every mutating command, with each field named and each value explained |
| Recovery | Persistent sequence numbers, ResendRequest, SequenceReset-GapFill, Cancel on Disconnect — at the two FIX venues. NSE has no resend at all: recovery is the message download, which replays what a user was sent after a cursor |
| Encryption | None at the FIX venues. NSE runs AES-256-GCM on every message, written by hand because the standard library has no cipher |
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
                (4.2 and FIXT.1.1), persistent store, session table, field naming
  binary/       the second encoding OCG-C publishes: frame, data types,
                presence map, and the codec that turns one into a fix.Message
  nnf/          NSE's native protocol: big-endian data types, the Chapter 10
                packet, AES-256-GCM by hand, fixed-offset structures, and a
                session layer in which a connection is a box carrying many users
  wire/         what every protocol here shares: the acceptor that binds a
                connection to a session, and value conversion
  tls/          TLS on a MemoryBIO for the reactor's transport seam, and the
                DER/RSA/X.509 that issue the certificate it serves
  venues/
    common_commands.py  every venue-agnostic control command
    japannext/  the Japannext dialect, rules, handlers and reference data
    hkex/       the HKEX OCG-C dialect, likewise
    nse/        the NSE Capital Market dialect, its structures, its circuit
                filters and its pre-open
    nsefo/      the NSE Futures & Options dialect: the same protocol, its own
                structures under the same transaction codes
  web/          the browser board: its own process, a client of the venues
  control/      JSON-lines control server and command registry
  cli/          the exsim command-line client
  scenario/     declarative scenario runner
  runner/       process entry point
bin/            the exchangesim command
config/         one JSON file per venue instance, the web board's, services.json
scenarios/      example scenarios; the CI suite
var/            runtime state: logs, pidfiles, FIX sequence stores
tools/          pdftext.py, a stdlib PDF text extractor for reading venue specs
tests/          1,749 tests, unittest only
```

Three layers with a hard dependency rule — arrows point inwards only, and
`core/` may not import from `venues/` or from any protocol gateway:

```
  Wire format            fix/ (tag=value)   binary/ (OCG-C)   nnf/ (NSE)
      |                  all three produce the same fix.Message
  ---------------------------------------------
  Gateway                venue-specific dialect and mapping
      |  commands v          ^ events
  ---------------------------------------------
  Venue module           validation rules, reference data
      |
  ---------------------------------------------
  Venue-agnostic core    book, matching, lifecycle, state, market data
```

Gateways turn wire messages into core commands and core events back into wire
messages. The core never builds a protocol field; the gateway never touches the
book. Adding an exchange is a new package under `venues/` plus one line in the
registry — and it inherits the whole control surface, the CLI and the web board
without further work. Adding a *protocol* is a package beside `fix/` that
implements the same six-method codec seam, which is what NSE needed and HKEX's
second encoding did not.

**How well that held for the second venue.** HKEX needed four core additions,
and each is a capability the core was missing rather than a Hong Kong special
case: market orders, self-match prevention keyed on the order alongside the
existing per-market mode, an "ignore price check" execution instruction, and a
mass-cancel reason. The session layer gained four options for FIXT.1.1, all
defaulting off, so FIX 4.2 behaviour is byte-identical. Nothing in `core/` knows
the word "HKEX".

### Where the four venues differ

| | Japannext | HKEX | NSE Cash | NSE F&O |
|---|---|---|---|---|
| Protocol | FIX 4.2 | FIX 5.0 SP2 over FIXT.1.1, in two encodings | NNF — not FIX in any encoding | the same NNF, its own structures under the same transaction codes |
| Session | ResendRequest recovery, client may reset at Logon | `NextExpectedMsgSeqNum(789)` negotiation, client reset **refused** | no sequence numbers, no resend, no session-level Reject | the same |
| A connection | is a session | is a session | is a **box**, carrying many signed-on users | the same |
| Order handle | `ClOrdID(11)` | `ClOrdID(11)` | **none** — the exchange's `OrderNumber` | the same |
| Instrument | `Symbol(55)` | `SecurityID(48)`; `Symbol` is not in the dialect at all | Symbol **and** Series together | `CONTRACT_DESC` — five fields; a future's strike is **-1** |
| Market routing | `TargetSubID(57)` per message | the security's segment — no message names a market | one market; the book type is checked and every book but Regular Lot refused | the same; book 3 conflates Stop Loss with MIT |
| Price limits | one band table around the nominal price | two rules: the 9-times rule and the quotation rule (see below) | a per-security circuit filter, as a percentage of the previous close | the same, per contract |
| Order type / TIF | `OrdType(40)`, `TimeInForce(59)` | the same | **bits** of `ST_ORDER_FLAGS` |
| Auctions | none — the rules say so outright | POS and CAS, five tie-break rules | the pre-open, four tie-break rules |
| Self-trade prevention | per-market mode, keyed on MPID | per-order `SelfMatchPreventionID(2362)` | none |
| Groups | none | `<Parties>` and `<DisclosureInstructionGrp>` on every business message | none — every field is always on the wire |
| Acknowledgement | only for an order that reaches the book untraded | **before** matching | **before** matching, and it is the only place the order number appears |
| Rejection | `OrdRejReason(103)` | its own reject codes | a numeric `ErrorCode` on the erroring form of the same transaction |
| Encryption | none | none | AES-256-GCM on every message |

### Acknowledgement before execution

The venues answer a question the FIX standard leaves open: when an
aggressive order trades the moment it arrives, is it also reported *accepted*?

Japannext's Order Accepted report describes an order that reached the book
untraded — `CumQty` 0, `LeavesQty` equal to `OrderQty` — so an order that filled
on entry would have to be described twice and is not: its execution report
already carries the resting quantity.

HKEX answers the other way, and its message flows are explicit (FIX 3.12
sections 6.6.1 and 6.13.1, Binary 3.2 section 6.14.1): a New Order that trades
immediately is answered with `ExecType=New, OrdStatus=New, CumQty=0,
LeavesQty=OrderQty`, and only then with its fills. It matters most for an order
that never rests. Without the acknowledgement, a client whose IOC finds no
liquidity hears of the order for the first time in the report that expires it —
an Execution Report naming an `OrderID` it has never been given, which is
exactly what a real gateway rejected as unrecognised.

So it is a matching-engine setting, `MatchingEngine.ack_on_entry`, off by
default and switched on per market — `rules.ACK_BEFORE_EXECUTION` for HKEX.
Two consequences are worth naming:

* **One acceptance per order, never two.** With the flag on, the acceptance is
  emitted on entry and `_rest` does not repeat it for whatever remains; with it
  off, `_rest` is the only place that emits one. An amendment that re-enters the
  book filters it out either way — the order was acknowledged when it arrived.
* **`OrderAccepted` snapshots its totals**, the same treatment `OrderFilled`
  needs and for the same reason: the acceptance is produced ahead of the fills
  in one batch and rendered after them, by which time the order has traded.

A rejected order is not acknowledged: the report means the market accepted the
order, not that the gateway received it.

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

### The message download

NNF's whole recovery mechanism, and the thing that stands where a FIX
`ResendRequest` would. A report produced while a user was signed off is
dropped rather than queued — there is nothing to queue it for — so the
client gets it back by asking: `DOWNLOAD_REQUEST (7000)` names a stream and a
cursor, and the venue answers `HEADER_RECORD (7011)`, a `MESSAGE_RECORD (7021)`
per message after that cursor, and `TRAILER_RECORD (7031)`.

Five things here are load-bearing.

- **The cursor is the header's own `TimeStamp1`**, in jiffies (1 second =
  65536). A client does not count messages: it remembers the last stamp it saw
  and asks for everything after, and zero means the whole trading day. That
  makes the field load-bearing rather than decorative, and it was eight zero
  bytes on every message until the download was built. Its epoch is an
  ASSUMPTION — the document gives the unit and never the origin — and 1980 is
  used because the header's sibling nanosecond `Timestamp` says so, which keeps
  one capture from holding two epochs.
- **The inner header of a record is the ordinary `MESSAGE_HEADER`, not
  `INNER_MESSAGE_HEADER`.** The document says the opposite in so many words (CM
  6.6 p.44: "For inner Header Refer Table 2"); the direct interface does not
  follow it, and a real client confirmed which it reads. The two hold the same
  nine fields with the first twelve bytes permuted — transaction code at offset
  0 against trader id at offset 0 — so the wrong one does not fail cleanly.
- **A recovered message is always the non-trimmed form.** F&O serves order
  entry in two encodings, and a `_TR` structure has no forty-byte header at
  all, so it could never be wrapped in a record — which is why `MessageStore`
  keeps the `Message` rather than the encoded frame, and takes a `normalise`
  hook (`rules.untrimmed`) that files the plain twin. That hook reads the
  error code rather than mapping the transaction code blindly, because one
  trimmed code recovers as either a confirmation or an `ORDER_ERROR`. Keeping
  the `Message` is also what keeps ciphertext out of the store.
- **A record is the one structure in this protocol with no published length.**
  `nnf/layout.py:RecordLayout` is the one class that knows it: `check()` has
  nothing to check, and `MessageLength` — "the length of the entire message" —
  is for once not a constant, and is the only way a client finds the record's
  end.
- **The download's own three codes are never stored.** A second download would
  otherwise return the first one wrapped in a third, without bound;
  `rules.is_recoverable` is where a venue says so.

Streams are the other half. `TimeStamp2` carries the machine number a message
came from (its eighth byte, which is the low byte of a big-endian `LONG LONG`,
so the number itself is what goes in), and `SYSTEM_INFORMATION_OUT`'s
`AlphaChar` carries the count — as a *byte*, not a digit — which the client
loops its download over. This simulator is one machine and serves one stream
(`nnf.stream`); asking for another gets an empty download rather than a
refusal, because refusing would stop that loop.

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
exsim --port 9102 call auction '{"market":"MAIN","symbol":"700"}'
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

`counterparty` decides what the Counterparty Broker ID on a trade report says:

```json
{ "counterparty": { "default": "8888", "override": null } }
```

A real match names the broker that actually traded. `default` is what to name
when the other side has none -- an order injected over the control plane names
no Exchange Participant -- so with it set, every fill carries a counterparty.
`override` displaces the real one on every trade, which is for a client under
test that reconciles against one expected counterparty and should not have to
care which session was resting opposite. Both null reports only who really
traded, and omits the field when that is nobody. A Broker ID longer than the
binary encoding's field is refused at startup rather than reaching a binary
client truncated and a FIX client whole.

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
venue.assumptions                       (every venue)
smp  smp.register  smp.clear  auction  auction.lock  auction.reference  segments   (HKEX)
boxes  box.kill  gateway_router  preopen  preopen.lock  transactions               (NSE)
```

At NSE, `sessions` and `boxes` are two different populations and both are worth
having: a *session* is a signed-on user, which is what an order's owner and a
report's recipient mean, and a *box* is the connection several of them share.
`box.kill` signs off every user on one, as the specification requires.

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
browser stating no preference gets.

A fourth key, `board`, says how the page is laid out and coloured. It grants
nothing — it is presentation — but it is served from the config rather than
chosen per browser, because two people describing one screen the other way
round is a real hazard on a desk.

```json
{ "board": { "buy_side": "right", "buy_colour": "red", "sell_colour": "green" } }
```

**`buy_side` moves three things at once**: the ticket's Buy button, Level 1's
bid quote and the ladder's bid column. They are never split. A ticket that
disagreed with the ladder above it is a click waiting to go the wrong way, so
one setting places all three, `right` by default — where a Japanese depth board
puts bids. Two of the three are CSS `order` off a `data-buy-side` attribute on
`<html>`; the ladder is the exception, because table cells cannot be reordered
by `order`, so `app.js:ladderCells` is the single place that writes them in
sequence and the static `<thead>` is reversed once at boot.

**A colour is a name, not a value.** `style.css` defines `--hue-red`,
`--hue-green`, `--hue-blue` and `--hue-amber` in both palettes, and `--bid` and
`--ask` are *aliases* onto two of them rather than literals — which is what lets
the page repoint a side at boot while keeping that hue's light-theme version.
The server validates the name against the same four, because a free-text colour
would let a config write a board nobody can read: every hue is checked against
the 4.5:1 floor, in both themes, on every surface and on its own wash, by
`tests/test_web_palette.py`. The default pair is the Japanese convention — red
for buying, green for selling, the reverse of the Western one — and the two
sides may not wear the same hue.

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

## Process control

A working simulator is several processes -- one per venue, plus the board --
and `exchangesim/ctl/` is the single front end to them:

```bash
exchangesim start | stop | restart | status | logs | check
```

What it starts is `config/services.json`: a name, a module and a config file
per service, in the order they must come up. It holds no ports. Each service's
ports are read from that service's own config when they are needed, so a port
lives in exactly one place and moving one cannot leave the listing stale.

It is deliberately **not** a service manager. It does not restart a crashed
process. Whatever supervises processes on a given host can do that, and two
supervisors fighting over one daemon is worse than none. What it replaces is
the handful of backgrounded commands and the `kill` that follows them.

Four decisions carry the weight here.

**The state of the world is a pidfile per service**, under `var/run`. It has to
survive the controller exiting -- `start` returns to the prompt and the venues
stay up -- and a pidfile is the only record that does. It is never trusted on
its own: `supervisor.inspect` checks that the process exists and, on Linux,
that `/proc/<pid>/cmdline` still names our config, because a recycled pid would
otherwise be reported as a running venue and `stop` would kill a stranger.

**`start` waits for the port, not for a timer.** A venue is ready when its
control port accepts a connection, which is after reference data has loaded and
the acceptors have bound. That is why `make smoke` no longer sleeps three
seconds and hopes: on a loaded build agent the sleep was either too short or
wasted. Services are started one at a time in declaration order, so the board
never comes up before the venues it dials, and stopped in reverse.

**Each daemon rotates its own log.** `--log-file var/log/<name>.log` plus
`log.max_bytes` and `log.backups` give the process a `RotatingFileHandler`, so
the file is capped by the only writer that holds it -- nothing outside can
catch it holding a renamed file, and there is no cron job or `logrotate` config
to be missing on a host. Five files of 5 MB by default.

**A supervised daemon is started with `--no-console`, and that is not
cosmetic.** Its stdout and stderr are captured to a *second* file,
`var/log/<name>.out`, for what happens outside logging: an import error, a
traceback on the way down, a message from the interpreter itself. Leaving the
stderr handler on would write every line into both files and rotate only one of
them. In normal operation the `.out` file stays empty, which makes a non-empty
one worth reading first -- `exchangesim logs <name> --out` shows it.

Two platform notes. On POSIX a child gets its own session (`os.setsid`), so
closing the terminal does not take the venue with it, and `stop` is a SIGTERM
the daemons handle. Windows has no SIGTERM: children are started in their own
process group and sent a console `CTRL_BREAK_EVENT`, which the daemons handle
as SIGBREAK -- but console control events only reach processes sharing the
sender's console, so a `stop` from a different terminal falls back to
terminating outright. That is survivable rather than graceful, and it is
survivable because the FIX sequence store is written and fsynced on every
update, never at shutdown.

## Deployment

There is no installer, no service account and nothing that touches the system:
a deployment is a clone of this repository and a Python interpreter.

```bash
git clone <this repository> exchangesim
cd exchangesim
bin/exchangesim start
```

Everything it writes stays inside the tree -- `var/log` for logs, `var/run` for
pidfiles, `var/<venue>` for FIX sequence stores -- so the account that runs it
needs nothing but write access to its own checkout, and removing it is `rm -rf`.
Nothing is registered with the OS, so nothing has to be unregistered.

On RHEL 8 the interpreter is `/usr/libexec/platform-python` (3.6.8), which is
present by default; `bin/exchangesim` finds it. Set `EXSIM_PYTHON` to override.
There are no dependencies to install, at build time or run time.

Keeping it running across a reboot or a crash is deliberately out of scope --
`ctl/` is a supervisor of last resort, not a service manager (see [Process
control](#process-control)). If a host needs that, whatever already supervises
processes there can run `bin/exchangesim start`; that is a decision about the
host, not about the simulator, and this repository does not make it.

`--check` returns before the venue's setup, so it does not exercise reference
data loading or port binding; to validate those, start the process.

## Development

```bash
python -m unittest discover -s tests -t .          # 1,273 tests, a few seconds
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

TLS rides that reactor through a seam rather than inside it: a connection runs
its bytes through a duck-typed transport whose default is the identity, so the
plain path is unchanged and `core/reactor.py` imports no `ssl`. The
implementation in `tls/` terminates TLS on a pair of `ssl.MemoryBIO`s instead of
an `SSLSocket`, because an `SSLSocket` buffers decrypted plaintext where
`select()` cannot see it — a reactor can then be told "not readable" while a
whole message sits undelivered. `tls/` also issues its own certificates, in
pure Python, for the same reason `nnf/crypto.py` writes its own AES: the
standard library ships `ssl` but nothing that can write an X.509 certificate,
and this project takes no dependencies. The DER is write-only; `tls/certs.py`
decides when to reissue from metadata it keeps beside the PEMs.

Because the deployment target's OpenSSL cannot always be assumed, development
uses **two** interpreters: `venv36` is the target and `venv314` exists only so
the TLS 1.3 path is exercised somewhere. Nothing requires 3.14 at run time, and
a test that cannot run on one of them skips with a reason naming the OpenSSL
version rather than passing silently.

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

NSE India, from <https://www.nseindia.com/static/trade/platform-services-neat-trading-system-protocols>:

- `TP_CM_Trimmed_NNF_PROTOCOL 6.6` — everything: the transaction codes and
  structures, the Chapter 10 packet, the two encryption methodologies, the
  order-flag bitfield and the whole error-code table. It is unusually complete
  for a venue document, publishing the bitfields **twice**, once per byte order,
  so the bit numbering here is a transcription rather than the assumption it had
  to be at HKEX.
- NSE circulars on the pre-open session — the equilibrium-price rule chain,
  which the protocol document does not define
- NSE circulars on tick size and the operating range — the five-paise tick and
  the per-scrip circuit filter

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
- the **Counterparty Broker ID** on a trade — `PartyRole=17` in FIX, bit 31 in
  the binary Execution Report — is sent whenever there is a broker to name.
  Both documents mark the field not required and the FIX one says only
  "provided only if applicable" without defining when; Hong Kong is
  broker-transparent, so the reading here is that it applies to every match
  with a counterparty broker, auction fills included. An order injected over
  the control plane has no Broker ID; `counterparty.default` names the broker
  to report in its place, so a fill need never carry a hole, and
  `counterparty.override` reports one fixed broker on every trade for a client
  that reconciles against a single expected counterparty.
- a limit price may reach **9 spreads** through the opposite best, which is the
  *enhanced* limit order allowance. OCG-C distinguishes a plain limit order by
  `MaxPriceLevels(1090)=1`, which this venue accepts and ignores, so it never
  refuses an order the real venue would take.

**NSE** — the venue implements the Regular Lot book of the Normal market and
the pre-open call auction, over the NNF Trimmed Protocol. Deliberately not
built, and refused with a published error code rather than faked:

- **every other book**: Special Terms, Stop Loss, Odd Lot, Spot, Auction and
  Call Auction 2, each answered `ERR_INVALID_BOOK_TYPE (16422)`. An odd lot is
  not promoted to a board lot, for the reason it is not at HKEX either.
- **All Or None** (16319) and **minimum fill** (16320), **disclosed quantity**
  (16400) and **Good Till Cancelled / Good Till Date** (16326). Disclosed
  quantity in particular: the core has no replenishment concept, and accepting
  the field while ignoring it would be the worst of both.
- **the broadcast market-data feed**, which is UDP multicast and LZO-compressed.
  LZO cannot be written under the standard-library-only constraint. Market data
  reaches a person through the control plane, the CLI and the board instead.
- **trade modification and cancellation**, the freeze and approval flow, and
  market-wide index circuit breakers.

**Spread orders trade on a book of their own.** F&O's spread is a calendar
spread — "a combination of two normal orders on two contracts with same
symbol and different expiry dates" — and it is quoted at `PriceDiff`, the gap
between the legs. Modelled as an instrument, it needs no matching of its own:
price-time priority on a difference is still price-time priority. The core
gained exactly one thing for it, `Instrument.signed_price`, because a spread's
price may be zero or negative and "a price must be positive" is a rule about
levels. Valid pairs are read from `reference/spreads.csv`, the Spread
Combination file; two listed futures are not automatically a spread. One match
is reported as **two** `TRADE_CONFIRMATION`s, one per leg, and only the
difference between their prices is exact — the levels are an ASSUMPTION the
document leaves to trading rules. Two-leg and three-leg orders share the
480-byte structure and nothing else, and are refused by transaction code.

**Order entry is served in both published encodings.** The plain 316-byte
`MS_OE_REQUEST` and the compact 158-byte `MS_OE_REQUEST_TR`, which carries no
forty-byte header at all — and which is what a real gateway sends, Chapter 11
saying "Only Trim-NNF protocol is supported by Direct Interface". A client is
answered in whichever it asked in, and an unsolicited report follows the
encoding its *order* arrived in. The one gap the appendix leaves is a trimmed
`ORDER_ERROR`: there is none, so a refusal is the confirmation code carrying a
non-zero `ErrorCode` — an ASSUMPTION, in `rules.TRIMMED_RESPONSES`. Chapter 15's
immediate-acknowledgement codes share these structures but live on a separate
Gateway Router port, and are unbuilt.

And three things that are built but not to the letter:

- **the Gateway Router's certificate is the simulator's own.** The leg *is* TLS
  now, defaulting to 1.3 as the specification requires, and the published
  methodology is a self-signed CA the exchange issues and distributes — so a
  self-signed CA is the right shape. What cannot be reproduced is the
  exchange's actual CA, so a member must point its client at the generated
  `var/tls/gr_ca_cert1.pem` rather than the file it was given by NSE. Note that
  the venue **refuses to start** where `gateway_router.tls` asks for 1.3 and the
  interpreter's OpenSSL cannot do it (RHEL 8 can; a Windows build against
  OpenSSL 1.0.2 cannot), rather than quietly serving something weaker. `"1.2"`
  is a development value and a *floor*: a real client pins minimum and maximum
  both to 1.3 and will not connect to a 1.2-only server.
- **the sign-on password is not verified.** A simulator holds no credential
  store, so `SIGN_ON_REQUEST_IN` is accepted on a configured User ID whatever
  password it carries — and the password is struck out of the audit either way.
- **order numbers are sequential from 1** rather than in NSE's own encoding,
  which the specification does not publish. They are unique, monotonic and fit
  the `DOUBLE` the wire carries, which is everything a client can rely on.

The rest is in `venues/nse/rules.py:ASSUMPTIONS`, reported by
`exsim --port 9103 call venue.assumptions`. The one that bit hardest against a
real client was the layout of the dynamic half of the cryptographic IV. Both
documents give it as a C `long long` inside a struct whose *address* is handed
to the cipher, so its bytes follow the member's host and not this protocol's
big-endian wire: it is written **little-endian**, which is the reverse of every
other multi-byte value here and is not a slip. What the documents still leave
open is whether a member byte-swaps that field out of the Gateway Router
response before storing it, so the exchange issues it as **zero** — the value
at which both readings coincide — and `gateway_router.dynamic_iv` defaults to
`"auto"`, settling the layout on whichever reading authenticates a box's first
message and pinning it for the connection. `"little"` and `"big"` pin it up
front instead. The direction the counter walks is in the same place; see the
`NewCipher` docstring for why the document's wording cannot be read literally.

`gateway_router.advertise_host` covers the other half of reaching a simulator
from another machine. The `IPAddress` in a GR response is a member's only
statement of where the trading gateway is, and a gateway bound to `0.0.0.0`
cannot answer with its own bound address — so the address the member reached
the router on is used instead, and `advertise_host` overrides both for a NAT or
a port forward.

The spread table and both price rules were checked against the published Rules
of the Exchange rather than assumed; `exsim assumptions` lists what remains.
