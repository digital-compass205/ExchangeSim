# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An exchange simulator that stands in for a venue's test environment in CI/CD. Two venues: Japannext PTS equities over FIX 4.2, and the HKEX securities market over OCG-C, covering board-lot continuous trading and the POS/CAS call auctions. HKEX publishes OCG-C in **two encodings of one protocol** -- FIX 5.0 SP2 on a FIXT.1.1 session, and a fixed-width binary format -- and both are served, on one set of books. See `README.md` for the user guide, `DETAILED_DOC.md` for architecture, venue rules and known gaps, and `docs/specs/README.md` for what each specification required.

**Hard constraints, both deliberate:**

- **Python 3.6.8, standard library only.** No third-party packages at build or run time. The target is RHEL 8's `/usr/libexec/platform-python`. No `dataclasses`, no `datetime.fromisoformat`, no `asyncio.run`/`create_task`, no walrus. f-strings, PEP 526 annotations and insertion-ordered dicts are fine.
- **Fidelity to the published specification beats convenience.** Venue behaviour is transcribed from the PDFs listed in `docs/specs/README.md`. Where a spec is silent, mark the choice `ASSUMPTION` in code and add it to `ASSUMPTIONS` in that venue's `rules.py` so `exsim assumptions` reports it. Do not quietly invent venue behaviour — and prefer refusing a flow outright to half-simulating it, which is why an HKEX odd-lot order is rejected rather than treated as a board lot.

## Commands

Development is on Windows against `venv36` (3.6.8, same version as the RHEL 8 target). There is **no `make` on the Windows box** — the `Makefile` is for the Linux target.

```bash
PY=./venv36/Scripts/python.exe            # on RHEL 8: /usr/libexec/platform-python

$PY -m unittest discover -s tests -t .    # full suite (~1090 tests, a few seconds)
$PY -m unittest tests.test_matching       # one module
$PY -m unittest tests.test_matching.SelfTradePreventionTest.test_cancel_newest_cancels_the_incoming_balance

$PY -m exchangesim.runner.main --config config/japannext.json --check   # validate config only
$PY -m exchangesim.runner.main --config config/japannext.json           # run a venue
$PY -m exchangesim.runner.main --config config/hkex.json                # FIX 9011, binary 9012, control 9102

$PY -m exchangesim.web.main --config config/web.json    # web board on :9200 (its own process)

$PY -m exchangesim.cli.exsim info                       # control CLI ($EXSIM_MARKET saves --market)
$PY -m exchangesim.cli.exsim --port 9102 audit          # every message and command recorded
$PY -m exchangesim.cli.exsim --port 9102 audit --seq 7  # one entry, field by field
$PY -m exchangesim.cli.exsim --port 9102 instruments    # same CLI, any venue
$PY -m exchangesim.scenario.runner "scenarios/*.json"   # end-to-end suite; needs *both* venues running

$PY tools/pdftext.py spec.pdf --grep OrdType            # read venue spec PDFs (stdlib only)
```

`--check` returns before `venue.setup()`, so it does **not** exercise reference-data loading or port binding. To validate those, actually start the process.

**Killing a daemon:** use PowerShell `Get-Process python | Stop-Process -Force`. Bash `kill` on a Windows PID has silently failed here and left daemons running.

## Architecture

Three layers with a hard dependency rule: **`core/` may not import from `venues/` or any protocol gateway.** Arrows point inwards only.

```
  FIX gateway (venues/japannext/handlers.py)   wire dialect
        | commands v            ^ events
  ------------------------------------------------------
  Venue module (venues/japannext/)             rules, reference data, mapping
        |
  ------------------------------------------------------
  Core (core/)                                 book, matching, lifecycle, state
```

Gateways turn wire messages into `core/commands.py` requests and `core/events.py` events back into wire messages. The core never builds a FIX field; the gateway never touches the book. Adding an exchange is a new package under `venues/` plus a line in `venues/registry.py`.

**How well that held for the second venue.** HKEX needed four core additions, and each one is a capability the core was missing rather than a Hong Kong special case: market orders in `matching.py`, self-match prevention keyed on the order (`Order.stp_id` / `stp_instruction`) alongside the existing per-market mode, `ExecInst.IGNORE_PRICE_CHECK`, and `CancelReason.MASS_CANCEL`. The session layer gained four `SessionConfig` options for FIXT.1.1 — all default off, so FIX 4.2 behaviour is byte-identical. Nothing in `core/` learned the word "HKEX". If a third venue needs a core change, that is the bar: does the core lack a *concept*, or are you smuggling in a dialect?

A new venue also inherits its whole control surface: `venues/common_commands.py` holds every venue-agnostic command (trading state, instruments, market data, orders, behaviour, sessions) and a venue's own `commands.py` calls `common_commands.register(registry, venue)` before adding what only it can provide. For Japannext that residue is one command, `venue.assumptions`; for HKEX it is that plus the SMP registry and `segments`. The test is mechanical: a command that needs `from .dictionary import ...` belongs to the venue, everything else is shared.

### Where the two venues differ, and why it matters

Reach for the Japannext module as a template only after checking these, because each is a place where copying it would be wrong.

| | Japannext | HKEX |
|---|---|---|
| Session | FIX 4.2, ResendRequest recovery, client may reset at Logon | FIXT.1.1, `NextExpectedMsgSeqNum(789)` negotiation, client reset **refused** (use `session.reset`) |
| Instrument | `Symbol(55)` | `SecurityID(48)`; `Symbol` is not in the dialect at all |
| Market routing | `TargetSubID(57)` per message | the security's `segment` column — no message names a market |
| Price limits | one band table around the nominal price | **two** rules: the multiplicative 9-times rule against the nominal price (`rules.NineTimesRule`, not a table), and the quotation rule against the live BBO (`StandardValidator._check_quotation`) |
| Auctions | none — the rules say so outright | POS and CAS, uncrossed by `core/auction.py` |
| Self-trade prevention | per-market mode, keyed on MPID | per-order `SelfMatchPreventionID(2362)`; the *instruction* is registered against the ID out of band, hence `venue.smp_instructions` |
| Groups | none | `<Parties>` and `<DisclosureInstructionGrp>` on every business message |
| Encodings | tag=value FIX | tag=value FIX **and** binary, one port each, one session table |

Repeating groups needed **no codec change**: `Message` keeps fields ordered and offers `get_all`/`append`, so a group is read positionally. What that cannot do is check the count, so `hkex/handlers.py:_check_count` does, rejecting a mismatch with `SessionRejectReason=16`. Any new group needs the same.

### The second encoding

HKEX publishes OCG-C twice, as tag=value FIX and as a fixed-width little-endian binary format, and entitles a Comp ID to one of them. The binary one is a **codec, not a second gateway**: `binary/` turns a frame into the same `fix.Message` everything above it speaks, so the session layer, dictionary validation, handlers, engine, audit, CLI and board are shared and neither encoding knows about the other.

The seam is four methods -- `framer`, `extract`/`decode`, `encode`, `is_admin` -- named by `fix/codec.py:FixCodec` and implemented again by `binary/codec.py:BinaryCodec`. `Session` and `Acceptor` hold one; nothing else in `fix/` touches the wire format. **If you find yourself branching on `session.wire` outside a gateway, the branch is in the wrong place** -- the one legitimate use is `hkex/handlers.py:_for_wire`, and the reason is below.

Five things here are load-bearing.

- **The one Comp ID on the wire is the client's, in both directions** (section 7.2). The codec supplies the venue's own identity as the missing half, so `_check_comp_ids` is unchanged; `BinaryCodec(..., client=True)` mirrors that for a client, which is what the test harness and the scenario runner use. A client that puts the *venue's* Comp ID in the header resolves to no session at all.
- **A binary message type without a layout decodes to MsgType `B<n>`**, which no dialect defines, so the dictionary refuses it as an invalid message type -- a clean Reject rather than a dropped session -- and `binary_type()` parses the number back so the Reject can name it. An unrecognised *value* likewise passes through as text for the dictionary to refuse. What is fatal is an unknown **bit position**: the field's width is unknown, so nothing after it can be parsed.
- **`dictionary.build_binary()` is a separate transcription, not a flag.** The two published tables disagree about required fields -- a Cancel Request has no OrderQty, `SecurityExchange` is optional, Logon has no EncryptMethod or HeartBtInt -- and each session is validated against the document its client was written from.
- **There is no OrderCancelReject in the binary encoding.** A refused cancel or amend is an Execution Report with `ExecType` `X`/`Y` carrying the order's identity and running totals, which 35=9 has no fields for. `_for_wire` rebuilds it in the gateway because those fields come from the *order*; a codec that reached for one would be a gateway.
- **Bit positions are per message type.** `Price` is bit 9 of a New Order, 11 of an Amend and 12 of an Execution Report. Every Execution Report variant in section 7.6.7 shares one assignment, though, so there is one layout and which fields it fills stays in the handlers.

`Audit` entries carry the `protocol` that recorded them and are read back with `venue.wire_codec(...)`; a binary entry renders as a hex dump with credentials struck out, so the bytes line up with a client's own log.

### Auctions

`core/auction.py` finds one price and executes everything at it; `venues/hkex/auctions.py` holds what HKEX wraps around that. The split is the usual one — the rule chain (maximum volume, lowest imbalance, surplus direction, closest to the reference price, higher of two) is the standard call auction and belongs to the core; the reference price, the two-stage bands and the carried-forward-order treatment are the venue's.

Three things here are easy to get wrong:

- **An at-auction order has no price, so it cannot go in the sorted ladder.** `BookSide` keeps a separate FIFO for them, and they are deliberately excluded from `best_price`, `depth` and `orders_in_priority` — the BBO and the ita board are statements about limit prices. `orders()` and `__len__` *do* include them, so cancel-all and the order count stay honest. `orders_in_priority` is the continuous-matching iterator: a priceless resting order has no price to execute at, so it must never appear there.
- **An auction price is only available when the limit books overlap.** A book holding nothing but at-auction orders has no price of its own, and HKEX then matches at the reference price instead — which is why `Market.uncross` falls back to it rather than treating "no IEP" as "no trade".
- **Uncrossing happens before the state change is applied.** `set_trading_state` calls `_close_auction` first, because moving to a closed state expires resting orders and a closing auction run afterwards would find an empty book.

Auction periods are **command-driven**, like every other phase here: `state.set` opens the session, `auction.lock` ends the input period, `state.set` ends it and uncrosses. Do not add a scheduler — a random closing period would make a test's outcome depend on the clock.

### The audit

`exchangesim/audit.py` is a bounded ring of everything the simulator said and was
told: FIX messages both ways, and control commands that changed something. It sits
at the top level rather than in `core/` (which stays protocol-agnostic) or `fix/`
(it also holds commands), and imports neither — what it is handed is duck-typed.

Four things here are deliberate and easy to undo by accident.

- **Recording is on the message path, so it does almost nothing.** One pass over
  `message.fields` lifting a fixed frozenset of tags, then the raw bytes, then
  stop. Naming fields, labelling values and building a summary all happen in
  `audit.entry`, on the one entry somebody opened. Four `Message.get` calls would
  be four linear scans — that is why `fix/render.py:extract` exists.
- **`Audit.entries()` scans backwards and breaks at the cursor.** The live tail
  polls with `after=<seq>`, so the cost must be the number of *new* entries, not
  the size of the ring. The obvious left-to-right version is O(ring) per poll per
  open board. It also reports `truncated` rather than silently skipping entries a
  cursor has outlived.
- **Control commands are hooked in `CommandRegistry.dispatch`, not the control
  server**, because the test harnesses dispatch against the registry directly.
  The command is recorded *before* the handler runs and completed afterwards, so
  it precedes its own execution reports. The unauthorised early return in
  `control/server.py` never reaches dispatch and is recorded there instead.
- **`registry.add(..., audit=True)` is the default, and queries opt out.** A new
  mutating command is then recorded whether or not its author thought about it;
  the reverse mistake floods the tape and would be silent on a venue nobody has
  open, so `tests/test_audit.py:AuditedCommandsTest` pins the exact audited set
  for both venues. Add a command, and that test tells you to choose.

Value meanings come from `enum_labels()` reversing the constant classes the
dictionaries already feed to `FieldDef(values=…)`, so they cannot drift from the
validation. It selects members **by type, not by name**: `ALL` is a metadata tuple
on Japannext's `SubID` and a real wire value on HKEX's `MassCancelRequestType`.
`FieldDef.redact` blanks a credential in the field dump *and* in the wire string,
which is therefore rebuilt from fields rather than echoed from the raw bytes.

### Non-obvious invariants

These caused real bugs and are easy to reintroduce.

**Events are produced as a batch, then rendered.** By render time the order has moved on. `OrderFilled` therefore snapshots `cum_qty`, `leaves_qty`, `notional_units` and `order_qty` at construction, and `_render_filled` restates them over whatever `_base_report` derived from the live order. Without this an IOC's partial fill reports `LeavesQty=0` (its post-cancellation value) and every fill of a multi-level sweep reports the final average price rather than its own running one. Any new event that carries running totals needs the same treatment.

**A book change is not the same thing as an event, and market data must be told about both.** `Market._absorb` publishes `book:` off the event batch, which is right for a submit or a cancel and wrong for an amend: reducing a quantity produces no event at all, and re-entry after a price change filters its `OrderAccepted` out — so the board kept showing the old size until `amend` began passing `changed=True`. Any future path that reaches into a book without producing an event needs the same flag.

**Prices are integers in the venue's smallest increment**, never floats — at a 0.1 tick a binary float cannot represent the value exactly. `PriceCodec` (`core/prices.py`) converts only at the boundary, and rejects excess precision rather than rounding, because rounding would make the simulator disagree with a venue that rejects the order.

**Injected orders carry an owner, not a session key.** `order.new` tags them `control:<OWNER>`, a *string*, where a FIX session key is a `(begin_string, sender, target)` tuple — so the two can never collide and `_session_for` correctly resolves nothing for them. A trade still produces an event per side, so the command filters events by `order.session_key` before reporting or publishing: reading `events[0]` blindly returns the *counterparty's* order, which it did until a test caught it.

**Reports route by order owner, not by message sender.** A trade or a self-trade-prevention cancel touches a resting order belonging to a different client. Each application's `_session_for` resolves the owning session from `order.session_key`; sending to whoever triggered the event is wrong.

**Messages sent while a session is disconnected are still numbered and persisted.** That is what makes Cancel on Disconnect work: the client discovers the gap at its next Logon and retrieves the reports by resend. Do not short-circuit `Session.send` when `transport is None`.

**`OrigClOrdID` is the order's *current* ClOrdID, not its original one** (the specification is explicit). `OrderRegistry.resolve` enforces this while still remembering every identifier ever used, so a genuine duplicate is still rejectable.

**Timestamp precision is a property of the dialect, not of FIX.** Japannext writes milliseconds and OCG-C microseconds, so `FieldDef.max_decimals` carries it and `build_session_dictionary(timestamp_decimals=...)` passes it down. The shared checker accepted only `.sss` until a real HKEX client sent what its own specification documents and was rejected with `SessionRejectReason=6`.

**Markets are addressed by SubID, not by port — at Japannext.** `DAY`, `NGHT`, `DAYX`, `DAYU` are four separate books and trading states reached over one connection via `TargetSubID(57)`, falling back to the session's `default_sub_id`. HKEX is the counter-example and the reason "market" must stay a core concept rather than a header tag: there, the security's segment picks the book and no message names it. A venue that narrows which markets carry an instrument overrides `Venue.books_for`, so `instrument.add` places it where the wire protocol would.

**The clock is injected, but FIX `SendingTime(52)` must always be real wall-clock** or conformant clients session-reject with `SessionRejectReason=10`. `FixedClock` exists for tests; market state transitions are command-driven, so no accelerated clock is needed at runtime.

**`MessageDef.inbound=False`** marks messages the venue only ever sends; the dictionary rejects them on receipt. New outbound message types must set it.

### The web process

`web/` is a **client of the venues, not part of them**: its own process, connecting to each venue's control port. Two things follow. It must never use `cli/client.py` — that client is deliberately blocking, and a stalled venue would freeze the board for every other venue; `web/link.py` is the non-blocking equivalent on a reactor connection. And it holds no venue logic: `POST /api/<venue>/<command>` forwards to `CommandRegistry.dispatch` verbatim, so every control command is a web API for free and the venue stays the only validator.

Server-Sent Events reuse `control/subscriptions.py:Publisher` unchanged — it only requires a `push(topic, data)` method, so an open HTTP response duck-types a control session. Topics are namespaced `<venue>/<topic>` on the way in because one web process watches several venues.

**Capability flags gate by command, not by verb.** `allow_order_entry`, `allow_market_control` and `allow_audit` are separate frozensets in `web/app.py` because they are different powers — closing a market expires every resting order on it, including other clients', and the audit shows every client's traffic whether or not this board may trade. A new mutating command must be added to one of them or it is ungated; the page reads all three from `/api/venues` and hides what it may not use, but the refusal is enforced server-side regardless.

**The stylesheet has one flat namespace, and it has bitten twice.** A row class of `control` picked up `.control { display: flex }` from the header widget and turned table rows into flex containers; a `<table class="fields">` picked up the order ticket's `.fields { display: grid }`. Before naming anything in `static/`, grep the stylesheet for the name. Related: `main` sets `display: grid`, which outranks the user agent's `[hidden] { display: none }` — hence the explicit `main[hidden]` rule, without which the hidden view stays on screen while reporting itself hidden.

**Zero is a number, except on the ladder.** `qty()` blanks zero, which is right for a price level with nothing on it and wrong everywhere else — a killed FOK reporting "(0 done, 0 left)" rendered as "( done, left)" until `num()` was split out. Use `qty()` only for ladder cells; use `num()` for anything where a zero is the answer.

Three rules govern the page itself. **Prices stay strings** — they arrive already formatted by `PriceCodec`, and `parseFloat` would reinstate the binary-float problem that codec exists to prevent; marking the best bid or the last trade is string equality between values from one formatter, so it needs no arithmetic. And **a `book:` event is a signal, not a payload**: `notify_book_change` publishes only the BBO, so the page refetches the ladder, coalesced to ~10/second. Publishing full depth on every book change would move rendering work into the matching path for no gain.

The page is **English-only text with Japanese-convention colour**: buying red, selling green, which is the reverse of the Western pairing and is set once in the `--bid`/`--ask` tokens in `style.css`. Every colour use goes through those two, so the convention is one edit, not thirty.

**Every colour in `style.css` is a token, and that is what makes the light theme possible.** `:root` holds the dark palette, `@media (prefers-color-scheme: light)` restates the same token names, and nothing below the two blocks writes a colour of its own — a hex value inline would exist in one theme and be invisible or illegible in the other. Adding a shade means adding a token to both blocks, so grep the file for a bare `#` or `rgba(` before shipping. Dark stays the fallback for a browser that states no preference.

The ita ladder is built server-side (`MarketDataService.ladder`) because tick size varies with price — a client stepping one remembered tick misaligns at the first tick-table threshold and cannot tell. Stepping down consults `price - 1` so a price exactly on a boundary takes the lower band's tick.

### Concurrency

Single-threaded `selectors` reactor (`core/reactor.py`), chosen over `asyncio` for deterministic I/O ordering (reproducible matching in CI) and because it can be stepped synchronously from tests. Two platform traps already handled — don't undo them:

- Windows `select()` raises `WSAEINVAL` on an empty fd set, so `step()` skips the select when nothing is registered.
- `SO_REUSEADDR` on Windows lets a *second* process bind a port already being listened on, so a failed restart silently shares the port. `_set_address_reuse` uses `SO_EXCLUSIVEADDRUSE` on Windows and `SO_REUSEADDR` elsewhere.
- A **failed** outbound connect reports the socket *writable*, so `Reactor.connect` must check `SO_ERROR` to tell success from failure — on Windows the failure arrives in the exception fd set, which `selectors.SelectSelector` folds into the write list. Windows also takes ~2s to refuse a loopback connect where Linux is instant, which is why only one test makes the OS actually refuse one.

## Testing

`unittest` only. No sleeps in unit tests: the reactor is pumped via `tests/support.py:pump()` and clocks are frozen, so there is no timing flakiness. Four harness layers, pick the narrowest that fits:

| Harness | Use for |
|---|---|
| `tests/coresupport.py` | book/matching/engine, no protocol |
| `tests/fixsupport.py` | FIX session layer against a fake transport |
| `tests/jnxsupport.py` | full Japannext venue, real dictionary and engine, fake sockets |
| `tests/hkexsupport.py` | the same for HKEX; its client helpers build the repeating groups, and `binary_venue_config()` puts a third broker on the binary encoding |
| `tests/support.py` | reactor and control plane over real loopback sockets |

`scenarios/*.json` run against a **live** daemon over real sockets and are the artifact CI calls. They share one process per venue, so give each scenario distinct ClOrdIDs (the registry remembers them for the process lifetime) and bracket it with `orders.cancel_all`. A scenario names its own `fix_port`, `control_port`, `begin_string` and `logon_fields`, so `make smoke` starts every venue in `VENUES` and one runner invocation covers them all.

**A green unit suite is not sufficient evidence.** Three bugs survived 500+ passing tests and were found only by starting the daemon and reading real FIX output. Before calling work done, run the venue and drive it.

## Conventions

- Commit at phase/feature boundaries, only once the full suite passes.
- Venue reference data is CSV (`venues/*/reference/`) so a table correction is a data edit, never a code change. Anything unconfirmed is flagged `UNVERIFIED` in the file header — currently only Japannext's tick-size tables, because the Trading Rules appendices are merged-cell tables that text extraction cannot reconstruct unambiguously. Japannext's price band tables and HKEX's spread table are transcribed from the published rules and are exact.
- **Boundary semantics in a threshold table are a real source of bugs.** `TickTable` rows apply from their bound *upwards*, but exchanges publish bands as "From 0.01 to 0.25 / Over 0.25 to 10.00" — so 0.25 belongs to the *lower* band and the next row's bound is 0.251, not 0.250. `tests/test_hkex.py:SpreadTableTest` checks both sides of every boundary against the Schedule for exactly this reason, and also asserts that every shipped base price is itself on-tick.
- `.gitattributes` pins LF: development is on Windows, deployment is RHEL 8.

## Other agent configs

A Gemini CLI config exists at `~/.gemini/settings.json`. If you want its user-level items (MCP servers, commands, instructions) brought into Claude Code, reply `/import` to see what is importable, then `/import --yes=<digest>` to apply it. If `/import` is unavailable on this surface, run `claude import` from a terminal.
