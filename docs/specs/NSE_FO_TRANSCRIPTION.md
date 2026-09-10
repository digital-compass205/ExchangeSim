# NSE Futures & Options (F&O) — Phase 0 transcription

Source: `docs/specs/TP_FO_Trimmed_NNF_PROTOCOL_9.50_20260820170606.pdf` ("Trimmed Protocol
for Non-NEAT Front End (NNF), Futures and Options Trading System", v9.50, 316 pages), read with
`tools/pdftext.py`. All page numbers below are the PDF's own printed page numbers, which match
`--pages` 1:1 for this document (checked against the table of contents, p.11-16).

This is a **research document, not code**. It exists so Phase 1 can build `venues/nsefo/` (or
similar) the way `venues/nse/` was built from the CM spec, without re-reading the PDF from
scratch. Everything is scoped to the same slice CM implements: the Regular Lot book of the
Normal market, continuous trading, plus what a client needs to log on and receive trade reports.
Stop Loss/MIT, Negotiated Trade, give-up and the broadcast feed are identified but not
transcribed field-by-field, per the task's scope.

**Spread orders were in that list and are not any more.** Chapter 5's
`MS_SPD_OE_REQUEST` (480 bytes) and its nine transaction codes are transcribed and
served: a spread is a calendar spread, two futures on one symbol with different
expiries, quoted at `PriceDiff`. Byte for byte the plain 316-byte `MS_OE_REQUEST` **is**
its first leg, so the structure is built from the plain fields rather than transcribed
again. Two-leg and three-leg orders (2102/2104) share the structure and nothing else —
`PriceDiff` "is not used for 2L/3L" — and remain refused by transaction code.

`tools/pdftext.py` has no table model — it reconstructs a table from text-positioning operators,
and multi-digit offsets that straddle two of the extractor's internal line breaks come out split,
e.g. `"9\n8"` for `98`, `"1\n72"` for `172`, `"22\n1"` for `221`. Every split offset below was
**reassembled by concatenating the printed digits** (`"9" + "8" = 98`) and then **verified by
summing field widths from the start of the structure**, exactly the check
`tests/test_nse_dictionary.py` runs mechanically for CM. Every structure below reaches its
published packet length exactly by this arithmetic; where it does not, that is called out
explicitly rather than silently adjusted.

---

## 1. Structures in scope

### 1.1 MESSAGE_HEADER — identical to CM, one field renamed

Table 1, p.21. F&O's own field-by-field table:

| Field | Type | Size | Offset |
|---|---|---|---|
| TransactionCode | SHORT | 2 | 0 |
| LogTime | LONG | 4 | 2 |
| AlphaChar | CHAR | 2 | 6 |
| **TraderId** | LONG | 4 | 8 |
| ErrorCode | SHORT | 2 | 12 |
| Timestamp | LONG LONG | 8 | 14 |
| TimeStamp1 | CHAR | 8 | 22 |
| TimeStamp2 | CHAR | 8 | 30 |
| MessageLength | SHORT | 2 | 38 |

Packet length 40 bytes (p.21), same as CM.

**Answer to the MESSAGE_HEADER question: byte-for-byte identical to CM's, field for field,
including every type and every offset.** The one difference is cosmetic: F&O's spec calls the
LONG at offset 8 `TraderId`; CM's calls the same slot `UserId`. Same width (4), same offset (8),
same job — the brief description under the table (p.21-22) says "This field should contain the
user ID" for both documents, and it is the field NNF developers are told to "populate the
relevant User ID field in" (Chapter 11, p.209, which reprints this same table verbatim under the
direct-interface section and calls the field `User Id` in prose while the table still prints
`TraderId`). This is a naming difference in the document, not a wire difference — `layouts.py`
for F&O should use `T.LONG` at offset 8 as CM does; only the constructed field name in the
dialect differs, and it need not even differ if `USER_ID` is reused.

The document also defines `INNER_MESSAGE_HEADER` (Table 2, p.23, 40 bytes, same fields
reordered: TraderId@0, LogTime@4, AlphaChar@8, TransactionCode@10, ErrorCode@12,
Timestamp@14, TimeStamp1@22, TimeStamp2@30, MessageLength@38) and `BCAST_HEADER`
(Table 3, p.23-24, 40 bytes). Both are out of scope (used only by Update Local DB / broadcast
downloads) but are captured here in case Phase 1 needs the download path.

### 1.2 CONTRACT_DESC — the instrument identity

Table 27, p.61-62. 28 bytes, embedded in `MS_OE_REQUEST` at offset 58 and in `MS_TRADE_CONFIRM`
at offset 136 (both verified by the summing check below).

| Field | Type | Size | Offset |
|---|---|---|---|
| InstrumentName | CHAR | 6 | 0 |
| Symbol | CHAR | 10 | 6 |
| ExpiryDate | LONG | 4 | 16 |
| StrikePrice | LONG | 4 | 20 |
| OptionType | CHAR | 2 | 24 |
| CALevel | SHORT | 2 | 26 |

**What a complete contract looks like, per venue value description (p.249-250):**

- `InstrumentName` — one of `FUTIDX`, `FUTSTK`, `OPTIDX`, `OPTSTK` (p.249; also named on p.161
  and p.193 as examples of the same field elsewhere). Left-justified, blank-padded CHAR(6).
- `Symbol` — the underlying's symbol (e.g. the index or stock name), CHAR(10).
- `ExpiryDate` — meaningful for every instrument type; a LONG holding seconds since
  midnight 1-Jan-1980, the same epoch as every other date/time field in this protocol
  (Chapter 2 guideline 3, p.18). Not documented as zero for any instrument type.
- `StrikePrice` — meaningful only for `OPTIDX`/`OPTSTK`. **For a Futures contract
  (`FUTIDX`/`FUTSTK`) this field is `-1`, not zero** (p.249: "This field will contain a valid
  strike for Options Contract and for Futures Contract it will be -1."). This is the one field
  where "absent" is not spelled as zero, contradicting the general NNF convention (Chapter 2,
  "all numeric data must be set to zero... unless a value is assigned") — flagged again under
  §6.
- `OptionType` — CHAR(2), one of `CE` (call), `PE` (put), or `XX` for a Futures contract, where
  the field is meaningless but still populated with the sentinel (p.249-250). This is a genuine
  three-valued enumeration, not a boolean call/put flag.
- `CALevel` — "Corporate Action Level... should be zero" for order entry (p.64, p.83). Always 0
  in the slice this simulator implements; F&O has no corporate-action adjustment flow in scope.

So a complete FUTIDX/FUTSTK contract has `StrikePrice=-1`, `OptionType="XX"`; a complete
OPTIDX/OPTSTK contract has a real `StrikePrice` and `OptionType` of `CE` or `PE`. `CALevel` is
always 0 for every instrument type in this slice.

The Trimmed variant `CONTRACT_DESC_TR` (Table 137, p.299) is the same five fields **without**
`CALevel` — 26 bytes, offsets 0/6/16/20/24. See §1.9.

### 1.3 ST_ORDER_FLAGS and ADDITIONAL_ORDER_FLAGS — the bit tables

**F&O publishes both endiannesses, exactly as CM's README already anticipated.** Table 28,
p.62-63, prints a small-endian table and then a big-endian table for the same 2-byte
`ST_ORDER_FLAGS`; Table 29, p.63, does the same for the 1-byte `ADDITIONAL_ORDER_FLAGS`. The
host is big-endian (Chapter 2, p.19), so the big-endian table is the one that matters, and the
small-endian one is its bit-for-bit mirror — the same relationship CM's `layouts.py` documents
for its own `ST_ORDER_FLAGS`.

**`ST_ORDER_FLAGS` is bit-for-bit *different* from CM's, despite the identical name and the same
2-byte width.** Big-endian, MSB to LSB, byte 0 then byte 1 (p.62-63):

| Bit (byte, MSB→LSB) | Byte 0 | Byte 1 |
|---|---|---|
| 7 | ATO | MF |
| 6 | Market | MatchedInd |
| 5 | SL | Traded |
| 4 | MIT | Modified |
| 3 | Day | Frozen |
| 2 | GTC | PreOpen |
| 1 | IOC | Reserved |
| 0 | AON | Reserved |

CM's `ST_ORDER_FLAGS` byte 0 is `ATO, Market, OnStop, Day, GTC, IOC, AON, MF` and byte 1 is
`MatchedInd, Traded, Modified, Frozen, PreOpen, —, STPC, —`. F&O's byte 0 replaces CM's single
`OnStop` bit with **two** bits, `SL` (Stop Loss) and `MIT` (Market If Touched) — distinct order
types that CM's cash-market book doesn't have — and moves `MF` out of byte 0 into byte 1's MSB.
F&O's byte 1 has no `STPC` bit at all: self-trade-prevention-cancel moved to
`ADDITIONAL_ORDER_FLAGS` (below), and F&O's two Reserved bits sit at the *bottom* of byte 1
where CM's single STPC bit and one Reserved bit sit. **A structure with this name cannot be
copied from CM's `layouts.py:_order_flags` — it needs its own `Flags` tuple.**

Order Terms/Attributes table (p.68-69) confirms the semantics: `AON`=1 All Or None, `IOC`=1
Immediate Or Cancel, `GTC`=1 Good Till Cancel, `Day`=1 Day (default), `MIT`=1 Market If Touched,
`SL`=1 Stop Loss, `Market`=0 for a market order (an inverted-sense bit — reads oddly but is
transcribed as printed), `ATO`=1 market order in Preopen, `Frozen`/`Modified`/`Traded`/
`MatchedInd`/`PreOpen`/`MF` are output-only status bits with the obvious sense.

**`ADDITIONAL_ORDER_FLAGS` is a whole structure CM does not have.** Table 29, p.63. 1 byte,
big-endian bit order MSB→LSB: `Reserved(3), STPC, Reserved, Reserved, COL, BOC` (small-endian
mirror: `BOC, COL, Reserved, Reserved, STPC, Reserved(3)`).

- `BOC` — not expanded in this document's glossary; contextually a book/order-class flag (its
  companion `COL` below is spelled out, `BOC` is not — flagged under §6 as **Not found**).
- `COL` — Cancel On Logoff (p.69, confirmed against the User COL Status Update feature,
  p.232-233).
- `STPC` — "Cancel order resulting in self trade as per default action by the exchange" when 0,
  "Cancel active order resulting in self trade" when 1 (p.69). This is CM's self-trade-prevention
  instruction, **relocated** from bit 15 of `ST_ORDER_FLAGS` (CM) to bit 4 of
  `ADDITIONAL_ORDER_FLAGS` (F&O). The note on p.69 is important: "STPC bit in the modification
  transcodes should be same as set in the original order else the modification request will be
  rejected. In case of triggered stop loss order, bit selected during order entry will be
  considered." — the instruction is fixed at entry and re-asserted, not renegotiated, on every
  later message.

### 1.4 MS_OE_REQUEST — order entry/modify/cancel and their confirmations

Table 26, p.59-61. **316 bytes**, transaction code `BOARD_LOT_IN (2000)` on the header, but this
one structure carries fourteen transaction codes exactly as CM's 290-byte structure does (§4).

| Field | Type | Size | Offset |
|---|---|---|---|
| MESSAGE_HEADER | STRUCT | 40 | 0 |
| ParticipantType | CHAR | 1 | 40 |
| Reserved | CHAR | 1 | 41 |
| CompetitorPeriod | SHORT | 2 | 42 |
| SolicitorPeriod | SHORT | 2 | 44 |
| Modified/CancelledBy | CHAR | 1 | 46 |
| Reserved | CHAR | 1 | 47 |
| ReasonCode | SHORT | 2 | 48 |
| Reserved | CHAR | 4 | 50 |
| TokenNo | LONG | 4 | 54 |
| CONTRACT_DESC | STRUCT | 28 | 58 |
| CounterPartyBrokerId | CHAR | 5 | 86 |
| Reserved | CHAR | 1 | 91 |
| Reserved | CHAR | 2 | 92 |
| CloseoutFlag | CHAR | 1 | 94 |
| Reserved | CHAR | 1 | 95 |
| OrderType | SHORT | 2 | 96 |
| OrderNumber | DOUBLE | 8 | 98 |
| AccountNumber | CHAR | 10 | 106 |
| BookType | SHORT | 2 | 116 |
| BuySellIndicator | SHORT | 2 | 118 |
| DisclosedVolume | LONG | 4 | 120 |
| DisclosedVolumeRemaining | LONG | 4 | 124 |
| TotalVolumeRemaining | LONG | 4 | 128 |
| Volume | LONG | 4 | 132 |
| VolumeFilledToday | LONG | 4 | 136 |
| Price | LONG | 4 | 140 |
| TriggerPrice | LONG | 4 | 144 |
| GoodTillDate | LONG | 4 | 148 |
| EntryDateTime | LONG | 4 | 152 |
| MinimumFill/AONVolume | LONG | 4 | 156 |
| LastModified | LONG | 4 | 160 |
| ST_ORDER_FLAGS | STRUCT | 2 | 164 |
| BranchId | SHORT | 2 | 166 |
| TraderId | LONG | 4 | 168 |
| BrokerId | CHAR | 5 | 172 |
| cOrdFiller | CHAR | 24 | 177 |
| Open/Close | CHAR | 1 | 201 |
| Settlor | CHAR | 12 | 202 |
| Pro/ClientIndicator | SHORT | 2 | 214 |
| SettlementPeriod | SHORT | 2 | 216 |
| ADDITIONAL_ORDER_FLAGS | STRUCT | 1 | 218 |
| Reserved | CHAR | 1 | 219 |
| Reserved (16 bits, unnamed — see note) | USHORT | 2 | 220 |
| Reserved | CHAR | 1 | 222 |
| Reserved | CHAR | 1 | 223 |
| NnfField | DOUBLE | 8 | 224 |
| MktReplay | LONG LONG | 8 | 232 |
| PAN | CHAR | 10 | 240 |
| AlgoID | LONG | 4 | 250 |
| Reserved | SHORT | 2 | 254 |
| LastActivityReference | LONG LONG | 8 | 256 |
| Reserved | CHAR | 52 | 264 |

Total: 264 + 52 = **316**, matching the published packet length exactly.

**Transcription note on offset 220-223.** The extractor renders this run as eighteen
individually-named rows — `Filler1`...`Filler16` (each `USHORT, 1 (bit)`, at offset 220 for the
first eight and 221 for the next eight) and `Filler17`/`Filler18` (each `CHAR, 1`, at offsets 222
and 223) — which is the PDF's own table for a 4-byte reserved run laid out as a 16-bit
placeholder bitfield plus two spare bytes, not an extraction artifact. It sits between
`ADDITIONAL_ORDER_FLAGS`+its 1-byte Reserved (ending at offset 220) and `NnfField` (starting at
224), and the widths (2+1+1=4) close the gap exactly. Nothing in the field-description table
(p.64-68) names these bits, so they are transcribed as four bytes of Reserved and not given
individual tags; the fact that they are pre-declared as a *bitfield* rather than plain filler
bytes suggests this space is earmarked for a future third flags byte, exactly as
`ADDITIONAL_ORDER_FLAGS` itself was presumably added into what was once Reserved space in an
earlier protocol version (the revision history, p.3-6, shows flags and error codes being added
release over release).

**`MktReplay` is `LONG LONG` (8 bytes), not `DOUBLE`.** CM's analogous field at this position is
`ExecTimeStamp`, a `DOUBLE`. F&O's field description (p.68) says it "contains the time when the
order enters the trading system... stamped at the host end," functionally the same idea as CM's
`ExecTimeStamp`, but the wire type is different — do not reuse CM's `T.DOUBLE` here.

**Field semantics worth flagging**, from p.64-68:

- `TokenNo` — the contract's token number; sent alongside `CONTRACT_DESC` and cross-validated
  against it if non-zero ("If the valid token number is sent, the validation will be done on
  token number as well as contract descriptor," p.64). This simulator has no token-number
  reference data; treating a zero/absent `TokenNo` as "validate on `CONTRACT_DESC` alone" is the
  natural reading and is recorded as an assumption in §6.
- `CounterPartyBrokerId` — "valid only for Negotiated Trade Orders... for other books, this field
  should be blank" (p.64). Out of scope (Negotiated Trade is not RL continuous trading).
- `CloseoutFlag` — "should be set to blank" for order entry; used only when a broker is in
  closeout status (p.64, p.69-70), a compliance state this simulator does not model.
- `OrderType` — "should be set to blank" for order entry (p.65) and not otherwise defined for any
  RL transaction code in this document. See §6, **Not found**.
- `Open/Close` — Futures/Options-specific position-open-or-close indicator (p.67); CM's cash
  market has no equivalent concept at all, since equities have no notion of opening or closing a
  position the way a derivatives contract does.
- `PAN` — mandatory on every order regardless of Pro/Client (p.68), matching CM's PAN field.
- `AlgoID` — 0 for a non-algo order (p.68), matching CM's ALGO_ID.
- `LastActivityReference` — a nanosecond-resolution activity token, echoed back on every response
  and required as an idempotency-style check on modify/cancel requests (p.67, p.75, p.77, p.80).
  Functionally identical to CM's `LastActivityReference`.

### 1.5 PRICE_MOD — an F&O-only optimized price-modification structure

Table 30, p.75-76. **106 bytes**, not present in CM at all. Two transaction codes:
`PRICE_MOD_IN (2013)` and `PRICE_MOD_ACK_IN (20406)` (the latter is the "immediate ack" variant,
§1.9/§4).

| Field | Type | Size | Offset |
|---|---|---|---|
| MESSAGE_HEADER | STRUCT | 40 | 0 |
| TokenNo | LONG | 4 | 40 |
| TraderID | LONG | 4 | 44 |
| OrderNumber | DOUBLE | 8 | 48 |
| BuySell | SHORT | 2 | 56 |
| Price | LONG | 4 | 58 |
| Volume | LONG | 4 | 62 |
| LastModified | LONG | 4 | 66 |
| Reference | CHAR | 4 | 70 |
| LastActivityReference | LONG LONG | 8 | 74 |
| Reserved | CHAR | 24 | 82 |

Total: 82 + 24 = **106**, matches.

Purpose (p.75): change only the price of an existing Regular Lot order without resending the
whole `MS_OE_REQUEST`; "Volume will not be modified through this transcode." A price of 0
converts the order to a market-priced order (p.76). `Reference` is client-discretionary and not
echoed meaningfully by the venue ("The front-end may use this field at their discretion," p.77).
This is genuinely new functionality relative to CM — an in-scope RL order can be price-modified
this way, and Phase 1 should decide whether to implement it or refuse `PRICE_MOD_IN` outright (no
error code is specified for "not implemented"; refusing with `ERR_BAD_TRANS_CODE (16003)` is the
same choice CM makes for any transaction code it doesn't recognise).

### 1.6 MS_TRADE_CONFIRM — the trade confirmation

Table 37, p.120-121. **296 bytes**, transaction code `TRADE_CONFIRMATION (2222)`, and (like
CM's analogous structure) shared by several other codes — see §4.

| Field | Type | Size | Offset |
|---|---|---|---|
| MESSAGE_HEADER | STRUCT | 40 | 0 |
| ResponseOrderNumber | DOUBLE | 8 | 40 |
| BrokerId | CHAR | 5 | 48 |
| Reserved | CHAR | 1 | 53 |
| TraderNumber | LONG | 4 | 54 |
| AccountNumber | CHAR | 10 | 58 |
| BuySellIndicator | SHORT | 2 | 68 |
| OriginalVolume | LONG | 4 | 70 |
| DisclosedVolume | LONG | 4 | 74 |
| RemainingVolume | LONG | 4 | 78 |
| DisclosedVolumeRemaining | LONG | 4 | 82 |
| Price | LONG | 4 | 86 |
| ST_ORDER_FLAGS | STRUCT | 2 | 90 |
| GoodTillDate | LONG | 4 | 92 |
| FillNumber | LONG | 4 | 96 |
| FillQuantity | LONG | 4 | 100 |
| FillPrice | LONG | 4 | 104 |
| VolumeFilledToday | LONG | 4 | 108 |
| ActivityType | CHAR | 2 | 112 |
| ActivityTime | LONG | 4 | 114 |
| CounterTraderOrderNumber | DOUBLE | 8 | 118 |
| CounterBrokerId | CHAR | 5 | 126 |
| Reserved (alignment pad) | CHAR | 1 | 131 |
| Token | LONG | 4 | 132 |
| CONTRACT_DESC | STRUCT | 28 | 136 |
| OpenClose | CHAR | 1 | 164 |
| OldOpenClose | CHAR | 1 | 165 |
| BookType | CHAR | 1 | 166 |
| Reserved | LONG | 4 | 168 |
| OldAccountNumber | CHAR | 10 | 172 |
| Participant | CHAR | 12 | 182 |
| OldParticipant | CHAR | 12 | 194 |
| ADDITIONAL_ORDER_FLAGS | STRUCT | 1 | 206 |
| Reserved | CHAR | 1 | 207 |
| Reserved | CHAR | 1 | 208 |
| Reserved (ReservedFiller2) | CHAR | 1 | 209 |
| PAN | CHAR | 10 | 210 |
| OldPAN | CHAR | 10 | 220 |
| AlgoID | LONG | 4 | 230 |
| Reserved | SHORT | 2 | 234 |
| LastActivityReference | LONG LONG | 8 | 236 |
| Reserved | CHAR | 52 | 244 |

Total: 244 + 52 = **296**, matches.

**Transcription note on offset 131.** `CounterBrokerId` (CHAR, 5) ends at offset 131, which is
odd; `Token` (LONG, 4) needs 2-byte alignment under `pragma pack 2`, so a 1-byte pad is required
before it. The document's printed offset for `Token` is 132, one past 131, confirming the pad —
this is the same "structures of odd size are padded to an even number of bytes" rule from
Chapter 2 (p.20) applied mid-structure, not just at the end.

**How this differs from CM's 228-byte `MS_TRADE_CONFIRM`.** F&O is 296 bytes against CM's 228 —
68 bytes more. The largest additions are Futures/Options-only concepts CM's cash market has no
analogue for: `OpenClose`/`OldOpenClose` (position open/close on the trade, p.123), `Participant`/
`OldParticipant` (the clearing participant on the trade, distinct from the broker), `OldPAN` (the
PAN before a trade modification), and a full `ADDITIONAL_ORDER_FLAGS` byte plus its reserved
padding. CM's `OP_ORDER_NUMBER`/`OP_BROKER_ID` (counterparty order/broker) map onto F&O's
`CounterTraderOrderNumber`/`CounterBrokerId` at the same conceptual position, just renamed and
at different offsets because of everything inserted ahead of them.

### 1.7 MS_SIGNON — logon request/response (same codes as CM, different structure)

Table 6/7, p.31-32 (request) and p.34-35 (response) — **same layout for `SIGN_ON_REQUEST_IN
(2300)` and `SIGN_ON_REQUEST_OUT (2301)`**, both **278 bytes**.

| Field | Type | Size | Offset |
|---|---|---|---|
| MESSAGE_HEADER | STRUCT | 40 | 0 |
| UserID | LONG | 4 | 40 |
| Reserved | CHAR | 8 | 44 |
| Password | CHAR | 8 | 52 |
| Reserved | CHAR | 8 | 60 |
| NewPassword | CHAR | 8 | 68 |
| TraderName | CHAR | 26 | 76 |
| LastPasswordChangeDate | LONG | 4 | 102 |
| BrokerID | CHAR | 5 | 106 |
| Reserved | CHAR | 1 | 111 |
| BranchID | SHORT | 2 | 112 |
| VersionNumber | LONG | 4 | 114 |
| Batch2StartTime (IN) / EndTime (OUT) | LONG | 4 | 118 |
| HostSwitchContext (IN) / Reserved (OUT) | CHAR | 1 | 122 |
| Colour | CHAR | 50 | 123 |
| Reserved | CHAR | 1 | 173 |
| UserType | SHORT | 2 | 174 |
| SequenceNumber | DOUBLE | 8 | 176 |
| WsClassName (IN) / Reserved (OUT) | CHAR | 14 | 184 |
| BrokerStatus | CHAR | 1 | 198 |
| ShowIndex | CHAR | 1 | 199 |
| ST_BROKER_ELIGIBILITY_PER_MKT | STRUCT | 2 | 200 |
| MemberType | SHORT | 2 | 202 |
| ClearingStatus | CHAR | 1 | 204 |
| BrokerName | CHAR | 25 | 205 |
| Reserved | CHAR | 16 | 230 |
| Reserved | CHAR | 16 | 246 |
| Reserved | CHAR | 16 | 262 |

Total: 262 + 16 = **278**, matches (both directions; the OUT table on p.35 reprints these same
offsets).

**This is a critical safety finding: F&O's `MS_SIGNON` reuses the exact same transaction codes as
CM's `SIGNON_IN`/`SIGNON_OUT` (2300/2301) but is a different, incompatible 278-byte structure**
against CM's 276-byte one. Field-by-field differences: F&O's `BrokerName` is CHAR(25), CM's is
CHAR(26); F&O has a 50-byte `Colour` field CM does not have at all; F&O's request-only
`Batch2StartTime`/`HostSwitchContext` and response-only `EndTime` have no CM equivalent by those
names (though `EndTime` plays CM's `LAST_MARKET_CLOSE`-adjacent role — see field description
below); F&O's `WsClassName` (14 bytes) occupies the slot CM calls `WorkstationNumber` (also 14
bytes) but is described differently ("network ID of the workstation," CM, vs no description given
for `WsClassName` beyond the field table itself — see §6). **A shared `layouts.py` module across
CM and F&O must not reuse one `_signon_fields()` function; the two dialects need separate
transcriptions even though the transaction codes collide.**

Field semantics (p.32-36): `Password`/`NewPassword` — CHAR(8), mandatory to contain at least one
uppercase, one lowercase, one digit and one of `@#$%&*/\` (p.32-33), first login must use
`Neat@FO1` and change it. `Colour` — "should be set to blank" (p.34); no further semantics
given, likely a vestige of a NEAT terminal preference never meaningful over NNF. `EndTime` — "the
time when the markets last closed... If this time is different from the time sent in an earlier
logon, all orders, trades and messages for this trader must be deleted from the Local Database"
(p.35-36) — this is exactly CM's `LAST_MARKET_CLOSE`/`SequenceNumber` role, but F&O carries it
in a *separate* field (`EndTime`) from `SequenceNumber`, whereas CM overloads `SequenceNumber`
itself for both "sent as zero on request" and "carries last-market-close on response." F&O's
`SequenceNumber` (DOUBLE, offset 176) is described identically to CM's — "contains the time when
the markets closed the previous trading day" — so F&O effectively has this concept twice
(`EndTime` and `SequenceNumber`), unlike CM's single overloaded field. This duplication is
transcribed as printed rather than resolved; flagged again in §6.

### 1.8 MS_SYSTEM_INFO_REQ / MS_SYSTEM_INFO_DATA — same codes, different structures

**`SYSTEM_INFORMATION_IN (1600)`**, Table 8, p.37-38: **44 bytes**, not empty like CM's.

| Field | Type | Size | Offset |
|---|---|---|---|
| MESSAGE_HEADER | STRUCT | 40 | 0 |
| LastUpdatePortfolioTime | LONG | 4 | 40 |

Total 44, matches. Another shared-code collision: CM's `SYSTEM_INFORMATION_IN` is the bare
40-byte header; F&O's carries one extra field.

**`SYSTEM_INFORMATION_OUT (1601)`**, Table 9, p.38-40: **106 bytes** against CM's 94.

| Field | Type | Size | Offset |
|---|---|---|---|
| MESSAGE_HEADER | STRUCT | 40 | 0 |
| ST_MARKET_STATUS | STRUCT | 8 | 40 |
| ST_EX_MARKET_STATUS | STRUCT | 8 | 48 |
| ST_PL_MARKET_STATUS | STRUCT | 8 | 56 |
| UpdatePortfolio | CHAR | 1 | 64 |
| MarketIndex | LONG | 4 | 65 |
| DefaultSettlementPeriod(Normal) | SHORT | 2 | 69 |
| DefaultSettlementPeriod(Spot) | SHORT | 2 | 71 |
| DefaultSettlementPeriod(Auction) | SHORT | 2 | 73 |
| CompetitorPeriod | SHORT | 2 | 75 |
| SolicitorPeriod | SHORT | 2 | 77 |
| WarningPercent | SHORT | 2 | 79 |
| VolumeFreezePercent | SHORT | 2 | 81 |
| SnapQuoteTime | SHORT | 2 | 83 |
| Reserved | CHAR | 2 | 85 |
| BoardLotQuantity | LONG | 4 | 87 |
| TickSize | LONG | 4 | 91 |
| MaximumGtcDays | SHORT | 2 | 95 |
| ST_STOCK_ELIGIBLE_INDICATORS | STRUCT | 2 | 97 |
| DisclosedQuantityPercentAllowed | SHORT | 2 | 99 |
| RiskFreeInterestRate | LONG | 4 | 101 |
| Reserved (alignment pad) | CHAR | 1 | 105 |

Total: 101 + 4 = 105 (odd) → padded to **106** per the same odd-size rule (Chapter 2, p.20);
matches the published length exactly.

Sub-structures (p.39-40), each 8 bytes, identical shape (`Normal/Oddlot/Spot/Auction`, each
SHORT, offsets 0/2/4/6):

- `ST_MARKET_STATUS` — Table 10.
- `ST_EX_MARKET_STATUS` — Table 11 ("EX" = extended/exchange-driven market status, not expanded
  further in the document — see §6).
- `ST_PL_MARKET_STATUS` — Table 12 ("PL" not expanded either — see §6).

`ST_STOCK_ELIGIBLE_INDICATORS` (Table 13, p.40), 2 bytes, big-endian bit order:
`AON, MinimumFill, BooksMerged, Reserved(5)` in byte 0, `Reserved` (a plain byte, not itemised
bits) in byte 1. Functionally the same concept as CM's `SECURITY ELIGIBLE INDICATORS`
(`SECURITY_AON`, `SECURITY_MIN_FILL`, `SECURITY_BOOKS_MERGED`) but F&O's bit *order* within the
byte is `AON, MinFill, BooksMerged` (MSB-first) against CM's `AON, MinFill, BooksMerged` from its
own big-endian table too — these appear to agree in relative order, unlike `ST_ORDER_FLAGS`, but
should still be re-verified against CM's exact bit table before assuming identity, since the two
are separately-published tables in separately-published documents.

`RiskFreeInterestRate` (p.42) has no CM equivalent — an options-pricing input the cash market has
no use for.

### 1.9 The "Trimmed" (`_TR`) structures — a second, more compact wire format

Chapter appendix, p.298-310 ("Trimmed Structures"). These are **not** the same thing as the
document's overall title ("Trimmed... Protocol"); they are an additional, optional, *more*
compact encoding layered inside it, apparently for lower-bandwidth or immediate-ack use, each
paired with its own even-numbered "immediate ack" transaction code from Chapter 15 (p.252-255,
not transcribed in depth — background/overview/co-existence text only).

The defining feature: **these structures do not use the 40-byte `MESSAGE_HEADER` at all.** They
open with a compact inline prefix instead:

- `MS_OE_REQUEST_TR` (Table 136, p.298-299), 158 bytes, transaction codes
  `BOARD_LOT_IN_TR (20000)` / `TRIMMED_BOARD_LOT_ACK_IN (20400)`: prefix is
  `TransactionCode(SHORT,0), UserID(LONG,2), ReasonCode(SHORT,6)` — 8 bytes, not 40 — then
  `TokenNo(LONG,8)`, `CONTRACT_DESC_TR(STRUCT 26,12)`, and a trimmed selection of the full
  `MS_OE_REQUEST` fields ending in `NnfField`, `PAN`, `AlgoID`, `Reserved`. The document
  explicitly flags mixed packing for this one: **"Use pragma pack(2). Use pragma pack(1) for
  ADDITIONAL_ORDER_FLAGS"** (p.298) — a packing exception found nowhere else in the document.
- `MS_OM_REQUEST_TR` (Table 138, p.302-303), 186 bytes — the trimmed modify/cancel, transaction
  codes `ORDER_MOD_IN_TR (20040)`, `ORDER_CANCEL_IN_TR (20070)`,
  `TRIMMED_ORDER_MOD_ACK_IN (20402)`, `TRIMMED_ORDER_CANCEL_ACK_IN (20404)`,
  `ORDER_QUICK_CANCEL_IN_TR (20060)`. Same 8-byte-prefix pattern (with `Modified/CancelledBy`
  in place of `ReasonCode` at offset 6).
- `MS_OE_RESPONSE_TR` (Table 139, p.305-307), 240 bytes, codes `ORDER_CONFIRMATION_TR (20073)`,
  `ORDER_MOD_CONFIRMATION_TR (20074)`, `ORDER_CXL_CONFIRMATION_TR (20075)`. Its own even more
  compact header: `TransactionCode(SHORT,0), LogTime(LONG,2), UserId(LONG,6), ErrorCode(SHORT,10),
  TimeStamp1(LONGLONG,12), TimeStamp2(CHAR,1,20)` — note `TimeStamp2` is **one byte**, not the
  usual eight — 22 bytes total, not 40.
- `MS_TRADE_CONFIRM_TR` (Table 140, p.308-310), 230 bytes, code `TRADE_CONFIRMATION_TR (20222)`.
  Its own compact header again: `TransactionCode(SHORT,0), LogTime(LONG,2), TraderId(LONG,6),
  Timestamp(LONGLONG,10), Timestamp1(DOUBLE,18), Timestamp2(DOUBLE,26)` — 34 bytes, and notably
  `Timestamp1`/`Timestamp2` are `DOUBLE` here, not `CHAR(8)` as in the standard `MESSAGE_HEADER`.

The transaction-code naming pattern for the "_TR" family (from the appendix, p.276-278; verified
against the four values the nanosecond-timestamp list gives directly, p.278-279) is: take the
original four-digit code and insert a `0` after its first digit — `2000→20000`, `2040→20040`,
`2070→20070`, `2073→20073`, `2074→20074`, `2075→20075`, `2222→20222`. This is descriptive of the
pattern observed, not a rule stated in the document; every individual value used in this
transcription was read from the table, not derived.

**That recommendation was wrong, and a real gateway proved it.** Phase 1 scoped the
whole `_TR` family out as an optional low-latency extra; it is not optional. Chapter 11
says "Only Trim-NNF protocol is supported by Direct Interface" (p.216), and the client
under test sends order entry in `MS_OE_REQUEST_TR` and nothing else — so refusing these
codes refused the venue's entire order flow. They are served now: `venues/nsefo/layouts.py`
defines all four structures with `HeaderLayout`s of their own, and a client is answered in
whichever encoding it asked in.

What remains out of scope is the narrower thing this section conflated with them: the
**immediate-ack feature** of Chapter 15 (`TRIMMED_*_ACK_IN` 20400/20402/20404 and the
22-byte `MS_ACK_RESPONSE`), which the document puts on a separate Gateway Router port and
channel (p.252) — a second listener rather than a second structure.

One thing the appendix does not publish is a trimmed equivalent of `ORDER_ERROR (2231)`,
`ORDER_MOD_REJECT (2042)` or `ORDER_CANCEL_REJECT (2072)`. Since `MS_OE_RESPONSE_TR` carries
both an `ErrorCode` and a `ReasonCode`, a refusal is taken to be the confirmation code with a
non-zero error; see `rules.TRIMMED_RESPONSES`, which records it as an ASSUMPTION.

### 1.10 The Gateway Router / box connection — identical to CM

`GR_REQUEST (2400)` / `GR_RESPONSE (2401)` (Table under "Gateway Router Request/Response",
p.211-214), `SECURE_BOX_REGISTRATION_REQUEST_IN/OUT (23008/23009)` (p.214-215),
`BOX_SIGN_ON_REQUEST_IN (23000)` (p.215), `HEARTBEAT (23506)` (p.217) are **byte-for-byte
identical** to CM's `layouts.py` (same field names, types and offsets, including the 136-byte
"new encryption" `GR_RESPONSE` with `StaticCryptographicIV(8)@108`,
`DynamicCryptographicIV(LONGLONG,8)@116`, `CryptographicAdditionalKey(12)@124`). Chapter 10's
encryption methodology (p.204-207: TLS 1.3 to the Gateway Router, AES-256-GCM with a static+
dynamic IV that increments before encryption and decrements before decryption) is described in
language matching CM's own chapter closely enough that `nnf/crypto.py` and the `tls/` package
should need **no F&O-specific changes** — this is shared infrastructure, not a new dialect
concern.

Two things differ from what `layouts.py` currently defines for CM:

- **`BOX_SIGN_ON_REQUEST_OUT` disagrees on its own length between two tables in this document.**
  The detailed structure table (p.216) gives `BoxId(SHORT,2)@40` + `Reserved(CHAR,10)@42` = 52
  bytes, matching CM's `layouts.py` exactly. The Appendix transaction-code summary table
  (p.278) gives the size as **54** bytes for the same structure/code. Per the same rule CM's own
  `layouts.py` docstring states ("the detailed table wins"), 52 is used here, and the
  disagreement is recorded again in §6.
- **`BOX_SIGN_OFF (20322)`, 42 bytes** (p.217-218 detailed table; p.278 appendix, code printed as
  `2032\|2` = 20322): `MESSAGE_HEADER(40)@0` + `BoxId(SHORT,2)@40`. Sent by the exchange when it
  terminates a box connection, carrying an error code explaining why. This transaction code does
  **not** appear in CM's `transactions.py` at all — it is new plumbing for F&O (or, if it also
  exists in CM's own spec, CM's implementation simply doesn't use it yet). Worth adding either
  way, since "how the box learns why it was disconnected" is otherwise unanswered.

### 1.11 Logoff — same codes, and one three-way size disagreement

`SIGN_OFF_REQUEST_IN (2320)` (p.54-55) is the bare 40-byte header, matching CM.

`SIGN_OFF_REQUEST_OUT (2321)` — **the document disagrees with itself twice over.** The table
caption on p.55 (Table 25, "SIGNOFF_OUT") states "Packet Length 40 bytes", but the field rows
printed directly under that caption are `MESSAGE_HEADER(40)@0`, `UserId(LONG,4)@40`,
`Reserved(CHAR,145)@44` — which sum to 40+4+145 = **189** bytes, not 40. The Appendix
transaction-code summary table (p.272) gives this same structure/code a *third* number: **190**
bytes. 189 is odd; padding it to an even number per the Chapter 2 rule gives exactly 190,
matching the appendix. **Best reading: the "40 bytes" caption on p.55 is a stale copy-paste from
the empty-header convention used elsewhere in the document (see CM's own `SIGN_OFF_REQUEST_OUT`,
which genuinely is empty) and should be disregarded; the structure is `UserId(LONG,4)@40` +
`Reserved(CHAR,146)@44` = 190 bytes**, with the one-byte discrepancy between 145 and 146 being
the same odd-to-even pad seen elsewhere. This is transcribed as an assumption, not a certainty —
see §6.

This is a genuine difference from CM, which sends an empty-header `SIGN_OFF_REQUEST_OUT`; F&O's
carries the user ID and (per p.36's `EndTime` discussion, which is about logon, not logoff — no
field-level description of this `UserId`/`Reserved` payload is given anywhere in Chapter 3 for
the logoff response) an undocumented 146-byte reserved payload. Flagged under §6, **Not found**.

### 1.12 MS_TRADE_INQ_DATA — trade modification/cancellation (out of scope structure, captured for reference)

Table 31, p.83-84. **234 bytes.** Trade Modification (`TRADE_MOD_IN (5445)`) and Trade
Cancellation (`TRADE_CANCEL_IN (5440)` / `TRADE_CANCEL_OUT (5441)`) both use it; `TRADE_ERROR
(2223)` is the shared rejection code for both flows.

| Field | Type | Size | Offset |
|---|---|---|---|
| MESSAGE_HEADER | STRUCT | 40 | 0 |
| TokenNo | LONG | 4 | 40 |
| CONTRACT_DESC | STRUCT | 28 | 44 |
| FillNumber | LONG | 4 | 72 |
| FillQuantity | LONG | 4 | 76 |
| FillPrice | LONG | 4 | 80 |
| MktType | CHAR | 1 | 84 |
| BuyOpenClose | CHAR | 1 | 85 |
| Reserved | LONG | 4 | 86 |
| BuyBrokerId | CHAR | 5 | 90 |
| SellBrokerId | CHAR | 5 | 95 |
| TraderId | LONG | 4 | 100 |
| RequestedBy | CHAR | 1 | 104 |
| SellOpenClose | CHAR | 1 | 105 |
| BuyAccountNumber | CHAR | 10 | 106 |
| SellAccountNumber | CHAR | 10 | 116 |
| Reserved | CHAR | 24 | 126 |
| ReservedFiller | CHAR | 2 | 150 |
| Reserved | CHAR | 2 | 152 |
| BuyPAN | CHAR | 10 | 154 |
| SellPAN | CHAR | 10 | 164 |
| Reserved | CHAR | 60 | 174 |

Total: 174 + 60 = **234**, matches. This is genuinely out of scope for Phase 1 (see §7), but is
transcribed here because it shares `CONTRACT_DESC` and follows the same rules, and because
Phase 1 needs a real error code, not a guess, to refuse it (§7).

---

## 2. Enumerations

### 2.1 Market Types (p.281)

| ID | Meaning |
|---|---|
| 1 | Normal Market |
| 2 | Odd Lot Market (Not used) |
| 3 | Spot Market (Not used) |
| 4 | Auction Market (Not used) |

Only 4 market types in F&O against CM's richer set — F&O's own appendix marks three of the four
as "Not used" outright.

### 2.2 Market Status (p.281)

| ID | Meaning |
|---|---|
| 0 | PreOpen (Only for Normal Market) |
| 1 | Open |
| 2 | Closed |
| 3 | PreOpen Ended |
| 4 | Postclose |

**F&O has a fifth status, `4 Postclose`, that CM does not.** Confirmed by the Quick Reference
table (p.280): a Postclose session accepts only market orders in RL/ST book types, "Close: Order
entry is not allowed." This is a genuine additional trading phase relative to CM's Normal/
PreOpen/Open/Closed cycle.

### 2.3 Book Types (p.281-282)

| Book ID | Book Type | Market Type |
|---|---|---|
| 1 | Regular lot order | Normal Market |
| 2 | Special terms order | Normal Market |
| 3 | Stop loss / MIT order | Normal Market |
| 4 | Negotiated order (Not used) | Normal Market |
| 5 | Odd lot order (Not used) | Odd Lot Market |
| 6 | Spot order (Not used) | Spot Market |
| 7 | Auction order (Not used) | Auction Market |

Book 3 combines Stop Loss *and* Market-If-Touched under one book ID (they are distinguished by
the `SL`/`MIT` bits of `ST_ORDER_FLAGS`, §1.3, not by book type) — unlike CM, which has no book
ID conflation of this kind. Only Book 1 (Regular Lot) is in scope.

### 2.4 Security/Contract Status (p.282)

| ID | Status |
|---|---|
| 1 | Preopen |
| 2 | Open |
| 3 | Suspended |
| 4 | Preopen Extended |
| 5 | Open With Market |
| 6 | Price Discovery |

Not directly comparable to CM's per-market status enum (§2.2 above is the per-market one this
maps closer to); this is a per-*security* status, analogous to CM's per-security eligibility/
suspension state but with more values (`Open With Market`, `Price Discovery` have no CM
equivalent, both relating to F&O opening-price-discovery mechanics).

### 2.5 Activity Types (p.282-283)

Full published table (code : name : description), verbatim:

| Code | Name | Description |
|---|---|---|
| 1 | ORIGINAL_ORDER | Order entered (also GTC/GTD orders still in the book) |
| 2 | ACTIVITY_TRADE | The trade done |
| 3 | ACTIVITY_ORDER_CXL | The order is cancelled |
| 4 | ACTIVITY_ORDER_MOD | The order is modified |
| 5 | ACTIVITY_TRADE_MOD | The trade is modified |
| 6 | ACTIVITY_TRADE_CXL_1 | Trade cancellation was requested |
| 7 | ACTIVITY_TRADE_CXL_2 | Action taken on the cancellation request |
| 8 | ACTIVITY_BATCH_ORDER_CXL | End-of-day cancellation of un-traded Day/GTC/GTD orders |
| 9 | ACTIVITY_ORDER_MOD_REJECT | Order modification rejected |
| 10 | ACTIVITY_TRADE_MOD_REJECT | Trade modification rejected |
| 11 | ACTIVITY_TRADE_CXL_REJECT | Trade cancellation rejected |
| 12 | ACTIVITY_ORDER_REJECTED | Order entry rejected |
| 13 | ACTIVITY_ORDER_IN_BOOK | (description not printed on p.283) |
| 14 | ACTIVITY_ORDER_CXL_REJECT | Order cancel request rejected |
| 15 | ACTIVITY_PRICE_FREEZE_IN | Order entered, caused price freeze |
| 16 | ACTIVITY_PRICE_FREEZE_CXLD | Order in price freeze cancelled from CWS |
| 17 | ACTIVITY_FREEZE_ADMIN_SUSP | Order rejected via admin suspension while frozen |
| 18 | ACTIVITY_QTY_FREEZE_IN | Order entered, caused quantity freeze |
| 19 | ACTIVITY_QTY_FREEZE_CXLD | Order in quantity freeze cancelled from CWS |
| 20 | ACTIVITY_ORD_BROKER_SUSP | Order cancelled due to broker suspension |
| 43 | ACTIVITY_SPREAD_TRADE_CXL | Spread trade cancelled |

This is the venue's own `ActivityType` field on `MS_TRADE_CONFIRM` (§1.6). CM has an analogous
concept but keyed differently; this table is F&O's own enumeration in full.

### 2.6 CONTRACT_DESC value domains (repeated from §1.2 for completeness)

- `InstrumentName`: `FUTIDX`, `FUTSTK`, `OPTIDX`, `OPTSTK` (p.249).
- `OptionType`: `CE` (call), `PE` (put), `XX` (futures — meaningless but populated) (p.249-250).

### 2.7 BuySell, ProClient, Order Terms/Attributes, ModCxlBy

- `BuySellIndicator` — 1/2 for Buy/Sell, values not spelled out on the page captured but referred
  to identically to CM's convention ("This field should specify whether the order is a buy or
  sell," p.65, with the value table itself elided by the extractor — cross-checked against CM's
  `BuySell.BUY="1"`/`SELL="2"` and F&O's own tag reuse note in §5, this is assumed identical and
  not independently re-derived from this document; flagged under §6).
- `Pro/ClientIndicator` — "one of the following values to specify whether the order is entered on
  behalf of a broker or a trader" (p.67); values elided by the extractor the same way. Assumed
  identical to CM's `ProClient.CLIENT="1"`/`PRO="2"` pending confirmation — §6.
- `Modified/CancelledBy` — printed on p.64 and p.74 as "should take one of the following values,"
  values elided by the extractor both times. CM's own four-valued `ModCxlBy`
  (`Trader/BranchManager/CorporateManager/Exchange`, coded `T/B/M/E`) is a plausible match given
  identical user-role vocabulary elsewhere in this document (Corporate Manager, Branch Manager,
  Dealer — p.9-10's abbreviation list), but is **not independently confirmed** — §6.
- Order Terms/Attributes — see §1.3 (folded into the `ST_ORDER_FLAGS` discussion since the two
  are the same table).
- `UserType` (p.36, values elided): CM's `UserType` is `CORPORATE_MANAGER=0`, `BRANCH_MANAGER=1`,
  `DEALER=2` — F&O's abbreviation list (p.9-10) names the same three roles (CM, BM, DL) with no
  reason to expect different codes, but again **not independently confirmed by a printed value
  table** in the pages read — §6.

---

## 3. Error-code table

Full published table, Appendix "List of Error Codes" (p.256-269), transcribed verbatim (ID :
value : description). **266 rows** as counted below (some symbolic IDs are lower-case
`e$...`/`E$...` conventions used directly in the source system rather than the upper-case
`ERR_.../OE_...` style CM's own table favours — both styles are used inconsistently within this
one document, and are preserved exactly as printed rather than normalised).

| Symbolic ID | Value | Description |
|---|---|---|
| INVALID_INSTRUMENT_TYPE | 293 | Invalid instrument type |
| ORDER_NUMBER_INVALID | 509 | Order does not exist |
| ORD_CXL_INITIATOR_AUC_NOT_ALLOWED | 8049 | Initiator not allowed to cancel auction order |
| AUCTION_NUMBER_INVALID | 8485 | Auction number does not exist |
| MARKET_CLOSED | 16000 | The trading system is not available for trading |
| e$invalid_user | 16001 | Header user ID is not equal to user ID in the order packet |
| ERROR_BAD_TRANS_CODE | 16003 | Invalid Transcode |
| E$user_already_signed_on | 16004 | The user is already signed on |
| E$invalid_signoff | 16005 | System error while trying to sign-off |
| E$invalid_signon | 16006 | Invalid Box/User sign-on |
| e$signon_not_possible | 16007 | Signing onto the trading system is restricted |
| ERR_INVALID_SYMBOL | 16012 | Invalid Symbol |
| ERR_INVALID_ORDER_NUMBER | 16013 | Invalid order number |
| e$not_your_order | 16014 | This order is not yours |
| E$not_your_fill | 16015 | This trade is not yours |
| E$invalid_fill_number | 16016 | Invalid trade number |
| E$stock_not_found | 16019 | Stock not found |
| e$order_price_out_of_revised_price_range | 16020 | Order price is outside the revised price range |
| SECURITY_NOT_AVAILABLE | 16035 | Security unavailable for trading at this time |
| BROKER_NOT_FOUND | 16041 | Trading member does not exist |
| USER_NOT_FOUND | 16042 | Dealer does not exist |
| DUPLICATE_RECORD | 16043 | This record already exists |
| e$order_modified | 16044 | Order has been modified, please try again |
| STOCK_SUSPENDED | 16049 | Stock is suspended |
| ERR_FUNCTION_NOT_AVAILABLE | 16052 | Function not available |
| e$change_password | 16053 | Your password has expired, must be changed |
| ERR_INVALID_BRANCH | 16054 | Invalid branch for trading member |
| PREOPEN_TRADE_CANCELLATION_NOT_ALLOWED | 16055 | Trade executed during pre-open not allowed to cancel |
| OE_PROGRAM_ERROR | 16056 | Program error |
| ERR_INVALID_STATUS | 16063 | Requested user status is active |
| ERR_DATA_NOT_CHANGED | 16070 | Incoming packet data same as existing data |
| e$dup_trd_cxl_request | 16086 | Duplicate trade cancel request |
| ERR_INVALID_BUYER_USER_ID | 16098 | Invalid trader ID for buyer |
| ERR_INVALID_SELLER_USER_ID | 16099 | Invalid trader ID for seller |
| e$invalid_version | 16100 | System version has not been updated |
| OE_SYSTEM_ERROR | 16104 | System could not complete the transaction |
| ERR_USER_DISABLED | 16134 | Dealer is disabled |
| OE_INVALID_STOCK_STATUS | 16145 | Security not eligible to trade in Preopen |
| ERR_INVALID_USER_ID | 16148 | Invalid Dealer ID |
| ERR_INVALID_TRADER_ID | 16154 | Invalid Trader ID |
| OE_ATO_IN_OPEN | 16169 | ATO order cannot be entered when security is open |
| e$dup_request | 16198 | Duplicate modify/cancel request for same trade |
| e$only_cp_allowed | 16227 | Only market orders allowed in postclose |
| e$sl_mit_nt_not_allowed_pclose | 16228 | SL, MIT or NT orders not allowed during Postclose |
| e$gtc_gtd_ord_not_allowed_pclose | 16229 | GTC or GTD orders not allowed during Postclose |
| OE_CONT_MOD_NOT_ALLOWED | 16230 | Continuous-session orders cannot be modified |
| TRD_CONT_MOD_NOT_ALLOWED | 16231 | Continuous-session trades cannot be changed |
| STR_PRO_PARTIVIPANT_INVALID | 16233 | Proprietary requests cannot name a participant |
| ERROR_INVALID_PRICE | 16247 | (description not printed) |
| OE_DIFF_TRD_MOD_VOL | 16251 | Trade modification with different quantities received |
| ERROR_USER_NOT_EXISTS_IN_SYSTEM | 16260 | User does not exist in system |
| ERR_ALREADY_DELETED | 16264 | User or Branch is deleted |
| RECORD_NOT_FOUND | 16273 | Record does not exist |
| OE_MARKETS_CLOSED | 16278 | Markets have not been opened for trading |
| OE_SECURITY_NOT_ADMITTED | 16279 | Contract not yet admitted for trading |
| OE_SECURITY_MATURED | 16280 | Contract has matured |
| OE_SECURITY_EXPELLED | 16281 | Security has been expelled |
| OE_ISSUED_CAP_EXCEEDS | 16282 | Order quantity greater than issued capital |
| OE_PRICE_NOT_MULT | 16283 | Order price not a multiple of tick size |
| OE_PRICE_EXCEEDS_DAY_MIN_MAX | 16284 | Order price exceeds the day's min/max range |
| OE_IS_NOT_ACTIVE | 16285 | Broker is not active |
| e$system_wrong_state | 16300 | System in wrong state for the requested change |
| OE_AUCTION_PENDING | 16303 | Auction is pending |
| OE_QTY_FREEZE_CAN | 16307 | Order cancelled due to quantity freeze |
| OE_PRICE_FREEZE_CAN | 16308 | Order cancelled due to price freeze |
| OE_SOL_PERIOD_OVER | 16311 | Solicitor period for the auction is over |
| OE_COMP_PERIOD_OVER | 16312 | Competitor period for the auction is over |
| OE_AUC_PERIOD_GREATER | 16313 | Auction period would cross market close time |
| OE_LIMIT_TRIGGER | 16315 | Limit price worse than trigger price |
| OE_TRIGGER_PRICE_NOT_MULT | 16316 | Trigger price not a multiple of tick size |
| OE_NO_AON_ATTRIB | 16317 | AON attribute not allowed |
| OE_NO_MF_ATTRIB | 16318 | MF attribute not allowed |
| OE_NO_AON_IN_ATTRIB1 | 16319 | AON not allowed at security level |
| OE_NO_MF_ATTRIB1 | 16320 | MF not allowed at security level |
| OE_MF_GREATER_DISC | 16321 | MF quantity greater than disclosed quantity |
| OE_MF_NOT_MULT | 16322 | MF quantity not a multiple of regular lot |
| OE_MF_GREATER_ORIGINAL | 16323 | MF quantity greater than original quantity |
| OE_DISC_GREATER_ORIGINAL | 16324 | Disclosed quantity greater than original quantity |
| OE_DISC_NOT_MULT | 16325 | Disclosed quantity not a multiple of regular lot |
| OE_GTD_GREATER | 16326 | GTD greater than that specified at the trading system |
| OE_QUANTITY_GERATER_RL | 16327 | Odd lot quantity ≥ regular lot size |
| OE_QUANTITY_NOT_MULT_RL | 16328 | Quantity not a multiple of regular lot |
| OE_BROKER_NOT_PERMITTED | 16329 | Trading member not permitted in the market |
| OE_IS_SUSPENDED | 16330 | Security is suspended |
| OE_BRANCH_LIMIT_EXCEEDED | 16333 | Branch order value limit exceeded |
| OE_ORD_CAN_CHANGED | 16343 | The order to be cancelled has changed |
| OE_ORD_CANNOT_CANCEL | 16344 | The order cannot be cancelled |
| OE_INIT_ORD_CANCEL | 16345 | Initiator order cannot be cancelled |
| OE_ORD_CANNOT_MODIFY | 16346 | Order cannot be modified |
| ERR_TRADING_NOT_ALLOWED | 16348 | Trading not allowed in this market |
| OE_NT_REJECTED | 16357 | Control has rejected the Negotiated Trade |
| CHG_ST_EXISTS | 16363 | Status already in the required state |
| OE_SECURITY_IN_PREOPEN | 16369 | Contract is in preopen |
| OE_INQ_NOT_ALLOWED | 16372 | Order entry not allowed for an inquiry user |
| OE_SECURITY_INELIGIBLE | 16387 | Contract not allowed to trade |
| e$fok_order_cancelled | 16388 | Preopen/normal-market/IOC unmatched order cancelled by the system |
| TURNOVER_LIMIT_NOT_PROVIDED | 16392 | Turnover limit not provided |
| ERR_CANNOT_MOD_AUC_ORDER | 16397 | Cannot modify Auction orders |
| OE_MAX_DQ_ALLOWED | 16400 | DQ less than minimum quantity allowed |
| OE_ADMIN_SUSP_CAN | 16404 | Order cancelled due to freeze admin suspension |
| e$invalid_buy_sell_type | 16405 | Buy/Sell type entered is invalid |
| e$invalid_book_type | 16406 | Book type entered is invalid |
| e$invalid_trigger_price | 16408 | Trigger price has invalid characters |
| e$invalid_pro_client | 16414 | Pro/Client should be either 1 (client) or 2 (broker) |
| e$invalid_instructions | 16415 | Invalid combination of book type and order type |
| e$invalid_order_parameters | 16416 | Invalid order parameters |
| e$nnf_req_exceeded | 16418 | Number of NNF requests exceeded |
| INVALID_ORDER | 16419 | Invalid data in the order packet |
| ERR_BOX_RATE_EXCEEDED_AT_MILLISECOND_LEVEL | 16420 | Box rate exceeded at millisecond level |
| e$gtd_gt_maturity | 16440 | GTD greater than maturity date |
| DQ_NOT_ALLOWED_IN_PREOPEN | 16441 | DQ orders not allowed in preopen |
| ST_ORD_NOT_ALLOWED_POPEN | 16442 | ST orders not allowed in preopen |
| e$ord_lim_exceeds_ord_val_lim | 16443 | Order value exceeds the order limit value |
| ERR_USR_ORD_VALUE_LIMIT_EXCEEDED | 16444 | User order value limit exceeded |
| SL_NOT_ALLOWED | 16445 | Stop Loss orders not allowed |
| MIT_NOT_ALLOWED | 16446 | Market If Touched orders not allowed |
| E$ord_not_allowed_in_preopen | 16447 | Order entry not allowed in Preopen |
| ERROR_SL_LMT_RSNBLTY_CHECK | 16448 | Limit/trigger price difference beyond permissible range |
| e$not_modifiable | 16514 | Not modifiable |
| e$tm_cm_does_not_exist | 16518 | Clearing member / trading member link not found |
| e$not_clg_mem | 16521 | Not a clearing member |
| e$user_not_corp_mgr | 16523 | User is not a corporate manager |
| e$pm_cm_invalid | 16532 | Clearing member / participant link not found |
| e$corp_mgr_vu_mod | 16533 | Enter either Trading Member or participant |
| e$invalid_participant | 16541 | Participant is invalid |
| e$trade_approved_by_cm | 16550 | Trade already approved by CM, cannot modify/cancel |
| e$cm_stock_suspended | 16552 | Stock has been suspended |
| e$broker_not_permitted_in_fut | 16554 | Trading member not permitted in futures |
| e$broker_not_permitted_in_opt | 16555 | Trading member not permitted in options |
| e$qty_less_than_min_lot | 16556 | Quantity less than the minimum lot size |
| e$disc_qty_less_than_min_lot | 16557 | Disclosed quantity less than the minimum lot size |
| e$mf_qty_less_than_min_lot | 16558 | Minimum fill less than the minimum lot size |
| e$already_rejected | 16560 | The give-up trade has already been rejected |
| e$nt_orders_not_allowed | 16561 | Negotiated orders not allowed |
| e$nt_trade_not_allowed | 16562 | Negotiated trade not allowed |
| e$inconsistent_broker_branch | 16566 | User does not belong to the broker or branch |
| M$post_close_start | 16570 | The market is in post-close |
| M$post_close_ended | 16571 | The closing session has ended |
| M$post_close_trades | 16572 | Closing session trades have been generated |
| e$invalid_msg_length | 16573 | Message length is invalid |
| e$invalid_open_close_type | 16574 | Open-Close type entered is invalid |
| e$nnf_inq_req_exceeded | 16576 | Number of NNF inquiry requests exceeded |
| e$participant_and_volume_changed | 16577 | Both participant and volume changed |
| e$invalid_cover_uncover_type | 16578 | Cover-Uncover type entered is invalid |
| e$illegal_participant | 16580 | Order does not belong to the given participant |
| e$invalid_fill_price | 16581 | Invalid trade price |
| e$pro_no_participant | 16583 | Pro order may not name a participant |
| e$invalid_account_no | 16585 | Not a valid account number |
| e$allow_no_participant_order | 16586 | Participant order entry not allowed |
| M$delete_all_orders | 16589 | All continuous-session orders being deleted now |
| e$cum_ur_ord_val_limit_exceeded | 16597 | Branch limit should exceed the sum of user limits |
| e$branch_ord_val_limit_exceeded | 16598 | Branch limit should exceed the used limit |
| ERR_ORD_VAL_EXCEEDED | 16600 | Order value exceeds maximum permissible limit |
| ERR_PREOPEN_ORDER_REJECT | 16601 | Request rejected by the exchange |
| e$dealer_value_limit_exceeds | 16602 | Dealer value limit exceeds the set limit |
| e$participant_not_found | 16604 | Participant not found |
| e$either_leg_failed | 16605 | One leg of a spread/2L order failed |
| e$qty_greater_than_freeze_qty | 16606 | Quantity greater than freeze quantity |
| e$spread_not_allowed | 16607 | Spread not allowed |
| e$spread_allowed_if_stock_open | 16609 | Spread allowed only when stock is open |
| e$qty_should_be_same | 16610 | Both legs should have the same quantity |
| e$ord_mod_qty_frz_not_allowed | 16611 | Modified order quantity freeze not allowed |
| e$trade_rec_modified | 16612 | The trade record has been modified |
| e$tm_order_cant_be_modified | 16615 | Order cannot be modified |
| e$tm_order_cant_be_cancelled | 16616 | Order cannot be cancelled |
| e$tm_trade_cant_be_manipulated | 16617 | Trade cannot be manipulated |
| e$cm_of_tm_suspended | 16625 | Clearing member is suspended |
| e$expdate_not_in_ascending_ord | 16626 | Expiry date not in ascending order |
| e$invalid_contract_comb | 16627 | Invalid contract combination |
| e$bm_cannot_cancel_cm_orders | 16628 | Branch manager cannot cancel corporate manager's orders |
| e$bm_cannot_cancel_bm_orders | 16629 | Branch manager cannot cancel another branch manager's orders |
| e$cm_cannot_cancel_cm_orders | 16630 | Corporate manager cannot cancel another corporate manager's orders |
| e$spread_in_different_underlying | 16631 | Spread not allowed for different underlyings |
| e$invalid_cli_ac | 16632 | Client A/C number cannot be modified as a trading member ID |
| e$br_ord_limit_fut_buy_exceeded | 16636 | Futures buy branch order value limit exceeded |
| e$br_ord_limit_fut_sell_exceeded | 16637 | Futures sell branch order value limit exceeded |
| e$br_ord_limit_opt_buy_exceeded | 16638 | Options buy branch order value limit exceeded |
| e$br_ord_limit_opt_sell_exceeded | 16639 | Options sell branch order value limit exceeded |
| e$ur_ord_limit_fut_buy_exceeded | 16640 | Futures buy used limit exceeds user limit |
| e$ur_ord_limit_fut_sell_exceeded | 16641 | Futures sell used limit exceeds user limit |
| e$ur_ord_limit_opt_buy_exceeded | 16642 | Options buy used limit exceeds user limit |
| e$ur_ord_limit_opt_sell_exceeded | 16643 | Options sell used limit exceeds user limit |
| e$cant_appr_bhav_copy_generated | 16645 | Cannot approve, Bhavcopy already generated |
| e$Collateral_Lmt_Chk | 16646 | Cannot modify |
| e$address_not_found | 16656 | No address in the database |
| e$stk_in_popen | 16662 | Contract is opening, wait for it to open |
| e$invalid_nnf_field | 16666 | Invalid NNF field |
| e$gtcgtd_not_allowed | 16667 | GTC/GTD orders not allowed |
| ERR_USER_ALREADY_SIGNED_OFF | 16683 | User has already signed off |
| ERR_NO_PRIVILEGE | 16684 | User has no authority for the requested change |
| CLOSEOUT_ORDER_REJECT | 16686 | Closeout order rejected by the system |
| CLOSEOUT_FRZ_REJECT | 16687 | Closeout order would go into freeze (not allowed) |
| CLOSEOUT_NOT_ALLOWED | 16688 | Closeout order not allowed in the system |
| CLOSEOUT_TRDMOD_REJECT | 16690 | Trade-mod request by a broker in closeout |
| PARTIAL_ORDER_REJECT | 16706 | Cancelled by the system |
| PARTIAL_QUICK_ORDER_CXL_REJ | 16708 | Orders not completely cancelled by the system |
| ERROR_INVALID_SPRD_COMBINATION | 16711 | Spread order has an invalid combination |
| e$price_diff_out_of_range | 16713 | Price difference beyond operating range |
| RMS_REJECTED_IN_PREOPEN | 16725 | Order entry/modification rejected by the Exchange |
| ERROR_ALGOID_NNFID_MISMATCH_1 | 16730 | NNF id/Algo id mismatch — Algo ID is 0 in order request |
| ERROR_ALGOID_NNFID_MISMATCH_2 | 16731 | NNF id/Algo id mismatch — non-algo order must have Algo ID 0 |
| ERROR_ALGO_MKT_NOT_ALLOWED | 16732 | Market order not allowed for Algo order |
| ERROR_INVALID_NNF_ID | 16733 | Invalid NNF Id |
| ERROR_PREOPN_ATO_MOD_CAN_REJ | 16749 | Modification/cancellation of ATO orders not currently allowed |
| ERROR_PREOPN_ATO_NOT_ALLOWED | 16752 | ATO orders not currently allowed in preopen |
| ERR_USR_NOT_FOUND_IN_NNF_FILE | 16778 | User is not an NNF user |
| e$vc_order_rejected | 16793 | Order entered has invalid data |
| e$ssd_order_rejected | 16794 | Order entered has invalid data |
| e$order_cancelled_for_vc | 16795 | Order cancelled due to voluntary closeout |
| e$order_cancelled_for_ssd | 16796 | Order cancelled due to OI violation |
| MSG_CODE_VOLUNTARY_CLOSE_OUT_STATUS | 16797 | Broker is in Voluntary Closeout |
| MSG_CODE_SUSPENDED_STATUS | 16798 | Broker is Suspended |
| e$bo_price_out_of_range | 16803 | Bulk order rejected due to price freeze |
| e$bo_excess_quantity | 16804 | Bulk order rejected due to quantity freeze |
| e$user_ineligible_for_bulk_orders | 16805 | Trader not eligible for bulk order |
| e$user_not_allowed_for_regular | 16806 | Trader allowed to enter only bulk orders |
| e$account_debarred | 16807 | Account disabled from trading (SEBI/statutory direction) |
| e$account_debarred_by_pit | 16816 | Account disabled for the scrip during trading-window closure (SEBI PIT) |
| ERR_USR_ALREADY_UNLCKED | 16810 | User is already unlocked |
| ERR_DUPLICATE_UNLCK_ALRT | 16811 | Unlock request already present for this user |
| ERR_ACTV_NUM_OF_USRS_IN_BRNCH_EXCEEDED | 17022 | Active number of users in branch exceeded |
| EC_TRD_MOD_REJ_CLI_CP_MOD_NOT_ALLOWED | 17039 | Client code / participant modification not allowed |
| ERROR_QUANTITY_LIM_EXCEEDS_QTY_VAL_LIM | 17045 | Order quantity exceeds the user's quantity value limit |
| USER_TRD_MOD_DISABLED | 17046 | Trade modification not allowed for the user |
| ERR_DEPNDENT_SESSN_NOT_ACTIVE | 17063 | Dependent session is not active |
| e$trd_price_out_of_stock_tpp / e$trd_price_out_of_stock_lpp | 17070 | Price outside the current execution LPP range |
| e$order_cancelled_for_self_trade | 17071 | The order could have resulted in a self-trade |
| e$invalid_packet | 17101 | The packet has invalid data |
| e$hearbeat_not_received | 17102 | Heartbeat not received |
| e$Invalid_box_id | 17104 | Invalid box id |
| e$seq_no_mismatch | 17105 | Sequence number mismatch |
| e$box_rate_exceeded | 17106 | Box rate exceeded by the member |
| ERROR_HB_RATE_EXCEEDED | 17107 | Heartbeat rate exceeded by the member |
| e$max_user_count_exceeded | 17142 | Maximum user login per box exceeded |
| e$invalid_box_ip_combination | 16403 | Login from an invalid IP |
| ERR_INVALID_PAN_ID | 17177 | Invalid PAN Id |
| ERR_INVALID_ALGO_ID | 17179 | Invalid Algo Id |
| ERR_MKT_ORDER_NOT_ALLOWED | 17181 | Contract not traded; market order not allowed |
| ERR_TRADE_BEYOND_MARKUP_PRICE | 17182 | Order could result in a trade beyond the mark-up price |
| ERR_USER_HAVING_NULL_RIGHTS | 17184 | User has no trading rights |
| ERR_ALGO_ID_DISABLED | 17185 | Order rejected: Algo ID disabled by the Exchange |
| ERR_ORDER_CANCELLED_ALGOID_DISABLED | 17186 | Order cancelled: Algo ID disabled by the Exchange |
| ERR_INVALID_VALUE_IN_RESERVED | 17180 | Invalid value in a Reserved field |
| ERR_CHECKSUM_FAILED_GR | 19028 | Checksum verification failed at Gateway Router |
| ERR_MULTIPLE_GR_QUERY_RCV | 19029 | Multiple GR_QUERY requests received |
| ERR_ENCRYPTION_FLAG_MISMATCH | 19030 | Encryption flag mismatch |
| ERR_MD5_CHECKSUM_FAILURE | 19031 | MD5 checksum failed |

**266 rows** transcribed above (p.256-269), one description left blank where the source page
itself printed none (`ERROR_INVALID_PRICE`, `ACTIVITY_ORDER_IN_BOOK` in §2.5).

**Codes CM's own `transactions.py:ERROR_CODES` also has**, cross-checked by value: `16000`
(`ERR_MARKET_NOT_OPEN` in CM vs `MARKET_CLOSED` here — same value, same meaning, different
symbolic name — CM's own table already renames several NSE symbols this way, so this is expected
rather than alarming), `16001`, `16003`, `16004`, `16006`, `16007`, `16012`, `16013`, `16035`,
`16041`, `16042`, `16053`, `16054`, `16056`, `16098`, `16099`, `16100`, `16104`, `16134`,
`16148`, `16154`, `16169`, `16251`, `16273`, `16278`, `16279`, `16280`, `16281`, `16282`,
`16283`, `16284`, `16285`, `16307`, `16308`, `16311`, `16312`, `16315`, `16316`, `16317`,
`16318`, `16319`, `16320`, `16321`, `16322`, `16323`, `16324`, `16325`, `16326`, `16328`,
`16329`, `16330`, `16333`, `16348`, `16372`, `16379` (CM has this, not seen in the F&O pages
read), `16383`/`16387`/`16388`/`16392`/`16397`/`16400`/`16403`/`16404`/`16411`-`16427` (CM's
own numeric neighbourhood, several matching by value and meaning: `16403`→login-from-invalid-IP
in both, `16414`→invalid Pro/Client in both), `16493`, `16521` (different meaning in each! CM's
`16521`=`ERR_PRICE_OUTSIDE_REVISED_PRICE`, F&O's `16521`=`e$not_clg_mem`/"Not a clearing member"
— **same numeric value, different published meaning between the two protocol documents.** This
is not a structure collision but is exactly the kind of trap a shared error-label table would
walk into if CM's and F&O's `ERROR_CODES` dicts were ever merged into one; keep them separate
per venue), `16560`, `16562`, `16563`, `16567`-`16569`, `16571`, `16576`, `16577`, `16588`,
`16592`, `16598`, `16600`, `16601`, `16606`, `16700`, `16750`, `16761`, `16778`, `16910`,
`17015`, `17017`, `17022`, `17080`, `17102`, `17104`, `17105`, `17142`, `17177`, `17179`,
`17180`, `17182`, `17183`, `17184`, `19028`-`19031`. Most agree in meaning where they share a
value; `16521` is the one outright semantic collision found, and it means the two venues' error
tables must never be merged into a single lookup keyed only by numeric value.

**F&O-only codes** (not present at all in CM's table, the large majority of this list): every
code above whose symbolic name relates to Futures/Options concepts CM has no analogue for —
Algo IDs, PAN debarment, Postclose, closeout, spreads/2L/3L, give-up, branch/user order-value
limits, box-level rate limiting, and the Bhavcopy/broadcast-adjacent codes.

## 4. Transaction codes

Full appendix table transcribed from p.270-278 (`List of Transaction Codes`), restricted to
**interactive** codes (marked `I`) plus the handful of broadcast codes (`B`) needed for context;
pure market-data broadcasts are omitted as out of scope (README's existing precedent for CM).

| Transaction Code | Value | Structure | Size | I/B |
|---|---|---|---|---|
| SYSTEM_INFORMATION_IN | 1600 | MS_SYSTEM_INFO_REQ | 44 | I |
| SYSTEM_INFORMATION_OUT | 1601 | MS_SYSTEM_INFO_DATA | 106 | I |
| BOARD_LOT_IN | 2000 | MS_OE_REQUEST | 316 | I |
| NEG_ORDER_TO_BL | 2008 | MS_OE_REQUEST | 316 | I |
| NEG_ORDER_BY_CPID | 2009 | MS_OE_REQUEST | 316 | B |
| PRICE_MOD_IN / PRICE_MOD_ACK_IN | 2013 / 20406 | PRICE_MOD | 106 | I |
| ORDER_MOD_IN | 2040 | MS_OE_REQUEST | 316 | I |
| ORDER_MOD_REJECT | 2042 | MS_OE_REQUEST | 316 | I |
| ORDER_CANCEL_IN | 2070 | MS_OE_REQUEST | 316 | I |
| CANCEL_NEG_ORDER | 2076 | MS_OE_REQUEST | 316 | I |
| ORDER_CANCEL_REJECT | 2072 | MS_OE_REQUEST | 316 | I |
| ORDER_CONFIRMATION | 2073 | MS_OE_REQUEST | 316 | I |
| ORDER_MOD_CONFIRMATION | 2074 | MS_OE_REQUEST | 316 | I |
| ORDER_CANCEL_CONFIRMATION | 2075 | MS_OE_REQUEST | 316 | I |
| SP_BOARD_LOT_IN / SP_BOARD_LOT_ACK_IN | 2100 / 20408 | MS_SPD_OE_REQUEST | 480 | I |
| TWOL_BOARD_LOT_IN / ACK | 2102 / 20410 | MS_SPD_OE_REQUEST | 480 | I |
| THRL_BOARD_LOT_IN / ACK | 2104 / 20412 | MS_SPD_OE_REQUEST | 480 | I |
| SP_ORDER_CANCEL_IN / ACK | 2106 / 20414 | MS_SPD_OE_REQUEST | 480 | I |
| SP_ORDER_MOD_IN / ACK | 2118 / 20416 | MS_SPD_OE_REQUEST | 480 | I |
| SP_ORDER_CONFIRMATION | 2124 | MS_SPD_OE_REQUEST | 480 | I |
| TWOL_ORDER_CONFIRMATION | 2125 | MS_SPD_OE_REQUEST | 480 | I |
| THRL_ORDER_CONFIRMATION | 2126 | MS_SPD_OE_REQUEST | 480 | I |
| SP_ORDER_CXL_REJ_OUT | 2127 | MS_SPD_OE_REQUEST | 480 | I |
| SP_ORDER_CXL_CONFIRMATION | 2130 | MS_SPD_OE_REQUEST | 480 | I |
| TWOL_ORDER_CXL_CONFIRMATION | 2131 | MS_SPD_OE_REQUEST | 480 | I |
| THRL_ORDER_CXL_CONFIRMATION | 2132 | MS_SPD_OE_REQUEST | 480 | I |
| SP_ORDER_MOD_REJ_OUT | 2133 | MS_SPD_OE_REQUEST | 480 | I |
| SP_ORDER_MOD_CON_OUT | 2136 | MS_SPD_OE_REQUEST | 480 | I |
| TWOL_ORDER_ERROR | 2155 | MS_SPD_OE_REQUEST | 480 | I |
| THRL_ORDER_ERROR | 2156 | MS_SPD_OE_REQUEST | 480 | I |
| FREEZE_TO_CONTROL | 2170 | MS_OE_REQUEST | 316 | I |
| ON_STOP_NOTIFICATION | 2212 | MS_TRADE_CONFIRM | 296 | I |
| TRADE_CONFIRMATION | 2222 | MS_TRADE_CONFIRM | 296 | I |
| TRADE_ERROR | 2223 | MS_TRADE_INQ_DATA | 234 | I |
| ORDER_ERROR | 2231 | MS_OE_REQUEST | 316 | I |
| TRADE_CANCEL_CONFIRM | 2282 | MS_TRADE_CONFIRM | 296 | I |
| TRADE_CANCEL_REJECT | 2286 | MS_TRADE_CONFIRM | 296 | I |
| TRADE_MODIFY_CONFIRM | 2287 | MS_TRADE_MODIFY_CONFIRM | 296 | I |
| TRADE_MODIFY_REJECT | 2288 | MS_TRADE_CONFIRM | 296 | I |
| **SIGN_ON_REQUEST_IN** | **2300** | **MS_SIGNON** | **278** | I |
| **SIGN_ON_REQUEST_OUT** | **2301** | **MS_SIGNON / MS_ERROR_RESPONSE** | **278 / 182** | I |
| ERROR_RESPONSE_OUT | 2302 | MS_ERROR_RESPONSE | 182 | I |
| SIGN_OFF_REQUEST_IN | 2320 | MESSAGE_HEADER | 40 | I |
| SIGN_OFF_REQUEST_OUT | 2321 | SIGNOFF_OUT | 190 (see §1.11) | I |
| INVALID_MSG_LENGTH_RESPONSE | 2322 | (not in this appendix table; named in prose, p.25) | — | I |
| GR_REQUEST | 2400 | MS_GR_REQUEST | 48 | I |
| GR_RESPONSE | 2401 | MS_GR_RESPONSE | 124 / 136 | I |
| GIVEUP_APP_CONFIRM_TM | 4506 | GIVEUP_RESPONSE | 122 | I |
| GIVEUP_REJ_CONFIRM_TM | 4507 | GIVEUP_RESPONSE | 122 | I |
| BCAST_CONT_MSG | 5294 | MS_BCAST_CONT_MESSAGE | 244 | B |
| CTRL_MSG_TO_TRADER | 5295 | MS_TRADER_INT_MSG | 290 | B |
| TRADE_CANCEL_IN | 5440 | MS_TRADE_INQ_DATA | 234 | I |
| TRADE_CANCEL_OUT | 5441 | MS_TRADE_INQ_DATA | 234 | I |
| TRADE_MOD_IN | 5445 | MS_TRADE_INQ_DATA | 234 | I |
| SIGN_OFF_TRADER_IN | 5584 | MS_SIGNON | 278 | I |
| SIGN_OFF_TRADER_OUT | 5585 | MS_SIGNON / MS_ERROR_RESPONSE | 278 / 182 | I |
| DOWNLOAD_REQUEST | 7000 | MS_MESSAGE_DOWNLOAD | 48 | I |
| HEADER_RECORD | 7011 | MESSAGE_HEADER | 40 | I |
| MESSAGE_RECORD | 7021 | MESSAGE_HEADER | 40 | I |
| TRAILER_RECORD | 7031 | MESSAGE_HEADER | 40 | I |
| UPDATE_LOCALDB_IN | 7300 | MS_UPDATE_LOCAL_DATABASE | 82 | I |
| UPDATE_LOCALDB_DATA | 7304 | (variable, wrapped INNER_MESSAGE_HEADER) | 80-548 | I |
| UPDATE_LOCALDB_HEADER | 7307 | UPDATE_LDB_HEADER | 42 | I |
| UPDATE_LOCALDB_TRAILER | 7308 | UPDATE_LDB_HEADER | 42 | I |
| PARTIAL_SYSTEM_INFORMATION | 7321 | MS_SYSTEM_INFO_DATA | 106 | I |
| BATCH_ORDER_CANCEL | 9002 | MS_OE_REQUEST | 316 | I |
| BOX_SIGN_ON_REQUEST_IN | 23000 | MS_BOX_SIGN_ON_REQUEST_IN | 60 | I |
| BOX_SIGN_ON_REQUEST_OUT | 23001 | MS_BOX_SIGN_ON_REQUEST_OUT | 52 (detailed table) / 54 (appendix table, see §1.10) | I |
| SECURE_BOX_REGISTRATION_REQUEST_IN | 23008 | MS_SECURE_BOX_REGISTRATION_REQUEST_IN | 42 | I |
| SECURE_BOX_REGISTRATION_RESPONSE_OUT | 23009 | MS_SECURE_BOX_REGISTRATION_RESPONSE_OUT | 40 | I |
| BOX_SIGN_OFF | 20322 | MS_BOX_SIGN_OFF | 42 | I |
| HEARTBEAT | 23506 | MESSAGE_HEADER | 40 | I |
| BOARD_LOT_IN_TR / TRIMMED_BOARD_LOT_ACK_IN | 20000 / 20400 | MS_OE_REQUEST_TR | 158 | I |
| ORDER_MOD_IN_TR / TRIMMED_ORDER_MOD_ACK_IN | 20040 / 20402 | MS_OM_REQUEST_TR | 186 | I |
| ORDER_CANCEL_IN_TR / TRIMMED_ORDER_CANCEL_ACK_IN | 20070 / 20404 | MS_OM_REQUEST_TR | 186 | I |
| ORDER_QUICK_CANCEL_IN_TR | 20060 | MS_OM_REQUEST_TR | 186 | I |
| ORDER_CONFIRMATION_TR | 20073 | MS_OE_RESPONSE_TR | 240 | I |
| ORDER_MOD_CONFIRMATION_TR | 20074 | MS_OE_RESPONSE_TR | 240 | I |
| ORDER_CXL_CONFIRMATION_TR | 20075 | MS_OE_RESPONSE_TR | 240 | I |
| TRADE_CONFIRMATION_TR | 20222 | MS_TRADE_CONFIRM_TR | 230 | I |
| TXN_EXT_QUICK_ACK_* (nine codes) | 20401,20403,20405,20407,20409,20411,20413,20415,20417 | MS_ACK_RESPONSE | 22 | I |

### Codes CM also uses, with a different structure (the safety list)

| Code | CM's structure/size | F&O's structure/size | Note |
|---|---|---|---|
| **2000** BOARD_LOT_IN | ORDER_ENTRY_REQUEST, 290 | MS_OE_REQUEST, **316** | Already known per the task brief; confirmed (§1.4). |
| **2040/2070/2073/2074/2075/2231/2012/2042/2072** (all fourteen order codes on one structure) | 290 | 316 | Same structure, same 26-byte delta throughout, since it's one shared struct per venue. |
| **2222** TRADE_CONFIRMATION | MS_TRADE_CONFIRM, 228 | MS_TRADE_CONFIRM, **296** | §1.6. |
| **1600** SYSTEM_INFORMATION_IN | empty 40-byte header | MS_SYSTEM_INFO_REQ, **44** | §1.8. |
| **1601** SYSTEM_INFORMATION_OUT | 94 | MS_SYSTEM_INFO_DATA, **106** | §1.8. |
| **2300/2301** SIGN_ON_REQUEST_IN/OUT | SIGNON_IN/OUT, 276 | MS_SIGNON, **278** | §1.7 — the most consequential collision, since logon is the very first exchange on the wire. |
| **2302** ERROR_RESPONSE_OUT | ERROR_RESPONSE (SEC_INFO+message), 180 | MS_ERROR_RESPONSE (token Key+message), **182** | Different payload shape, not just a size delta: CM keys the error to a Symbol+Series, F&O keys it to a bare contract token (`Key`, CHAR(14), p.25). |
| **2320/2321** SIGN_OFF_REQUEST_IN/OUT | empty 40-byte header both ways | IN: empty (matches); OUT: SIGNOFF_OUT, **190** (§1.11, itself internally inconsistent) | Only the OUT direction differs. |
| **9002** BATCH_ORDER_CANCEL | not present in CM's `transactions.py` | MS_OE_REQUEST, 316 | Not a collision — CM doesn't define this code at all. |

Every other F&O structure listed above uses codes CM's `transactions.py` does not define at all
(no collision possible), or is byte-identical to CM (GR/box/heartbeat family, §1.10).

## 5. Proposed tag allocation

Following CM's `dictionary.py` convention exactly (`9000s` header, `9100s` body, `9200s` bit
flags, `9300s` download, **`9400+` reserved for F&O** — CM's own docstring, quoted in the task
brief, already reserves this range). Proposed ranges, sized generously against the field counts
actually transcribed above:

| Range | Purpose |
|---|---|
| `9400`-`9409` | F&O's own `MESSAGE_HEADER`/header-adjacent fields not already covered by the 9000s (none identified — F&O's header is field-for-field identical to CM's, so no new header tags are needed; reuse CM's `LOG_TIME`, `ALPHA_CHAR`, `USER_ID`/`TRADER_ID`, `ERROR_CODE`, `TIMESTAMP`, `TIMESTAMP1`, `TIMESTAMP2`, `MESSAGE_LENGTH` tags as-is). |
| `9410`-`9439` | `CONTRACT_DESC` fields: `INSTRUMENT_NAME`, `EXPIRY_DATE`, `STRIKE_PRICE`, `OPTION_TYPE`, `CA_LEVEL` (`SYMBOL` reuses FIX tag 55, see below). |
| `9440`-`9489` | `MS_OE_REQUEST` fields beyond what CM's own 9100s already name: `TOKEN_NO`, `COUNTERPARTY_BROKER_ID`, `CLOSEOUT_FLAG`, `ORDER_TYPE`, `TRIGGER_PRICE` (CM has no trigger price at all — Stop Loss is out of scope for CM but the *tag* belongs here since F&O's RL slice can carry SL/MIT bits even if the book itself is refused), `MKT_REPLAY`, `OPEN_CLOSE`, `SETTLEMENT_PERIOD` (distinct from CM's `SETTLEMENT_TYPE`). |
| `9490`-`9499` | `PRICE_MOD` fields not already covered: `REFERENCE` (the discretionary front-end tag). |
| `9500`-`9529` | `MS_TRADE_CONFIRM` fields beyond CM's: `COUNTER_TRADER_ORDER_NUMBER`, `COUNTER_BROKER_ID`, `OLD_OPEN_CLOSE`, `OLD_ACCOUNT_NUMBER`, `PARTICIPANT`, `OLD_PARTICIPANT`, `OLD_PAN`. |
| `9530`-`9549` | `MS_SIGNON` fields beyond CM's: `COLOUR`, `BATCH2_START_TIME`, `END_TIME`, `HOST_SWITCH_CONTEXT`, `WS_CLASS_NAME`, `MEMBER_TYPE`, `CLEARING_STATUS`. |
| `9550`-`9569` | `MS_SYSTEM_INFO_DATA` fields beyond CM's: `RISK_FREE_INTEREST_RATE`, and the three named sub-structures `ST_MARKET_STATUS`/`ST_EX_MARKET_STATUS`/`ST_PL_MARKET_STATUS` if their sub-fields need distinct tags from CM's flatter per-market SHORT fields (recommend reusing CM's `NORMAL_STATUS`/`ODDLOT_STATUS`/`SPOT_STATUS`/`AUCTION_STATUS` three times over, once per sub-structure, rather than minting nine new tags — the values mean the same thing three ways). |
| `9570`-`9579` | `ST_STOCK_ELIGIBLE_INDICATORS` (if kept distinct from CM's `SECURITY_AON`/`SECURITY_MIN_FILL`/`SECURITY_BOOKS_MERGED` — recommend reusing those three tags, since the concepts and bit meanings agree). |
| `9600`-`9619` | `ADDITIONAL_ORDER_FLAGS` bits (one tag per bit, mirroring CM's 9200s convention but in F&O's own range since the byte itself doesn't exist in CM): `FLAG_BOC`, `FLAG_COL`, `FLAG_STPC_ADDITIONAL` (name it distinctly from any tag reused for CM's own `FLAG_STPC`, since the two occupy different structures/offsets even though the concept is the same — see next paragraph). |
| `9620`-`9639` | `ST_ORDER_FLAGS` bits *not* shared with CM's set: `FLAG_SL`, `FLAG_MIT` (new bits; `FLAG_ATO`, `FLAG_MARKET`, `FLAG_DAY`, `FLAG_GTC`, `FLAG_IOC`, `FLAG_AON`, `FLAG_MF`, `FLAG_MATCHED_IND`, `FLAG_TRADED`, `FLAG_MODIFIED`, `FLAG_FROZEN`, `FLAG_PREOPEN` can all reuse CM's existing 9200s tags directly, since the bit *name* and *meaning* agree even though the bit *position* differs per structure — position is a codec concern in `layouts.py`, not a dictionary concern, so sharing the tag is safe and correct). |
| `9640`-`9649` | `MS_TRADE_INQ_DATA` fields (out-of-scope structure, but reserve tags in case Phase 2 implements trade modification): `MKT_TYPE`, `BUY_OPEN_CLOSE`, `SELL_OPEN_CLOSE`, `BUY_BROKER_ID`, `SELL_BROKER_ID`, `REQUESTED_BY`, `BUY_ACCOUNT_NUMBER`, `SELL_ACCOUNT_NUMBER`, `BUY_PAN`, `SELL_PAN`. |
| `9650`-`9659` | Box/GR fields — none needed; reuse CM's `BOX_ID`/`IP_ADDRESS`/`PORT`/`SESSION_KEY`/`CRYPTOGRAPHIC_KEY`/`STATIC_IV`/`DYNAMIC_IV`/`ADDITIONAL_KEY` tags verbatim (§1.10 confirms byte-identical structures). |

**On the `STPC` naming clash above:** F&O's `STPC` bit lives in a completely different structure
(`ADDITIONAL_ORDER_FLAGS`, offset 218 of `MS_OE_REQUEST`) from CM's `STPC` bit (bit 15 of
`ST_ORDER_FLAGS`, offset 140). They mean the same thing (self-trade-prevention-cancel
instruction) but sit in different bytes at different offsets in structurally unrelated
structures. **Do not give them the same tag number even if reusing CM's `FLAG_STPC` constant
seems tempting** — the codec needs to know which byte to read/write, and one dictionary tag
shared across two different bit positions in two different structures would be indistinguishable
in the audit. Mint a new tag (`FLAG_STPC_ADDITIONAL` above) for F&O's copy.

### FIX tags safe to reuse (following CM's own reuse list exactly)

| FIX tag | Name | Safe to reuse for F&O? |
|---|---|---|
| 55 | Symbol | Yes — `CONTRACT_DESC.Symbol` is exactly a symbol, same concept CM already maps this tag to. |
| 54 | Side | Yes — `BuySellIndicator` is exactly Side. |
| 38 | OrderQty | Yes — `Volume` is exactly OrderQty. |
| 44 | Price | Yes — `Price` is exactly Price. |
| 37 | OrderID | Yes — `OrderNumber` is the exchange-assigned order identifier, exactly OrderID's job (same reasoning CM already applies). |
| 32 / 31 | LastQty / LastPx | Yes — `FillQuantity`/`FillPrice` on `MS_TRADE_CONFIRM` are exactly these. |
| 1 | Account | Yes — `AccountNumber` is exactly Account, as CM already does. |
| 151 | LeavesQty | Yes — `TotalVolumeRemaining` is exactly LeavesQty, as CM already does. |
| 14 | CumQty | Yes — `VolumeFilledToday` is exactly CumQty, as CM already does. |

### Tags that must **not** be reused, and why

- **`ClOrdID(11)`/`OrigClOrdID(41)`** — same reasoning as CM verbatim: F&O has no client-supplied
  order handle either. `OrderNumber` (the DOUBLE) is the only identifier, and it is the venue's,
  not the client's.
- **`OrdType(40)`/`TimeInForce(59)`** — same reasoning as CM verbatim: F&O spells both as bits of
  `ST_ORDER_FLAGS`, not as scalar fields. (F&O additionally has a scalar `OrderType` field at
  offset 96 of `MS_OE_REQUEST`, but it is documented as "should be set to blank" for every RL
  transaction code — see §6 — so it does not carry FIX's `OrdType` domain and should not be
  mapped to tag 40 even though the field exists on the wire.)
- **`StrikePx(202)`** — tempting for `CONTRACT_DESC.StrikePrice`, but F&O's field carries the
  sentinel `-1` for a futures contract (§1.2), which is not a legal `StrikePx` value in FIX
  (a non-negative price field). Mint a private tag instead of reusing 202, so the sentinel can be
  rendered and audited without the dictionary's normal price-field validation rejecting it.
- **`PutOrCall(201)`** — tempting for `OptionType`, but F&O's field is three-valued (`CE`/`PE`/
  `XX`) against FIX's two-valued `PutOrCall` (0=Put, 1=Call). Mint a private tag with a
  three-value enum instead of forcing the third value (`XX`, futures) into a binary FIX field.
- **`MaturityDate(541)` or `MaturityMonthYear(200)`** — tempting for `ExpiryDate`, but F&O's field
  is a raw seconds-since-1980-epoch LONG (like every other date/time field in this protocol), not
  a FIX date-typed field. Mint a private tag and render it through the same epoch conversion as
  every other NNF timestamp, exactly as CM does for its own date fields.

## 6. What the specification does not say (bound for `ASSUMPTIONS`)

In the voice `venues/nse/rules.py:ASSUMPTIONS` uses:

- **`MS_OE_REQUEST`'s `TraderId` at header offset 8 is transcribed identically to CM's `UserId`
  at the same offset, on the reading that the two documents' authors used different English
  labels for one wire concept rather than describing two different fields.** The alternative
  reading — that F&O's header genuinely carries a per-trader identifier distinct from CM's
  per-session user identifier at this position — is not supported by anything in Chapter 2 or
  Chapter 11's repeated printing of this table (p.21, p.209), both of which describe the field in
  identical terms ("This field should contain the user ID" / "populate the relevant User ID
  field"), so it is treated as the same field under a different name.
- **`CONTRACT_DESC.StrikePrice = -1` for a futures contract is transcribed as a literal signed
  sentinel, not as "zero meaning absent."** This contradicts the protocol's own general rule
  (Chapter 2, p.20: "All numeric data must be set to zero... unless a value is assigned") and is
  the one field in this document found to do so; the alternative reading — that this is a
  documentation error and futures contracts really do carry zero — is rejected because the
  Give-Up chapter (p.249) states the `-1` convention affirmatively and specifically, rather than
  leaving it to the general rule by omission.
- **The four-byte reserved run at offset 220-223 of `MS_OE_REQUEST`** (the `Filler1`-`Filler18`
  bits/bytes described in §1.4) is transcribed as plain reserved space with no tags minted for
  its individual bits, on the reading that a field description table (p.64-68) that names every
  other field in the structure and is silent about these eighteen would have said something if
  any of them carried meaning today. The alternative reading — that these are genuinely
  meaningful bits simply omitted from the field-description prose by editorial oversight — cannot
  be ruled out from the pages read, and if a real F&O client is ever observed setting any of
  these bits, this assumption should be revisited before the simulator silently drops them.
- **`OrderType` (SHORT, offset 96 of `MS_OE_REQUEST`) has no stated value domain anywhere in the
  Order Entry chapter for a Regular Lot order** — every description found (p.65, p.71-83) says
  only "should be set to blank" for the RL-family transaction codes. It may carry a real
  enumeration for the Spread/2L/3L family (out of scope here), or it may be genuinely always
  blank for RL and only nominally present because the structure is shared across fourteen
  transaction codes. Transcribed as: accept only a blank/zero value on every RL transaction code
  this simulator implements, and reject any other value the same way CM rejects an unexpected
  field value — pending confirmation from a page outside the range read in this phase.
- **`BOC` (bit of `ADDITIONAL_ORDER_FLAGS`) is not expanded anywhere in the pages read.** `COL`
  and `STPC` are both defined in prose (§1.3); `BOC` is not. Given its position alongside `COL`
  (Cancel On Logoff) and the document's fondness for order-lifecycle abbreviations, a plausible
  reading is "Book Order Class" or a closeout-adjacent flag, but this is a guess, not a
  transcription, and is recorded under §"Not found" rather than asserted here.
- **`ST_EX_MARKET_STATUS` and `ST_PL_MARKET_STATUS`** (two of the three 8-byte sub-structures of
  `MS_SYSTEM_INFO_DATA`, §1.8) are given field tables identical in shape to `ST_MARKET_STATUS`
  but their names ("EX", "PL") are never expanded in the pages read. Given F&O trades index and
  stock derivatives across what NSE calls separate underlying segments, a plausible reading is
  "Exchange" vs "Physical/Local" market status feeds, or "Ex-" as in "ex-dividend"-adjacent
  corporate-action status — but again, this is not confirmed, and the three sub-structures are
  transcribed as three independent per-market status snapshots regardless of what distinguishes
  them, since their *wire shape* does not depend on resolving the name.
- **`MS_SIGNON`'s two time-adjacent fields, `EndTime` (request/response, offset 118 on IN /
  offset 118 on OUT under a different name) and `SequenceNumber` (offset 176), both describe
  "the time when the markets last closed" in their respective field-description tables** (p.35-36
  for `SequenceNumber`; the `EndTime` field itself has no separate field-description row in the
  pages read, only the generic note about "last closed" attached to the field immediately
  following it in the table). Whether these are truly two independent fields carrying the same
  value, or whether one of them is described in a paragraph the extractor merged into the wrong
  field's row, is not resolved here — transcribed as two separate fields per the offset table,
  with the semantic overlap flagged rather than silently collapsed into one.
- **`SIGN_OFF_REQUEST_OUT`'s packet length is stated three different ways across two tables**
  (§1.11: 40 in one caption, 189 by summing the same table's own rows, 190 in the appendix
  summary). The transcription in §1.11 takes 190 (matching the appendix and the odd-to-even
  padding rule) as authoritative, on the same "detailed table wins... unless it's internally
  inconsistent, in which case the total that satisfies both the padding rule and a second
  independent table wins" reasoning CM's own `layouts.py` applies to `SYSTEM_INFORMATION_DATA`'s
  90-vs-94 disagreement. This is the weakest-evidenced structure in this transcription and should
  be the first one re-verified against a real F&O client's logoff-confirmation traffic before
  Phase 1 ships it.
- **`BOX_SIGN_ON_REQUEST_OUT`'s packet length disagrees by 2 bytes** between the detailed
  structure table (52, p.216, matching CM's `layouts.py` exactly) and the appendix summary table
  (54, p.278). Transcribed as 52 per the detailed-table-wins rule; unlike the logoff case above,
  there is no internal arithmetic inconsistency within the detailed table itself, so this reading
  is on firmer ground than the logoff one.
- **`BuySellIndicator`, `Pro/ClientIndicator`, and `Modified/CancelledBy` value domains are
  assumed identical to CM's** (`BuySell.BUY="1"`/`SELL="2"`; `ProClient.CLIENT="1"`/`PRO="2"`;
  `ModCxlBy` coded `T`/`B`/`M`/`E`) because every page found describing these fields had its
  actual value list elided by the PDF extractor's column-jump behaviour around bulleted/tabular
  value lists (the same failure mode `docs/specs/README.md` already documents for Japannext's
  merged-cell tick tables). This should be treated as **unverified** until a page containing the
  literal value list is found and re-read, not as confirmed by this phase.

## 7. Out of scope, and its published refusal code

| Flow | Refusal code | Citation |
|---|---|---|
| Special Terms book (Book Type 2) | `e$invalid_book_type (16406)` | p.261 |
| Stop Loss orders (Book Type 3, `SL` bit) | `SL_NOT_ALLOWED (16445)` | p.264 |
| Market If Touched orders (Book Type 3, `MIT` bit) | `MIT_NOT_ALLOWED (16446)` | p.264 |
| Negotiated Trade orders (Book Type 4) | `e$nt_orders_not_allowed (16561)` | p.263 |
| Odd Lot book (Book Type 5) | `e$invalid_book_type (16406)` | p.261 (Odd Lot Market is marked "Not used" for F&O itself, p.281, so this is doubly refused — by the exchange's own appendix and by this simulator's scope) |
| Spot book (Book Type 6) | `e$invalid_book_type (16406)` | p.261 ("Not used", p.281) |
| Auction book (Book Type 7) | `e$invalid_book_type (16406)` / `ERR_CANNOT_MOD_AUC_ORDER (16397)` for modify attempts | p.259, p.261 ("Not used" as a market type, p.281 — F&O's own appendix marks the whole Auction *market* unused, unlike CM which implements POS/CAS) |
| Spread orders (2100-2136 family) | `e$spread_not_allowed (16607)` | p.264 |
| 2L orders (2102, 2131 etc.) | Refuse the transaction code outright: `ERROR_BAD_TRANS_CODE (16003)` — no 2L-specific refusal code was found distinct from the general spread-not-allowed one, since 2L/3L share `MS_SPD_OE_REQUEST` and the spread-specific codes with genuine spread orders | p.256, p.271-272 |
| 3L orders (2104, 2132 etc.) | Same as 2L, `ERROR_BAD_TRANS_CODE (16003)` | p.256, p.271-272 |
| Trade Modification (`TRADE_MOD_IN 5445`) | `USER_TRD_MOD_DISABLED (17046)` | p.268 |
| Trade Cancellation (`TRADE_CANCEL_IN 5440`) | `e$dup_trd_cxl_request (16086)` is for a duplicate request specifically; for an outright "not supported" refusal, `ERROR_BAD_TRANS_CODE (16003)` is the more honest choice, since no code in this table means "trade cancellation is unavailable" the way `e$spread_not_allowed` means "spreads are unavailable" | p.257, p.256 |
| Give-Up / Interactive Give-Up (Chapter 14, `GIVEUP_APP_CONFIRM_TM`/`GIVEUP_REJ_CONFIRM_TM`) | Not applicable as a client refusal — these are host-to-client notifications about a Clearing Member's own approval/rejection decision (p.248), not a request a trading member sends and the venue can refuse. Out of scope means "never generated," not "refused." | p.248-250 |
| Closeout-status order flows (Chapter 13's closeout rules) | `CLOSEOUT_NOT_ALLOWED (16688)` | p.267 |
| Price/Quantity freeze and its approval workflow | `OE_QTY_FREEZE_CAN (16307)` / `OE_PRICE_FREEZE_CAN (16308)` name the *cancellation*; there is no single "freeze workflow not supported" code — this simulator's choice (as CM already makes for HKEX-style freeze/approval flows) is to never produce a freeze in the first place, which sidesteps needing a refusal code at all | p.259 |
| Broadcast/market-data feed (UDP multicast, Bhavcopy, all Chapter 8/9 structures) | No refusal applicable — never offered on the interactive connection at all, same reasoning as CM's own unbuilt multicast feed (LZO compression, stdlib-only constraint) | Chapters 8-9, p.134-203 |
| The `_TR` "Trimmed" structure family and Chapter 15's immediate-ack feature (§1.9) | `ERROR_BAD_TRANS_CODE (16003)` for every `_TR`/`_ACK_IN` code | p.256, p.271-278 |
| Branch/User order-value-limit updates, password reset, COL status, trade-mod/cxl status, user unlock, kill switch at the trading-member level (Chapter 13) | `ERR_NO_PRIVILEGE (16684)` is the closest generic "not authorised" code; these are administrative flows this simulator's control plane substitutes for directly (as `venues/common_commands.py` already does for the equivalent CM administrative surface), so no wire-level refusal is expected to be needed in practice | p.266 |
| Postclose market status (§2.2) | Not a flow to refuse so much as a state to never enter — if Phase 1 does not implement the Postclose phase, `set_trading_state` should simply never offer it as a target state, the same way CM's core refuses a state transition it does not model | p.280-281 |

