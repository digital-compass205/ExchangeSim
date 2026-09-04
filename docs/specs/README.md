# Venue specifications

The PDFs themselves are **not committed** (`.gitignore` excludes `docs/specs/*.pdf`)
— they are large binaries published by the venues. Fetch them here on demand.

Read them without adding a dependency:

```bash
$PY tools/pdftext.py docs/specs/<file>.pdf --grep OrdType
```

## Japannext PTS (implemented)

Index: <https://www.japannext.co.jp/en/support>

| File | Used for |
|---|---|
| `JNX_FIX_Trading_Specification_Equities_3.00.pdf` | the dialect in `venues/japannext/dictionary.py` |
| `JNX_Trading_Rules_Equities_2.02_EN.pdf` | price bands and tick tables in `venues/japannext/reference/` |
| `JNX_Self-Trade_Prevention_2.00.pdf` | the three STP modes and which side is cancelled |

Appendices 3–5 of the Trading Rules are merged-cell tables that text extraction
cannot reconstruct unambiguously, which is why the shipped tick ladders are
flagged `UNVERIFIED` in their CSV headers.

## HKEX securities market (implemented: board-lot continuous trading)

Index: <https://www.hkex.com.hk> → Services → Trading → Securities → Infrastructure → OCG-C

| File | Used for |
|---|---|
| `HKEX_OCGC_FIX_Trading_Protocol_3.12.pdf` | the dialect in `venues/hkex/dictionary.py`; v3.12, 19 July 2023 |
| `HKEX_OCGC_Binary_Trading_Protocol_3.2.pdf` | the second encoding: `binary/` and the layouts in `venues/hkex/binary.py`; v3.2, 19 July 2023, the same edition as the FIX one |
| `HKEX_OCGC_Connectivity_Guide_3.0.pdf` | session establishment, IP registration |
| Rules of the Exchange, **Second Schedule, Part A** | `venues/hkex/reference/spread_table.csv`. Transcribed verbatim, and it already incorporates the reductions that followed the June 2024 minimum-spread consultation (conclusions December 2024, Phase 1 effective 4 August 2025) |
| Rules of the Exchange, quotation and nominal-price rules | the two price checks: 24 spreads behind the same-side best and 9 through the other's, plus the multiplicative 9-times rule |
| *Trading Mechanism of the CAS in the Securities Market* | the CAS reference price, its two-stage price limits, and the five IEP determination rules that `core/auction.py` implements |

The protocol document deliberately says almost nothing about auctions or price
limits — it defines the reject codes and leaves the rules to the Rules of the
Exchange. Both were transcribed from the published rules rather than inferred;
what could not be pinned down is in `rules.ASSUMPTIONS`.

### What the protocol requires that Japannext did not

This is the reason a Hong Kong venue was more than a new dialect module, and
where each requirement ended up.

| Requirement | Where it lives |
|---|---|
| **FIX 5.0 SP2 semantics over `BeginString=FIXT.1.1`**, `DefaultApplVerID(1137)=9` on Logon, `ApplVerID(1128)=9` on everything the venue generates | `SessionConfig.appl_ver_id` / `default_appl_ver_id`, stamped in `Session._transmit` |
| **`NextExpectedMsgSeqNum(789)` required on Logon** — FIX 4.4+ sequence negotiation, not Japannext's pure ResendRequest flow. Neither side may raise a ResendRequest from the Logon's own MsgSeqNum | `SessionConfig.next_expected_seq_num`, honoured in `Session._on_logon` |
| **A client may not reset the sequence through Logon** (section 4.6.2.1); it asks the operations desk | `SessionConfig.allow_logon_reset=False`; the equivalent is the `session.reset` control command |
| **`EncryptedPassword(1402)` is mandatory from the client**, RSA-encrypted per `EncryptedPasswordMethod(1400)=101` | required by the dictionary, **not verified** — recorded as an `ASSUMPTION`, since a simulator holds no private key |
| **Instruments named by `SecurityID(48)`** with `SecurityIDSource(22)=8` and `SecurityExchange(207)=XHKG`, never `Symbol(55)` | the gateway maps at the boundary; `Symbol` is not defined in the dialect at all |
| **Repeating groups in earnest** — `<Parties>` (`453`/`448`/`447`/`452`) carrying Broker Number, BCAN and BS User ID, and a mandatory `<DisclosureInstructionGrp>` | read positionally from `Message.get_all`; `handlers._check_count` rejects a NumInGroup that disagrees with the entries, `SessionRejectReason=16` |
| Order types **Market (`40=1`)** and Limit; TIF **0/3/4/9** | market orders landed in `core/matching.py` ahead of the venue; TIF 9 is refused, needing an auction |
| **Self-match prevention** via `SelfMatchPreventionID(2362)`, across Exchange Participants, cancelling the passive or the aggressive side | `Order.stp_id` / `stp_instruction` in the core. The *instruction* is registered against the ID out of band, so the venue keeps a registry — config `smp`, command `smp.register` |
| **`MarketSegmentID(1300)`** appears only on mass cancel | a security belongs to one segment, from the `segment` column of `securities.csv`, and that picks the book |
| **Board lot and odd/special lot are distinct flows**, the latter semi-automatic and matched by trade request | odd lots are **rejected**, not silently promoted |

The POS and CAS auctions are implemented: `core/auction.py` finds the price and
`venues/hkex/auctions.py` holds the reference price and the two-stage bands.

### One protocol, two encodings

HKEX publishes OCG-C twice — as tag=value FIX and as a fixed-width binary
format — and a client is entitled to one of them. Both are implemented, which
took a codec rather than a second gateway: `binary/` decodes a frame into the
same `fix.Message` the venue already handles, so the session layer, the
handlers, the books and the audit are shared. What the *documents* disagree
about, and where each difference lives:

| The binary specification says | Where |
|---|---|
| A frame is `STX`, a UInt16 length, the type, the sequence, PossDup/PossResend, a 12-byte Comp ID, a 32-byte field presence map, the body, and a CRC32C — all little-endian | `binary/message.py`, `binary/types.py` |
| A field's position in the body comes from its bit in the presence map, and the numbering is per message type | `venues/hkex/binary.py` |
| One Comp ID travels, the client's, in both directions | `binary/codec.py` |
| There is no SendingTime, no BeginString and no ApplVerID | the header is what section 7.2 lists, nothing more |
| Logon has no EncryptMethod and no HeartBtInt | `SessionConfig.requires_encrypt_method` / `requires_heart_bt_int` |
| A Reject is replayed on a resend rather than gap-filled (section 5.6) | `binary/codec.py:GAP_FILLABLE` |
| There is no OrderCancelReject: a refused cancel or amend is an Execution Report with ExecType `X` or `Y`, carrying totals 35=9 has no fields for | `handlers.py:_for_wire` |
| `<Parties>` is four flat broker fields and `<DisclosureInstructionGrp>` a bitmap | the getters and setters in `venues/hkex/binary.py` |
| A Cancel Request carries no OrderQty, and SecurityExchange is optional throughout | `dictionary.build_binary()` |

Both editions document `TransactTime` to **microseconds**, which the shared
timestamp validator did not accept until this venue's dialect began stating its
own precision.

Still genuinely unbuilt, and rejected rather than faked: the odd/special lot
book, quotes (`35=S/Z/AI`), trade capture (`35=AE/AR`), drop copy, and the
Volatility Control Mechanism.

## NSE India, Capital Market (in progress)

Index: <https://www.nseindia.com/static/trade/platform-services-neat-trading-system-protocols>

| File | Used for |
|---|---|
| `TP_CM_Trimmed_NNF_PROTOCOL_6.6_20260803151116.pdf` | everything: the transaction codes and structures in `venues/nse/`, the framing and crypto in `nnf/`; v6.6, Feb 2025 lineage |
| NSE call-auction circulars (pre-open session) | the equilibrium-price rule chain in `venues/nse/rules.py:PREOPEN_RULES` — **not** in the protocol document |
| NSE circulars on price bands / DPR | `venues/nse/reference/price_bands.csv` |

The protocol document is unusually complete for a venue here: it tables every
interactive transaction code with its structure and byte size, and it publishes
the order-flag bitfield **twice**, once for little-endian and once for big-endian
machines. Where HKEX left the bit numbering to be assumed, NSE states it, so
`ASSUMPTIONS` should stay correspondingly short. What the document does *not*
define is the trading rules — the pre-open uncrossing chain and the daily price
range come from the circulars, as HKEX's auction rules came from the Rules of
the Exchange.

### What this protocol requires that neither other venue did

| Requirement | Where it lives |
|---|---|
| **Not FIX in any encoding.** A proprietary fixed-width big-endian format with its own sign-on, no sequence numbers, no resend and no session-level Reject | a third protocol stack, `nnf/`, beside `fix/` and `binary/` |
| **A 22-byte packet prefix** — `Length(2) + SequenceNumber(4) + MD5-or-GCM-tag(16)` — over the 40-byte `MESSAGE_HEADER`, capped at 1024 bytes, with the checksum covering the message data only (Chapter 10) | `nnf/packet.py` |
| **Mandatory AES-256-GCM**, in two published methodologies: GCM-as-keystream with MD5 integrity, and full GCM with a 12-byte AAD and the tag in the packet prefix. The IV is static[8] + a 64-bit counter incremented before each encryption and *decremented* before each decryption | `nnf/crypto.py`, written by hand — the stdlib has MD5 and TLS but no AES |
| **A TLS 1.3 Gateway Router** on its own port issuing the key, IV and AAD (`GR_REQUEST 2400` / `GR_RESPONSE 2401`) | a second listener in `venues/nse/venue.py` |
| **A two-tier session**: one TCP connection is a *box* (`23008`/`23009` registration, `23000`/`23001` box sign-on) and every user linked to that Box ID signs on over it (`2300`/`2301`). Disconnecting the box logs off all its users | `nnf/session.py:BoxConnection` owning many `NnfSession` |
| **`pragma pack 2`** — an 8-byte `LONGLONG` sits at offset 14 of the header, and structures of odd size are padded to even | `nnf/layout.py`, fixed offsets rather than computed ones |
| **One structure, fourteen transaction codes.** `ORDER_ENTRY_REQUEST` (290 bytes) carries entry, modify, cancel, all three confirmations, both rejects, the error and the price confirmation | one `Layout`, many `MessageDef`s |
| **No client order identifier.** Modify and cancel address the exchange-assigned `OrderNumber` (DOUBLE); `NNFField` is a member reference for the CTCL audit trail, not a handle | `CancelRequest.order_id` in the core, resolved via `OrderRegistry.by_order_id` with an ownership check |
| **Order characteristics as a bitfield**, not scalar fields: `ST_ORDER_FLAGS` holds ATO, Mkt, OnStop, Day, GTC, IOC, AON, MF and the state bits | `nnf/layout.py:Flags`, one tag per bit |
| Prices in **paise**, times in **seconds since 1 Jan 1980**, strings **blank**-padded and never NUL-terminated | `core/prices.py:PriceCodec(2)`; `nnf/types.py` |
| Instruments named by **Symbol + Series** (`SEC_INFO`: `CHAR[10]` + `CHAR[2]`) — the numeric token appears only in the security master and the broadcasts | `NseVenue.resolve_symbol`, keyed on `"INFY-EQ"` |
| **Rejections are numeric `ErrorCode` in the header**, on a transaction-specific `*_ERROR`/`*_REJECT`, because there is no session-level Reject | `venues/nse/rules.py:REJECT_TO_ERROR_CODE`; the session hands a dialect failure to the gateway |

Market and book vocabulary, from the appendix:

| | |
|---|---|
| Market types | 1 Normal, 2 Odd Lot, 3 Spot, 4 Auction, 5 Call auction 1, 6 Call auction 2 |
| Market statuses | 0 PreOpen (Normal only), 1 Open, 2 Closed, 3 Preopen ended |
| Book types | 1 Regular Lot, 2 Special Terms, 3 Stop Loss, 5 Odd Lot, 6 Spot, 7 Auction, 11 Call Auction 1 |

Only the Regular Lot book of the Normal market is implemented, plus its pre-open
call auction. Every other book type is **rejected** rather than promoted to a
Regular Lot order, on the same reasoning as HKEX's odd lots. Also deliberately
unbuilt: the UDP multicast broadcast feed (it is LZO-compressed, and LZO cannot
be done under the stdlib-only constraint), the closing call auction, trade
modification and cancellation, disclosed quantity, and the freeze/approval flow.
