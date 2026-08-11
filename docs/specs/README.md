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

Still genuinely unbuilt, and rejected rather than faked: the odd/special lot
book, quotes (`35=S/Z/AI`), trade capture (`35=AE/AR`), drop copy, and the
Volatility Control Mechanism.
