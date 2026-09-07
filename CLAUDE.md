# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An exchange simulator that stands in for a venue's test environment in CI/CD. Four venues:

- **Japannext PTS** equities over FIX 4.2.
- **HKEX securities** over OCG-C, covering board-lot continuous trading and the POS/CAS call auctions. HKEX publishes OCG-C in **two encodings of one protocol** -- FIX 5.0 SP2 on a FIXT.1.1 session, and a fixed-width binary format -- and both are served, on one set of books.
- **NSE India Capital Market** over the NNF Trimmed Protocol, covering the Regular Lot book and the pre-open call auction. This one is **not FIX in any encoding**: a proprietary fixed-width big-endian format with its own sign-on, no sequence numbers, no resend, an exchange-assigned order number in place of a client order ID, AES-256-GCM on every message, and a connection that is a *box* carrying many signed-on users.
- **NSE India Futures & Options** over the same NNF protocol, covering futures and options on the Regular Lot book. NSE runs it as a *separate trading system* -- its own gateway, its own boxes and users -- so it is its own venue package, and it is the reason `nnf/` was built knowing nothing about a segment. A contract is named by `CONTRACT_DESC`, five fields rather than two.

See `README.md` for the user guide, `DETAILED_DOC.md` for architecture, venue rules and known gaps, and `docs/specs/README.md` for what each specification required.

**Hard constraints, both deliberate:**

- **Python 3.6.8, standard library only.** No third-party packages at build or run time. The target is RHEL 8's `/usr/libexec/platform-python`. No `dataclasses`, no `datetime.fromisoformat`, no `asyncio.run`/`create_task`, no walrus. f-strings, PEP 526 annotations and insertion-ordered dicts are fine.
- **Fidelity to the published specification beats convenience.** Venue behaviour is transcribed from the PDFs listed in `docs/specs/README.md`. Where a spec is silent, mark the choice `ASSUMPTION` in code and add it to `ASSUMPTIONS` in that venue's `rules.py` so `exsim assumptions` reports it. Do not quietly invent venue behaviour — and prefer refusing a flow outright to half-simulating it, which is why an HKEX odd-lot order is rejected rather than treated as a board lot, and why every NSE book but Regular Lot is refused with its published error code.

## Commands

Development is on Windows against `venv36` (3.6.8, same version as the RHEL 8 target). There is **no `make` on the Windows box** — the `Makefile` is for the Linux target.

```bash
PY=./venv36/Scripts/python.exe            # on RHEL 8: /usr/libexec/platform-python

$PY -m unittest discover -s tests -t .    # full suite (~1250 tests, a few seconds)
$PY -m unittest tests.test_matching       # one module
$PY -m unittest tests.test_matching.SelfTradePreventionTest.test_cancel_newest_cancels_the_incoming_balance

$PY -m exchangesim.ctl.main start                        # every venue + the board, backgrounded
$PY -m exchangesim.ctl.main status                      # pids, ports, uptime, log sizes
$PY -m exchangesim.ctl.main logs hkex -n 40             # tail one service (-f follows)
$PY -m exchangesim.ctl.main restart hkex                # one service, others untouched
$PY -m exchangesim.ctl.main stop                        # all of it
$PY -m exchangesim.ctl.main check                       # every service's config, start nothing

$PY -m exchangesim.runner.main --config config/japannext.json --check   # validate one config
$PY -m exchangesim.runner.main --config config/japannext.json           # one venue, foreground
$PY -m exchangesim.runner.main --config config/hkex.json                # binary 9011, FIX 9012, control 9102
$PY -m exchangesim.runner.main --config config/nse.json                 # NNF 9021, gateway router 9022, control 9103
$PY -m exchangesim.runner.main --config config/nsefo.json               # NNF 9031, gateway router 9032, control 9104

$PY -m exchangesim.web.main --config config/web.json    # web board on :9200 (its own process)

$PY -m exchangesim.cli.exsim info                       # control CLI ($EXSIM_MARKET saves --market)
$PY -m exchangesim.cli.exsim --port 9102 audit          # every message and command recorded
$PY -m exchangesim.cli.exsim --port 9102 audit --seq 7  # one entry, field by field
$PY -m exchangesim.cli.exsim --port 9102 instruments    # same CLI, any venue
$PY -m exchangesim.cli.exsim --port 9103 call boxes    # NSE: the connections, as against the users on them
$PY -m exchangesim.cli.exsim --port 9103 call preopen  # NSE: the indicative auction price, security by security
$PY -m exchangesim.cli.exsim --port 9104 instruments   # NSE F&O: the contract universe
$PY -m exchangesim.scenario.runner "scenarios/*.json"   # end-to-end suite; needs *every* venue running

$PY tools/pdftext.py spec.pdf --grep OrdType            # read venue spec PDFs (stdlib only)
```

`--check` returns before `venue.setup()`, so it does **not** exercise reference-data loading or port binding. To validate those, actually start the process.

**Stopping daemons:** `ctl.main stop` is the way — it tracks pids in `var/run`, so it stops what it started and nothing else. Only if that fails (a daemon started by hand, a pidfile deleted) fall back to PowerShell `Get-Process python | Stop-Process -Force`; Bash `kill` on a Windows PID has silently failed here and left daemons running.

**`ctl/` is a supervisor of last resort, not a service manager.** It does not restart a crashed process and registers nothing with the OS: a deployment is a clone of the repo, everything it writes stays under `var/`, and there is no installer or service account by design. It replaces the backgrounded commands and the `kill` that followed them. Three things in it are load-bearing: a pidfile is verified against `/proc/<pid>/cmdline` before anything is signalled (a recycled pid would otherwise get a stranger killed); `start` waits for the service's *control port* rather than a timer, which is what let `make smoke` drop its `sleep 3`; and a supervised daemon runs with `--no-console` so its rotated `var/log/<name>.log` and its captured `var/log/<name>.out` are not two copies of one stream. A non-empty `.out` means something happened outside logging — read it first.

## Architecture

Three layers with a hard dependency rule: **`core/` may not import from `venues/` or any protocol gateway.** Arrows point inwards only.

```
  Wire      fix/  (tag=value)   binary/  (OCG-C)   nnf/  (NSE)
            shared: wire/acceptor.py, wire/values.py, fix/message.py
        |
  ------------------------------------------------------
  Gateway   venues/<venue>/handlers.py          dialect, mapping
        | commands v            ^ events
  ------------------------------------------------------
  Venue     venues/<venue>/                     rules, reference data
        |
  ------------------------------------------------------
  Core      core/                               book, matching, lifecycle, state
```

Every wire format turns bytes into the same `fix/message.py:Message` and back,
behind the seam `fix/codec.py:FixCodec` names — `framer`, `extract`, `decode`,
`encode`, `is_admin`, `raw_string`, and `prepare`/`logon_defaults` for the
client half a scripted scenario uses. **If you find yourself branching on a
protocol name outside a gateway, the branch is in the wrong place.**

Gateways turn wire messages into `core/commands.py` requests and `core/events.py` events back into wire messages. The core never builds a FIX field; the gateway never touches the book. Adding an exchange is a new package under `venues/` plus a line in `venues/registry.py` — and, if it does not speak a protocol already here, a new package beside `fix/` and `binary/`.

**How well that held for the second venue.** HKEX needed four core additions, and each one is a capability the core was missing rather than a Hong Kong special case: market orders in `matching.py`, self-match prevention keyed on the order (`Order.stp_id` / `stp_instruction`) alongside the existing per-market mode, `ExecInst.IGNORE_PRICE_CHECK`, and `CancelReason.MASS_CANCEL`. The session layer gained four `SessionConfig` options for FIXT.1.1 — all default off, so FIX 4.2 behaviour is byte-identical. Nothing in `core/` learned the word "HKEX".

**And for the third, which is not FIX at all.** NSE needed **two** core additions, on the same test: does the core lack a *concept*, or are you smuggling in a dialect?

- **An order named by the venue's own identifier.** `CancelRequest`/`ReplaceRequest` gained `order_id`, `OrderRegistry` tolerates a null ClOrdID, and `Engine._named_order` resolves by `by_order_id` **with an explicit ownership check** — the ClOrdID index is keyed by session and carries ownership for free, a global order-id map does not, and without the check one client could cancel another's order by guessing a number. Not an NSE quirk: it is the shape of every native non-FIX exchange API, and FIX itself has cancel-by-OrderID.
- **Which tie-breaks a call auction applies.** `auction.uncross(book, reference, rules)` and `Market(auction_rules=...)`, defaulting to the five-rule chain that was there before. Maximising the matchable quantity is universal; resolving a remaining tie in the direction of the surplus is not, and NSE has no such rule — a shared chain would have given one venue the other's price, silently and only sometimes.

Everything else NSE needs is above the core. Nothing in `core/` learned the word "NSE", and `tests/test_auction.py` and `tests/test_hkex_auction.py` pass untouched, which is the evidence the second change was neutral. What *did* move is `Acceptor` and its router, from `fix/acceptor.py` to `wire/acceptor.py`: accepting a connection and binding it to a session is not FIX, and a third protocol needing it made that visible. `fix/acceptor.py` re-exports them, so no venue import and no existing test changed — which is the evidence for that one.

A new venue also inherits its whole control surface: `venues/common_commands.py` holds every venue-agnostic command (trading state, instruments, market data, orders, behaviour, sessions) and a venue's own `commands.py` calls `common_commands.register(registry, venue)` before adding what only it can provide. For Japannext that residue is one command, `venue.assumptions`; for HKEX it is that plus the SMP registry and `segments`; for NSE it is `boxes`, `box.kill`, `gateway_router`, `preopen`, `preopen.lock` and `transactions`. The test is mechanical: a command that needs `from .dictionary import ...` belongs to the venue, everything else is shared.

### Where the four venues differ, and why it matters

Reach for any venue module as a template only after checking these, because each row is a place where copying one into another would be wrong.

| | Japannext | HKEX | NSE Cash | NSE F&O |
|---|---|---|---|---|
| Protocol | FIX 4.2 | FIX 5.0 SP2, two encodings | **not FIX at all**: NNF, fixed-width, big-endian | the same NNF, and the same 40-byte header -- but its **own structures under the same transaction codes** |
| Session | ResendRequest recovery, client may reset at Logon | `NextExpectedMsgSeqNum(789)` negotiation, client reset **refused** (use `session.reset`) | no sequence numbers, no resend, no session-level Reject; recovery is a message download | the same |
| A connection is | a session | a session | a **box**, carrying many signed-on users; dropping it signs off every one | the same |
| Identity | CompID pair | CompID pair (one on the wire in binary) | a numeric User ID, over a numeric Box ID | the same, on its own boxes and its own gateway |
| Order handle | `ClOrdID(11)` | `ClOrdID(11)` | **none**: amend and cancel name the exchange's own `OrderNumber` | the same |
| Instrument | `Symbol(55)` | `SecurityID(48)`; `Symbol` is not in the dialect at all | `SEC_INFO`: Symbol **and** Series, neither of which names a security alone | `CONTRACT_DESC`: **five** fields -- symbol, instrument type, expiry, strike, option type. A future's strike is **-1**, not 0 |
| Market routing | `TargetSubID(57)` per message | the security's `segment` column — no message names a market | one market; the book type names it, and every book but Regular Lot is refused | the same, and book 3 conflates Stop Loss with MIT |
| Price limits | one band table around the nominal price | **two** rules: the multiplicative 9-times rule against the nominal price (`rules.NineTimesRule`, not a table), and the quotation rule against the live BBO (`StandardValidator._check_quotation`) | a circuit filter that is a **percentage** of the base price, set **per security** (`rules.CircuitFilter`, from the `band` column) | the same, per **contract** |
| Order type and TIF | `OrdType(40)`, `TimeInForce(59)` | the same | **bits** of `ST_ORDER_FLAGS`; neither scalar field exists | the same name, **different bits**: `OnStop` splits into `SL`/`MIT`, and STPC moves to a second `ADDITIONAL_ORDER_FLAGS` byte |
| Auctions | none — the rules say so outright | POS and CAS, uncrossed by `core/auction.py` | the pre-open, with a **four**-rule chain: no surplus-direction tie-break | none built; its pre-open and its fifth status, Postclose, are published but unimplemented |
| Self-trade prevention | per-market mode, keyed on MPID | per-order `SelfMatchPreventionID(2362)`; the *instruction* is registered against the ID out of band, hence `venue.smp_instructions` | **none**, and a market configured with any mode is refused at start-up | none either, though an `STPC` bit exists on the wire and is refused |
| Groups | none | `<Parties>` and `<DisclosureInstructionGrp>` on every business message | none; every field of a structure is always on the wire | the same |
| Acknowledgement | only for an order that rests untraded | **before** matching -- `Market(ack_on_entry=True)` | **before** matching, for the same reason and more sharply: the acknowledgement is where the client learns the order number | the same |
| Rejection | `OrdRejReason(103)` on an Execution Report | its own reject codes | a numeric `ErrorCode` in the header of the erroring form of the same transaction | the same, from its **own** table -- 16521 means different things at the two venues |
| Encryption | none | none (the credential is opaque) | **AES-256-GCM on every message**, under a key collected from a separate Gateway Router port | the same, over its own router on its own port |

Repeating groups needed **no codec change**: `Message` keeps fields ordered and offers `get_all`/`append`, so a group is read positionally. What that cannot do is check the count, so `hkex/handlers.py:_check_count` does, rejecting a mismatch with `SessionRejectReason=16`. Any new group needs the same.

Positional also means **a group can only be built where it is built**. `<Parties>` on an Execution Report carries the counterparty's Broker ID on a trade (`PartyRole=17`, bit 31 in binary), and that entry goes in through `_set_parties`, not appended by the caller afterwards — a fourth tag added at the end of the message is not inside the group at all. The value reaches the gateway as `OrderFilled.counterparty_mpid`, snapshotted at match time for the same reason the running totals are, and passes through `HkexVenue.contra_broker`, where `counterparty.default` covers a match whose other side has no Broker ID (a control-plane order) and `counterparty.override` reports one fixed broker on every trade.

### The second encoding

HKEX publishes OCG-C twice, as tag=value FIX and as a fixed-width little-endian binary format, and entitles a Comp ID to one of them. The binary one is a **codec, not a second gateway**: `binary/` turns a frame into the same `fix.Message` everything above it speaks, so the session layer, dictionary validation, handlers, engine, audit, CLI and board are shared and neither encoding knows about the other.

The seam is four methods -- `framer`, `extract`/`decode`, `encode`, `is_admin` -- named by `fix/codec.py:FixCodec` and implemented again by `binary/codec.py:BinaryCodec`. `Session` and `Acceptor` hold one; nothing else in `fix/` touches the wire format. **If you find yourself branching on `session.wire` outside a gateway, the branch is in the wrong place** -- the one legitimate use is `hkex/handlers.py:_for_wire`, and the reason is below.

Five things here are load-bearing.

- **The one Comp ID on the wire is the client's, in both directions** (section 7.2). The codec supplies the venue's own identity as the missing half, so `_check_comp_ids` is unchanged; `BinaryCodec(..., client=True)` mirrors that for a client, which is what the test harness and the scenario runner use. A client that puts the *venue's* Comp ID in the header resolves to no session at all.
- **A binary message type without a layout decodes to MsgType `B<n>`**, which no dialect defines, so the dictionary refuses it as an invalid message type -- a clean Reject rather than a dropped session -- and `binary_type()` parses the number back so the Reject can name it. An unrecognised *value* likewise passes through as text for the dictionary to refuse. What is fatal is an unknown **bit position**: the field's width is unknown, so nothing after it can be parsed.
- **`dictionary.build_binary()` is a separate transcription, not a flag.** The two published tables disagree about required fields -- a Cancel Request has no OrderQty, `SecurityExchange` is optional, Logon has no EncryptMethod or HeartBtInt -- and each session is validated against the document its client was written from.
- **There is no OrderCancelReject in the binary encoding.** A refused cancel or amend is an Execution Report with `ExecType` `X`/`Y` carrying the order's identity and running totals, which 35=9 has no fields for. `_for_wire` rebuilds it in the gateway because those fields come from the *order*; a codec that reached for one would be a gateway.
- **Bit positions are per message type.** `Price` is bit 9 of a New Order, 11 of an Amend and 12 of an Execution Report. Every Execution Report variant in section 7.6.7 shares one assignment, though, so there is one layout and which fields it fills stays in the handlers.
- **A repeating block is a count, then an entry per count, each with its own two-byte presence map** (`layout.py:Block`, section 6.2.2). On the FIX side it is the ordinary positional group — a NumInGroup tag and repeated members — which is *flat*: an outer block of several entries each carrying an inner block could not be read back, so `Block.pack` refuses to write one rather than emitting bytes it cannot decode. Only the Party Entitlement Report needs blocks today.

`Audit` entries carry the `protocol` that recorded them and are read back with `venue.wire_codec(...)`; a binary entry renders as a hex dump with credentials struck out, so the bytes line up with a client's own log.

### The third protocol

NNF is not an encoding of FIX and nothing here pretends it is. It gets its own
stack, `nnf/`, beside `fix/` and `binary/`, and its own session layer — because
`fix/session.py` is built on MsgSeqNum, resend, gap fill and a persistent store,
and NNF has none of them. Decisively: **NNF has no session-level Reject.** A
refusal is the `*_ERROR` or `*_REJECT` form of the transaction that caused it,
carrying a numeric `ErrorCode` in the message header, so rejection belongs to
the gateway. That is why the session hands a dialect failure back through
`Application.on_invalid` instead of answering it itself.

What **is** shared is `fix/message.py:Message`. Strip its docstring and it is an
ordered list of numerically-keyed fields with a type discriminator, which is
exactly what this protocol has — so the dictionary, the audit, the control
plane, the CLI and the board all work here without knowing NNF exists. Three
rules keep that from becoming a claim that NNF is FIX:

- **A message type is the decimal transaction code**, as a string: `"2000"`, not
  `"D"`. Nothing maps one onto the other, because there is no correspondence to
  map. A code with no structure decodes to its own number, which no dialect
  defines, and is refused by name.
- **A FIX tag is reused only where the concept and the value domain coincide**
  — `Symbol(55)`, `Side(54)`, `OrderQty(38)`, `Price(44)`, `OrderID(37)` for the
  exchange-assigned order number. `ClOrdID(11)` and `OrigClOrdID(41)` are
  **not defined**, because NNF has no client handle and inventing one would put
  an identifier no client ever sent into the order record and the audit;
  `OrdType(40)` and `TimeInForce(59)` are not defined either, because NSE spells
  both as bits. Everything else lives in a private range (9000s header, 9100s
  body, 9200s one tag per bit, 9400+ reserved for Futures & Options).
- **In a fixed-width protocol a dictionary checks values, not presence.** Every
  field travels on every message, so `MessageDef.required` can never fail;
  messages are validated with `check_unknown=False`. Reading it as presence
  checking would give false confidence.

Six things here are load-bearing.

- **Offsets are transcribed, never computed.** Structures are `pragma pack 2`,
  so an eight-byte `LONG LONG` genuinely sits at offset 14 of the header, and a
  walk that summed widths would drift. Reserved runs are declared too, which is
  what makes `tests/test_nse_dictionary.py`'s check strict: the fields of each
  structure must reach **exactly** the length the appendix tables — 290 for an
  order, 228 for a trade, 276 for a sign-on. That test catches nearly every
  transcription slip and costs nothing.
- **Zero means "absent"**, for a price or an amend quantity, because the field
  travels either way and there is no other way to say it. Read literally, an
  amendment naming no quantity would amend every order down to nothing.
- **A connection is a box and a session is a user.** Several users sign on over
  one box, and disconnecting the box signs off every one. `manager.sessions`
  answers with users, because that is what an order's owner and a report's
  recipient mean; `manager.resolve_inbound` answers with boxes, because that is
  what a connection is.
- **Reports produced while a user is disconnected are dropped, not queued.**
  That inverts the FIX invariant below, and it must: there is no resend to
  deliver them. The client asks for a download instead.
- **The audit records the plaintext packet, not the ciphertext.** Under the
  existing encryption methodology the keystream runs continuously across the
  whole connection, so a packet cannot be decrypted out of order — and
  rendering an audit entry must not touch a live connection's state at all.
  `NnfCodec.raw_string` therefore decrypts nothing.
- **The IV counter rises towards the exchange and falls away from it.** The
  document states only the member's rule — increment before encryption,
  decrement before decryption — which cannot work if both ends apply it to one
  copy. Two sequences walking away from one origin is the only reading that
  matches the text and keeps GCM's requirement that an IV never repeat.
- **That counter is laid out little-endian, and it is the only value here
  that is.** Both documents' pseudocode hands the cipher the *address* of a
  C struct (`char caStaticIv[8]; long long lDynamicIv;`), so those bytes
  follow the member's host rather than this protocol's big-endian wire.
  Written big-endian it round-trips perfectly against another simulator and
  fails against every real client — which is exactly what it did. Because
  it is still open whether a member byte-swaps the field out of the GR
  response first, the exchange issues it as **zero**, where both readings
  coincide, and `gateway_router.dynamic_iv` defaults to `auto`: the layout
  is settled by whichever reading authenticates a box's first message.

`nnf/` knows nothing about a market segment: which tag is the User ID, which is
the Box ID and which code is the heartbeat are all arguments. That is what
`tests/test_nnf_codec.py` pins, with a layout defined in the test file, and it
is what lets Futures & Options land later as a second dictionary and a second
set of layouts over the same machinery.

### The second segment, and what it cost

Futures & Options did land that way: `nnf/`, `tls/` and `core/` needed **no
change at all** to carry it, and `venues/nsefo/` is a dictionary, a layout set,
rules, handlers and a venue. That is the claim above, tested.

What it also showed is that a transaction code is not an identity. **The same
number carries a different structure in each segment** -- `BOARD_LOT_IN (2000)`
is 290 bytes of Capital Market and 316 of F&O, `SIGN_ON_REQUEST (2300)` 276
against 278, and ten codes collide in all. `ST_ORDER_FLAGS` is worse, because it
collides *silently*: same name, same two bytes, different bits, with `OnStop`
split into `SL`/`MIT` and `STPC` moved out to a second `ADDITIONAL_ORDER_FLAGS`
byte the cash market has no equivalent of. Nothing in a decode would complain.

Three consequences worth keeping:

- **Never share a `D`/`X` module across the two.** `BOX_ID` is 9170 at Capital
  Market and 9650 here, so a message built with one venue's constants and
  encoded against the other's layouts sets the wrong fields and reports success.
  That is why `venues/nsefo/gateway_router.py` is a copy rather than an import,
  despite the wire structures being identical -- the *tags* are not.
- **A scenario names its dialect.** `scenario/runner.py` hard-coded Capital
  Market's layouts for every `"nnf"` scenario until F&O arrived; `"dialect":
  "nsefo"` selects the other, and `"nse"` remains the default.
- **`tests/test_nsefo_dictionary.py` asserts the collisions by name**, and that
  no tag number means one thing in one dictionary and something else in the
  other. The 9400+ reservation Capital Market wrote down is what made that
  assertion possible rather than aspirational.

A future's `StrikePrice` is **-1**, not 0 -- the protocol breaking its own "zero
means absent" rule, and the one place where reading the sibling document's
convention across would be wrong.

### Auctions

`core/auction.py` finds one price and executes everything at it; `venues/hkex/auctions.py` and `venues/nse/auctions.py` hold what each venue wraps around that. The split is the usual one — finding the price belongs to the core; the reference price, the bands and the carried-forward-order treatment are the venue's.

**Which tie-breaks apply is the venue's too, and that was a correction.** The chain was hard-coded as HKEX's five rules until a second venue turned out to have four: NSE's pre-open is maximum quantity, minimum unmatched, closest to the previous close, higher of two — with **no** surplus-direction rule. So `uncross(book, reference_price, rules)` takes the chain and `Market(auction_rules=...)` carries it, defaulting to `auction.STANDARD_RULES` so HKEX is unchanged. Maximising the matchable quantity is universal; almost nothing after it is.

Four things here are easy to get wrong:

- **An at-auction order has no price, so it cannot go in the sorted ladder.** `BookSide` keeps a separate FIFO for them, and they are deliberately excluded from `best_price`, `depth` and `orders_in_priority` — the BBO and the ita board are statements about limit prices. `orders()` and `__len__` *do* include them, so cancel-all and the order count stay honest. `orders_in_priority` is the continuous-matching iterator: a priceless resting order has no price to execute at, so it must never appear there.
- **An auction price is only available when the limit books overlap.** A book holding nothing but at-auction orders has no price of its own, and HKEX then matches at the reference price instead — which is why `Market.uncross` falls back to it rather than treating "no IEP" as "no trade". NSE does the same at the previous close, so the fallback is shared rather than one venue's habit.
- **A priceless order is filled first.** At-market orders come off their FIFO before the limit book at the auction price, which is standard and is why an NSE pre-open with an ATO order on one side splits the fills the way it does.
- **Uncrossing happens before the state change is applied.** `set_trading_state` calls `_close_auction` first, because moving to a closed state expires resting orders and a closing auction run afterwards would find an empty book.

Auction periods are **command-driven**, like every other phase here: `state.set` opens the session, `auction.lock` (HKEX) or `preopen.lock` (NSE) ends the input period, `state.set` ends it and uncrosses. NSE's locked phase is its own published state — "Preopen ended", which the core calls `OPENING_AUCTION` — and the book is frozen at a price nothing has executed at yet. Do not add a scheduler: a random closing period would make a test's outcome depend on the clock.

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
  for every venue. Add a command, and that test tells you to choose.

Value meanings come from `enum_labels()` reversing the constant classes the
dictionaries already feed to `FieldDef(values=…)`, so they cannot drift from the
validation. It selects members **by type, not by name**: `ALL` is a metadata tuple
on Japannext's `SubID` and a real wire value on HKEX's `MassCancelRequestType`.
`FieldDef.redact` blanks a credential in the field dump *and* in the wire string,
which is therefore rebuilt from fields rather than echoed from the raw bytes.

### Non-obvious invariants

These caused real bugs and are easy to reintroduce.

**Events are produced as a batch, then rendered.** By render time the order has moved on. `OrderFilled` therefore snapshots `cum_qty`, `leaves_qty`, `notional_units` and `order_qty` at construction, and `_render_filled` restates them over whatever `_base_report` derived from the live order. Without this an IOC's partial fill reports `LeavesQty=0` (its post-cancellation value) and every fill of a multi-level sweep reports the final average price rather than its own running one. `OrderAccepted` snapshots for the same reason wherever a venue acknowledges before matching. Any new event that carries running totals needs the same treatment.

**Messages sent while a session is disconnected are still numbered and persisted — at a venue that has a resend.** That is what makes FIX's Cancel on Disconnect work: the client discovers the gap at its next Logon and retrieves the reports. NSE inverts it and must: NNF has no resend, so `NnfSession.send` records an undelivered report to the audit and drops the bytes, and the client learns what it missed by asking for a download. Do not "fix" either one into the other.

**Whether an acceptance precedes the executions is a venue answer, not a FIX one.** Japannext reports Order Accepted only for an order that reaches the book untraded, so an order filled on entry is described once. HKEX acknowledges first and executes after -- its own flows show `ExecType=New, CumQty=0, LeavesQty=OrderQty` ahead of the fills (FIX 3.12 §6.6.1, §6.13.1) -- so `MatchingEngine.ack_on_entry` carries it, off by default, on for HKEX and NSE markets via each venue's `rules.ACK_BEFORE_EXECUTION`. It matters most at NSE, where the acknowledgement is the *only* place a client learns the order number it must cancel by. The case that matters is an IOC that never rests: without the acknowledgement its expiry report is the first the client hears of the order, naming an `OrderID` it was never given, and a real gateway rejected exactly that. Emit the acceptance in one place only -- with the flag on, `_rest` must not repeat it for the remainder.

**A book change is not the same thing as an event, and market data must be told about both.** `Market._absorb` publishes `book:` off the event batch, which is right for a submit or a cancel and wrong for an amend: reducing a quantity produces no event at all, and re-entry after a price change filters its `OrderAccepted` out — so the board kept showing the old size until `amend` began passing `changed=True`. Any future path that reaches into a book without producing an event needs the same flag.

**Prices are integers in the venue's smallest increment**, never floats — at a 0.1 tick a binary float cannot represent the value exactly. `PriceCodec` (`core/prices.py`) converts only at the boundary, and rejects excess precision rather than rounding, because rounding would make the simulator disagree with a venue that rejects the order.

**Injected orders carry an owner, not a session key.** `order.new` tags them `control:<OWNER>`, a *string*, where a FIX session key is a `(begin_string, sender, target)` tuple — so the two can never collide and `_session_for` correctly resolves nothing for them. A trade still produces an event per side, so the command filters events by `order.session_key` before reporting or publishing: reading `events[0]` blindly returns the *counterparty's* order, which it did until a test caught it.

**Reports route by order owner, not by message sender.** A trade or a self-trade-prevention cancel touches a resting order belonging to a different client. Each application's `_session_for` resolves the owning session from `order.session_key`; sending to whoever triggered the event is wrong.

**Messages sent while a session is disconnected are still numbered and persisted.** That is what makes Cancel on Disconnect work: the client discovers the gap at its next Logon and retrieves the reports by resend. Do not short-circuit `Session.send` when `transport is None`.

**`OrigClOrdID` is the order's *current* ClOrdID, not its original one** (the specification is explicit). `OrderRegistry.resolve` enforces this while still remembering every identifier ever used, so a genuine duplicate is still rejectable.

**Timestamps: UTC on the wire and in the API, local only at the point of display.** `core/clock.py:format_iso` is the one statement of that -- ISO 8601, milliseconds, trailing `Z` -- and `app.js:clockOf` and `cli/exsim.py:local_clock` are the two conversions. A naive ISO string is read as *local* by both a browser and a person, so an unmarked UTC timestamp silently shows the wrong hour; that is what it did. `audit.py` repeats the format inline rather than importing it, holding to its rule of importing nothing.

**A stock code is a number, and it is carried as one.** HKEX's listing tables print `00001`; a real gateway sends `1`, and so does everything here -- reference data, reports, market data, the audit. `HkexVenue.resolve_symbol` forgives padding on the way *in* and answers with the bare number, because a client left matching `1` against `00001` is comparing two spellings of one number. Only padding is forgiven -- a code that is not a number, or strips to something unlisted, is still refused.

**Timestamp precision is a property of the dialect, not of FIX.** Japannext writes milliseconds and OCG-C microseconds, so `FieldDef.max_decimals` carries it and `build_session_dictionary(timestamp_decimals=...)` passes it down. The shared checker accepted only `.sss` until a real HKEX client sent what its own specification documents and was rejected with `SessionRejectReason=6`.

**Markets are addressed by SubID, not by port — at Japannext.** `DAY`, `NGHT`, `DAYX`, `DAYU` are four separate books and trading states reached over one connection via `TargetSubID(57)`, falling back to the session's `default_sub_id`. HKEX is the counter-example and the reason "market" must stay a core concept rather than a header tag: there, the security's segment picks the book and no message names it. A venue that narrows which markets carry an instrument overrides `Venue.books_for`, so `instrument.add` places it where the wire protocol would.

**The clock is injected, but FIX `SendingTime(52)` must always be real wall-clock** or conformant clients session-reject with `SessionRejectReason=10`. `FixedClock` exists for tests; market state transitions are command-driven, so no accelerated clock is needed at runtime.

**`MessageDef.inbound=False`** marks messages the venue only ever sends; the dictionary rejects them on receipt. New outbound message types must set it.

### The web process

`web/` is a **client of the venues, not part of them**: its own process, connecting to each venue's control port. Two things follow. It must never use `cli/client.py` — that client is deliberately blocking, and a stalled venue would freeze the board for every other venue; `web/link.py` is the non-blocking equivalent on a reactor connection. And it holds no venue logic: `POST /api/<venue>/<command>` forwards to `CommandRegistry.dispatch` verbatim, so every control command is a web API for free and the venue stays the only validator.

Server-Sent Events reuse `control/subscriptions.py:Publisher` unchanged — it only requires a `push(topic, data)` method, so an open HTTP response duck-types a control session. Topics are namespaced `<venue>/<topic>` on the way in because one web process watches several venues.

**Capability flags gate by command, and the gate names the *queries*.** `allow_order_entry`, `allow_market_control` and `allow_audit` are separate powers — closing a market expires every resting order on it, including other clients', and the audit shows every client's traffic whether or not this board may trade. `web/app.py` classifies `order.new` and the three audit commands explicitly; **`allow_market_control` is the catch-all for everything else that is not in `READ_ONLY_COMMANDS`**, so a command nobody has classified is refused rather than granted.

That is the way round it is for a reason. Listing the three *powers* and letting the remainder through failed open, and it had: fourteen mutating commands were ungated, `orders.cancel_all` among them — precisely the power `allow_market_control` exists to withhold — so a board with every flag off could still wipe every resting order on every venue it could see. Listing the queries instead means a new mutating command is refused until somebody decides which power it needs, and a new query is refused until it is listed, which is visible and harmless. `tests/test_web_app.py:ReadOnlyCommandsTest` binds the list to the registries' own `audit` flags at all four venues, so it cannot drift from what the commands actually do. The page reads the three flags from `/api/venues` and hides what it may not use, but the refusal is enforced server-side regardless.

**The stylesheet has one flat namespace, and it has bitten twice.** A row class of `control` picked up `.control { display: flex }` from the header widget and turned table rows into flex containers; a `<table class="fields">` picked up the order ticket's `.fields { display: grid }`. Before naming anything in `static/`, grep the stylesheet for the name. Related: `main` sets `display: grid`, which outranks the user agent's `[hidden] { display: none }` — hence the explicit `main[hidden]` rule, without which the hidden view stays on screen while reporting itself hidden.

**Zero is a number, except on the ladder.** `qty()` blanks zero, which is right for a price level with nothing on it and wrong everywhere else — a killed FOK reporting "(0 done, 0 left)" rendered as "( done, left)" until `num()` was split out. Use `qty()` only for ladder cells; use `num()` for anything where a zero is the answer.

Three rules govern the page itself. **Prices stay strings** — they arrive already formatted by `PriceCodec`, and `parseFloat` would reinstate the binary-float problem that codec exists to prevent; marking the best bid or the last trade is string equality between values from one formatter, so it needs no arithmetic. And **a `book:` event is a signal, not a payload**: `notify_book_change` publishes only the BBO, so the page refetches the ladder, coalesced to ~10/second. Publishing full depth on every book change would move rendering work into the matching path for no gain.

The page is **English-only text with Japanese-convention colour**: buying red, selling green, which is the reverse of the Western pairing. Every colour use goes through the `--bid`/`--ask` tokens in `style.css`, so the convention is one edit, not thirty — and those two are now *aliases* onto `--hue-red`/`--hue-green`, which is what makes them configurable: `board.buy_colour` repoints an alias at another hue token, keeping that hue's light-theme value. A side's colour is a **name off the palette**, validated server-side, never a value; a free-text colour would bypass the 4.5:1 floor `tests/test_web_palette.py` holds every hue to.

**Buy is on the right, and three things move together.** The ticket's Buy button, Level 1's bid and the ladder's bid column are all placed by `board.buy_side` (default `right`, where a Japanese depth board puts bids). Never split them: a ticket disagreeing with the ladder above it is a click waiting to go the wrong way. Two are CSS `order` off `data-buy-side` on `<html>`; the ladder is table cells, which `order` cannot touch, so `app.js:ladderCells` is the one place that writes them in sequence — and reversing the static `<thead>` needs a full re-append, not `insertBefore(last, first)`, which leaves the price column at the end.

**Every colour in `style.css` is a token, and that is what makes the light theme possible.** `:root` holds the dark palette, `@media (prefers-color-scheme: light)` restates the same token names, and nothing below the two blocks writes a colour of its own — a hex value inline would exist in one theme and be invisible or illegible in the other. Adding a shade means adding a token to both blocks. Dark stays the fallback for a browser that states no preference. **`tests/test_web_palette.py` holds both halves of that to account**: that no colour is written outside the two blocks, that the two define the same token names, and that every token carrying text clears 4.5:1 against every surface it can land on — page, panel, sunken row and input well, plus a coloured value on its own wash. The floor is not decorative: `--ink-faint` sat at 2.8:1 and the board read as grey on grey on a dim monitor, which numbers would have caught and eyes did not. It also pins the surfaces a step apart, because text contrast alone does not carry a dense table whose rows and borders have dissolved.

The ita ladder is built server-side (`MarketDataService.ladder`) because tick size varies with price — a client stepping one remembered tick misaligns at the first tick-table threshold and cannot tell. Stepping down consults `price - 1` so a price exactly on a boundary takes the lower band's tick.

### Concurrency

Single-threaded `selectors` reactor (`core/reactor.py`), chosen over `asyncio` for deterministic I/O ordering (reproducible matching in CI) and because it can be stepped synchronously from tests. Two platform traps already handled — don't undo them:

- Windows `select()` raises `WSAEINVAL` on an empty fd set, so `step()` skips the select when nothing is registered.
- `SO_REUSEADDR` on Windows lets a *second* process bind a port already being listened on, so a failed restart silently shares the port. `_set_address_reuse` uses `SO_EXCLUSIVEADDRUSE` on Windows and `SO_REUSEADDR` elsewhere.
- A **failed** outbound connect reports the socket *writable*, so `Reactor.connect` must check `SO_ERROR` to tell success from failure — on Windows the failure arrives in the exception fd set, which `selectors.SelectSelector` folds into the write list. Windows also takes ~2s to refuse a loopback connect where Linux is instant, which is why only one test makes the OS actually refuse one.

**The reactor terminates TLS through a seam, and imports no `ssl`.** `Connection` runs its bytes through a duck-typed transport — `receive`/`transmit`/`drain`/`close_notify`, documented on `reactor.py:_PlainTransport`, which is the identity default. `_out` still holds *ciphertext*, so `_flush`, `close_when_flushed` and the want-write bookkeeping never learned about encryption; `listen(transport=factory)` secures a port and `connect(transport=instance)` a client. Three things here are load-bearing:

- **`exchangesim/tls/` is built on `MemoryBIO`, not `wrap_socket`.** An `SSLSocket` buffers decrypted plaintext inside the SSL object where `select()` cannot see it, so the loop can be told "not readable" while a whole message sits undelivered — the fix is a `pending()` drain loop that is easy to get subtly wrong and impossible to test for. A MemoryBIO has no hidden buffer, and "a write blocked on a read" stops being a special case. `asyncio.sslproto` is built this way for the same reasons.
- **`receive` produces bytes to *send*.** A handshake answers itself with no application data at all, which is why `drain` is separate from `transmit` and why `_complete_connect` drains a client transport's first flight — nothing else would prompt it.
- **Version policy is feature-detected, never version-detected.** 3.6 has only the `OP_NO_TLSv1_*` bits and no `SSLContext.minimum_version`; 3.14 deprecates those bits. `tls/context.py` branches on `hasattr(ssl, "TLSVersion")`. And `"1.2"` is a *floor*, not a pin — under OpenSSL 3.x it still negotiates 1.3 — so a control command must report the **negotiated** version, not the configured policy.

Certificates are generated rather than shipped or shelled out for (`tls/der.py`, `rsa.py`, `x509.py`), which is the same call `nnf/crypto.py` made about AES. The DER is **write-only**: `tls/certs.py` decides whether to reissue from an `issued.json` beside the PEMs, which also catches a changed SAN — a router host edited in config otherwise fails at a *verifying* client with an error that says nothing about configuration. A certificate that merely parses proves nothing, so the test for all of it is a real `ssl` handshake with `CERT_REQUIRED`, and `openssl verify` when the binary is there.

Development uses **two interpreters**: `venv36` is the target (3.6.8, OpenSSL 1.0.2q, **no TLS 1.3**), and `venv314` exists *only* so the 1.3 path is exercised somewhere on this box. Nothing may require 3.14 at run time. A test that cannot run on one of them must `skipTest` with a reason naming the OpenSSL version, so neither interpreter can quietly report success for a path it did not take.

## Testing

`unittest` only. No sleeps in unit tests: the reactor is pumped via `tests/support.py:pump()` and clocks are frozen, so there is no timing flakiness. Four harness layers, pick the narrowest that fits:

| Harness | Use for |
|---|---|
| `tests/coresupport.py` | book/matching/engine, no protocol |
| `tests/fixsupport.py` | FIX session layer against a fake transport |
| `tests/jnxsupport.py` | full Japannext venue, real dictionary and engine, fake sockets |
| `tests/hkexsupport.py` | the same for HKEX; its client helpers build the repeating groups, and `binary_venue_config()` puts a third broker on the binary encoding |
| `tests/nnfsupport.py` | one NNF box over a fake socket, with a stub gateway: registration, box sign-on, user sign-on, heartbeats |
| `tests/nsesupport.py` | the full NSE venue; a `BoxClient` is a connection and signs several users on over it |
| `tests/support.py` | reactor and control plane over real loopback sockets |

`scenarios/*.json` run against a **live** daemon over real sockets and are the artifact CI calls. They share one process per venue, so give each scenario distinct ClOrdIDs (the registry remembers them for the process lifetime) and bracket it with `orders.cancel_all`. A scenario names its own `fix_port`, `control_port`, `begin_string`, `logon_fields` and `protocol`, so `make smoke` starts every venue in `SMOKE_SERVICES` and one runner invocation covers them all.

Two things a scenario against NSE needs that the others do not. There is **no one-message logon**: a box registers, signs on, and only then does a user sign on, so the sequence is scripted with `send` steps and the `logon` verb refuses. And the identifier a scenario cancels by is the **exchange's** to choose, so an `expect` step names what to remember (`"capture": {"order": "37"}`) and a later step refers to it as `"$order"` — without which a scenario passes once and fails on its second run in the same process.

**A green unit suite is not sufficient evidence.** Three bugs survived 500+ passing tests and were found only by starting the daemon and reading real FIX output. Before calling work done, run the venue and drive it — and for a scenario, run it **twice**, because a shared process is what turns an identifier the venue assigned into a value a scenario cannot hard-code.

## Conventions

- Commit at phase/feature boundaries, only once the full suite passes.
- Venue reference data is CSV (`venues/*/reference/`) so a table correction is a data edit, never a code change. Anything unconfirmed is flagged `UNVERIFIED` in the file header — Japannext's tick-size tables, because the Trading Rules appendices are merged-cell tables that text extraction cannot reconstruct unambiguously, and NSE's securities universe, whose symbols and lots are real but whose reference prices are plausible rather than quoted. Japannext's price band tables and HKEX's spread table are transcribed from the published rules and are exact.
- **Boundary semantics in a threshold table are a real source of bugs.** `TickTable` rows apply from their bound *upwards*, but exchanges publish bands as "From 0.01 to 0.25 / Over 0.25 to 10.00" — so 0.25 belongs to the *lower* band and the next row's bound is 0.251, not 0.250. `tests/test_hkex.py:SpreadTableTest` checks both sides of every boundary against the Schedule for exactly this reason, and also asserts that every shipped base price is itself on-tick.
- `.gitattributes` pins LF: development is on Windows, deployment is RHEL 8.

## Other agent configs

A Gemini CLI config exists at `~/.gemini/settings.json`. If you want its user-level items (MCP servers, commands, instructions) brought into Claude Code, reply `/import` to see what is importable, then `/import --yes=<digest>` to apply it. If `/import` is unavailable on this surface, run `claude import` from a terminal.
