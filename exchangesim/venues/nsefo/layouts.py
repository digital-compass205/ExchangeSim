"""Every Futures & Options structure, transcribed offset by offset.

This module is meant to be read beside ``docs/specs/NSE_FO_TRANSCRIPTION.md``:
each table below is one table of that document, in the same order, with the
same offsets. Nothing is computed. The structures are ``pragma pack 2`` and
pad odd runs to even, so a walk that summed widths would drift -- exactly the
discipline :mod:`exchangesim.venues.nse.layouts` documents for Capital Market,
and just as necessary here: this venue's ``MS_TRADE_CONFIRM`` pads *twice*
(offsets 131 and 167) where Capital Market's own structures pad once or not
at all.

``tests/test_nsefo_dictionary.py`` checks the one property that catches
almost every transcription slip mechanically: **the fields of each structure
must reach exactly the packet length the transcription's appendix tables give
it** -- 316 for an order, 296 for a trade, 278 for a sign-on (not Capital
Market's 276). A field written at the wrong offset or with the wrong width
moves the end of the structure, and the test names the transaction code.

Three things this document adds that Capital Market's has no equivalent of at
all: ``PRICE_MOD``, an optimised price-only modification; a whole second
bitfield, ``ADDITIONAL_ORDER_FLAGS``, carrying the self-trade-prevention
instruction Capital Market keeps inside ``ST_ORDER_FLAGS`` itself; and a
``StrikePrice`` field whose "no value" spelling is the literal integer
``-1``, not zero -- the one field in either document that breaks the
protocol's own "zero means absent" rule (see :class:`StrikeScaled`).

Two things about this document's own internal consistency are worth knowing.
``SIGN_OFF_REQUEST_OUT``'s packet length is stated three different ways
across two of its own tables (40 in a stale caption, 189 by summing the
caption's own field rows, 190 in the appendix); 190 is used here, the same
"detailed table wins unless it disagrees with itself, in which case the
reading that satisfies the padding rule and a second independent table wins"
reasoning Capital Market's own ``layouts.py`` applies to its own
``SYSTEM_INFORMATION_DATA`` disagreement. And ``BOX_SIGN_ON_REQUEST_OUT``
disagrees by two bytes between its detailed table (52, matching Capital
Market's own structure exactly) and the appendix summary (54); 52 is used
here, on firmer ground since there is no internal arithmetic inconsistency
in the detailed table itself.
"""

from ...nnf import layout as L
from ...nnf import types as T
from . import dictionary as D
from . import transactions as X


class StrikeScaled(L.Scaled):
    """``CONTRACT_DESC.StrikePrice``: paise-scaled, except for one sentinel.

    "This field will contain a valid strike for Options Contract and for
    Futures Contract it will be -1" (transcription §1.2, p.249) -- a literal
    signed integer, not "zero means absent" the way every other numeric field
    in this protocol spells absence. Scaling ``-1`` as an ordinary price
    would divide it into ``-0.01``, which is not what a futures contract's
    strike means; both directions are overridden so the sentinel round-trips
    as the literal text ``-1`` and everything else scales normally.
    """

    ABSENT = "-1"
    SENTINEL = -1

    def to_wire(self, text):
        if text == self.ABSENT:
            return self.SENTINEL
        return super(StrikeScaled, self).to_wire(text)

    def from_wire(self, value):
        if int(value) == self.SENTINEL:
            return self.ABSENT
        return super(StrikeScaled, self).from_wire(value)


#: Prices are in paise throughout this protocol, the same convention Capital
#: Market's document states outright and this one is assumed to share (see
#: dictionary.PRICE_DECIMALS).
PAISE = L.Scaled(2)
#: CONTRACT_DESC.StrikePrice's own scaling, sentinel-aware.
STRIKE = StrikeScaled(2)

#: The forty-byte message header (transcription §1.1) -- byte-for-byte
#: identical to Capital Market's own, restated independently rather than
#: imported. The two source documents are independent, and a future revision
#: of either one must not silently change the other's transcription; this is
#: the same call ``hkex/dictionary.py:build_binary()`` makes about its own
#: tables being a separate transcription from the FIX one, not a flag on it.
#: The one difference from Capital Market's table is cosmetic: this
#: document's own field-by-field table calls the offset-8 LONG "TraderId",
#: not "UserId" -- see the field's own comment in dictionary.py.
HEADER = L.HeaderLayout(40, (
    L.Field(L.TRANSACTION_CODE, "TransactionCode", T.SHORT, 0),
    L.Field(D.LOG_TIME, "LogTime", T.LONG, 2),
    L.Field(D.ALPHA_CHAR, "AlphaChar", T.Char(2), 6),
    L.Field(D.USER_ID, "TraderId", T.LONG, 8),
    L.Field(D.ERROR_CODE, "ErrorCode", T.SHORT, 12),
    L.Field(D.TIMESTAMP, "Timestamp", T.LONG_LONG, 14),
    # Declared CHAR[8] in the table and numeric in its own description: "in
    # TimeStamp1, current time is sent in jiffies from host end. This is 8
    # bytes in host end", and TimeStamp2 carries a machine number in its
    # eighth byte, which is the low byte of a big-endian LONG LONG. Read as
    # text they would be cut at the first NUL of a perfectly ordinary value.
    L.Field(D.TIMESTAMP1, "TimeStamp1", T.LONG_LONG, 22),
    L.Field(D.TIMESTAMP2, "TimeStamp2", T.LONG_LONG, 30),
    L.Field(D.MESSAGE_LENGTH, "MessageLength", T.SHORT, 38),
))


def _contract_desc(offset):
    """``CONTRACT_DESC``, Table 27 -- twenty-eight bytes, the instrument
    identity embedded in both ``MS_OE_REQUEST`` and ``MS_TRADE_CONFIRM``.
    """
    return (
        L.Field(D.INSTRUMENT_NAME, "InstrumentName", T.Char(6), offset),
        L.Field(D.SYMBOL, "Symbol", T.Char(10), offset + 6),
        L.Field(D.EXPIRY_DATE, "ExpiryDate", T.LONG, offset + 16),
        L.Field(D.STRIKE_PRICE, "StrikePrice", T.LONG, offset + 20, STRIKE),
        L.Field(D.OPTION_TYPE, "OptionType", T.Char(2), offset + 24),
        L.Field(D.CA_LEVEL, "CALevel", T.SHORT, offset + 26),
    )


def _order_flags(offset):
    """``ST_ORDER_FLAGS``, Table 28 -- the big-endian table.

    Bit-for-bit different from Capital Market's structure of the same name:
    ``SL`` (Stop Loss) and ``MIT`` (Market If Touched) replace Capital
    Market's single ``OnStop`` bit, ``MF`` moves from byte 0 to byte 1's most
    significant bit, and there is no ``STPC`` bit at all -- two Reserved bits
    sit at the bottom of byte 1 where Capital Market's single ``STPC`` bit
    and one Reserved bit sit instead. Copying Capital Market's own
    ``_order_flags`` here would be silently wrong on every order.
    """
    return L.Flags("ST_ORDER_FLAGS", offset, 2, (
        D.FLAG_ATO, D.FLAG_MARKET, D.FLAG_SL, D.FLAG_MIT,
        D.FLAG_DAY, D.FLAG_GTC, D.FLAG_IOC, D.FLAG_AON,
        D.FLAG_MF, D.FLAG_MATCHED_IND, D.FLAG_TRADED, D.FLAG_MODIFIED,
        D.FLAG_FROZEN, D.FLAG_PREOPEN, None, None,
    ))


def _additional_order_flags(offset):
    """``ADDITIONAL_ORDER_FLAGS``, Table 29 -- one byte, a whole structure
    Capital Market has no equivalent of. ``STPC`` here is a *different* bit,
    in a different structure at a different offset, from Capital Market's
    own ``STPC`` bit of ``ST_ORDER_FLAGS`` -- they are not the same field
    under two names and must not share a dictionary tag (see
    ``dictionary.FLAG_STPC_ADDITIONAL``).
    """
    return L.Flags("ADDITIONAL_ORDER_FLAGS", offset, 1, (
        None, None, None, D.FLAG_STPC_ADDITIONAL, None, None,
        D.FLAG_COL, D.FLAG_BOC,
    ))


def _security_eligibility(offset):
    """``ST_STOCK_ELIGIBLE_INDICATORS``, Table 13 -- big-endian; byte 1 is a
    plain reserved byte, not itemised bits, per the transcription."""
    return L.Flags("SecurityEligibleIndicators", offset, 2, (
        D.SECURITY_AON, D.SECURITY_MIN_FILL, D.SECURITY_BOOKS_MERGED,
        None, None, None, None, None,
        None, None, None, None, None, None, None, None,
    ))


def _market_status(offset, normal, oddlot, spot, auction):
    """One of the three 8-byte per-market-status sub-structures (Tables 10,
    11 and 12) -- Normal/Oddlot/Spot/Auction, each a SHORT, at offsets
    0/2/4/6 within it. The three sub-structures share this exact shape;
    which dictionary tags they decode to is the caller's choice (see the
    module docstring on ``dictionary.py`` for why that choice matters).
    """
    return (
        L.Field(normal, "Normal", T.SHORT, offset),
        L.Field(oddlot, "Oddlot", T.SHORT, offset + 2),
        L.Field(spot, "Spot", T.SHORT, offset + 4),
        L.Field(auction, "Auction", T.SHORT, offset + 6),
    )


def _order_request_fields():
    """``MS_OE_REQUEST``, Table 26 -- 316 bytes.

    One structure for ten transaction codes that collide, byte for byte in
    position but not in size, with Capital Market's own ten-code order
    family: a fresh order, a modification, a cancellation, all three
    confirmations, both rejects, the error and the price confirmation. Which
    of them a message is says what the fields *mean*, never where they sit.
    """
    return _contract_desc(58) + (
        L.Field(D.PARTICIPANT_TYPE, "ParticipantType", T.Char(1), 40),
        L.Reserved(41, 1),
        L.Field(D.COMPETITOR_PERIOD, "CompetitorPeriod", T.SHORT, 42),
        L.Field(D.SOLICITOR_PERIOD, "SolicitorPeriod", T.SHORT, 44),
        L.Field(D.MOD_CXL_BY, "ModCxlBy", T.Char(1), 46),
        L.Reserved(47, 1),
        L.Field(D.REASON_CODE, "ReasonCode", T.SHORT, 48),
        L.Reserved(50, 4),
        L.Field(D.TOKEN_NO, "TokenNo", T.LONG, 54),
        L.Field(D.COUNTERPARTY_BROKER_ID, "CounterPartyBrokerId", T.Char(5), 86),
        L.Reserved(91, 1),
        L.Reserved(92, 2),
        L.Field(D.CLOSEOUT_FLAG, "CloseoutFlag", T.Char(1), 94),
        L.Reserved(95, 1),
        L.Field(D.ORDER_TYPE, "OrderType", T.SHORT, 96),
        L.Field(D.ORDER_NUMBER, "OrderNumber", T.DOUBLE, 98),
        L.Field(D.ACCOUNT_NUMBER, "AccountNumber", T.Char(10), 106),
        L.Field(D.BOOK_TYPE, "BookType", T.SHORT, 116),
        L.Field(D.BUY_SELL, "BuySellIndicator", T.SHORT, 118),
        L.Field(D.DISCLOSED_VOL, "DisclosedVolume", T.LONG, 120),
        L.Field(D.DISCLOSED_VOL_REMAINING, "DisclosedVolumeRemaining",
                T.LONG, 124),
        L.Field(D.TOTAL_VOL_REMAINING, "TotalVolumeRemaining", T.LONG, 128),
        L.Field(D.VOLUME, "Volume", T.LONG, 132),
        L.Field(D.VOLUME_FILLED_TODAY, "VolumeFilledToday", T.LONG, 136),
        L.Field(D.PRICE, "Price", T.LONG, 140, PAISE),
        L.Field(D.TRIGGER_PRICE, "TriggerPrice", T.LONG, 144, PAISE),
        L.Field(D.GOOD_TILL_DATE, "GoodTillDate", T.LONG, 148),
        L.Field(D.ENTRY_DATE_TIME, "EntryDateTime", T.LONG, 152),
        L.Field(D.MIN_FILL_AON, "MinimumFillOrAonVolume", T.LONG, 156),
        L.Field(D.LAST_MODIFIED, "LastModified", T.LONG, 160),
        _order_flags(164),
        L.Field(D.BRANCH_ID, "BranchId", T.SHORT, 166),
        L.Field(D.TRADER_ID, "TraderId", T.LONG, 168),
        L.Field(D.BROKER_ID, "BrokerId", T.Char(5), 172),
        # "cOrdFiller": undocumented in the field-description tables (p.64-68
        # name every other field in this structure); transcribed as reserved
        # rather than guessed at (transcription §6).
        L.Reserved(177, 24, "cOrdFiller"),
        L.Field(D.OPEN_CLOSE, "OpenClose", T.Char(1), 201),
        L.Field(D.SETTLOR, "Settlor", T.Char(12), 202),
        L.Field(D.PRO_CLIENT, "ProClient", T.SHORT, 214),
        L.Field(D.SETTLEMENT_PERIOD, "SettlementPeriod", T.SHORT, 216),
        _additional_order_flags(218),
        L.Reserved(219, 1),
        # A sixteen-bit placeholder bitfield plus two spare bytes -- named
        # individually in the appendix's own extraction (Filler1..Filler18)
        # but nowhere given meaning in the field-description tables. See
        # transcription §1.4/§6.
        L.Reserved(220, 4),
        L.Field(D.NNF_FIELD, "NNFField", T.DOUBLE, 224),
        # LONG LONG here, not Capital Market's DOUBLE ExecTimeStamp at the
        # analogous position -- transcription §1.4.
        L.Field(D.MKT_REPLAY, "MktReplay", T.LONG_LONG, 232),
        L.Field(D.PAN, "PAN", T.Char(10), 240),
        L.Field(D.ALGO_ID, "AlgoID", T.LONG, 250),
        L.Reserved(254, 2),
        L.Field(D.LAST_ACTIVITY_REFERENCE, "LastActivityReference",
                T.LONG_LONG, 256),
        L.Reserved(264, 52),
    )


def _price_mod_fields():
    """``PRICE_MOD``, Table 30 -- 106 bytes, an F&O-only optimisation that
    changes only an order's price without resending the whole order.
    """
    return (
        L.Field(D.TOKEN_NO, "TokenNo", T.LONG, 40),
        L.Field(D.TRADER_ID, "TraderID", T.LONG, 44),
        L.Field(D.ORDER_NUMBER, "OrderNumber", T.DOUBLE, 48),
        L.Field(D.BUY_SELL, "BuySell", T.SHORT, 56),
        L.Field(D.PRICE, "Price", T.LONG, 58, PAISE),
        L.Field(D.VOLUME, "Volume", T.LONG, 62),
        L.Field(D.LAST_MODIFIED, "LastModified", T.LONG, 66),
        L.Field(D.REFERENCE, "Reference", T.Char(4), 70),
        L.Field(D.LAST_ACTIVITY_REFERENCE, "LastActivityReference",
                T.LONG_LONG, 74),
        L.Reserved(82, 24),
    )


def _trade_fields():
    """``MS_TRADE_CONFIRM``, Table 37 -- 296 bytes, against Capital Market's
    228. The extra 68 bytes are Futures/Options-only concepts: open/close on
    the trade, the clearing participant, the PAN before a modification, and
    a full ``ADDITIONAL_ORDER_FLAGS`` byte plus its padding.
    """
    return (
        L.Field(D.RESPONSE_ORDER_NUMBER, "ResponseOrderNumber", T.DOUBLE, 40),
        L.Field(D.BROKER_ID, "BrokerId", T.Char(5), 48),
        L.Reserved(53, 1),
        L.Field(D.TRADER_NUMBER, "TraderNumber", T.LONG, 54),
        L.Field(D.ACCOUNT_NUMBER, "AccountNumber", T.Char(10), 58),
        L.Field(D.BUY_SELL, "BuySellIndicator", T.SHORT, 68),
        L.Field(D.VOLUME, "OriginalVolume", T.LONG, 70),
        L.Field(D.DISCLOSED_VOL, "DisclosedVolume", T.LONG, 74),
        L.Field(D.TOTAL_VOL_REMAINING, "RemainingVolume", T.LONG, 78),
        L.Field(D.DISCLOSED_VOL_REMAINING, "DisclosedVolumeRemaining",
                T.LONG, 82),
        L.Field(D.PRICE, "Price", T.LONG, 86, PAISE),
        _order_flags(90),
        L.Field(D.GOOD_TILL_DATE, "GoodTillDate", T.LONG, 92),
        L.Field(D.FILL_NUMBER, "FillNumber", T.LONG, 96),
        L.Field(D.FILL_QTY, "FillQuantity", T.LONG, 100),
        L.Field(D.FILL_PRICE, "FillPrice", T.LONG, 104, PAISE),
        L.Field(D.VOLUME_FILLED_TODAY, "VolumeFilledToday", T.LONG, 108),
        L.Field(D.ACTIVITY_TYPE, "ActivityType", T.Char(2), 112),
        L.Field(D.ACTIVITY_TIME, "ActivityTime", T.LONG, 114),
        L.Field(D.COUNTER_TRADER_ORDER_NUMBER, "CounterTraderOrderNumber",
                T.DOUBLE, 118),
        L.Field(D.COUNTER_BROKER_ID, "CounterBrokerId", T.Char(5), 126),
        # Odd offset (131) under pragma pack 2 -- the next field is a LONG
        # and needs even alignment, so a one-byte pad is required before it.
        # The document's own printed offset for Token (132) confirms the pad.
        L.Reserved(131, 1),
        L.Field(D.TOKEN_NO, "Token", T.LONG, 132),
    ) + _contract_desc(136) + (
        L.Field(D.OPEN_CLOSE, "OpenClose", T.Char(1), 164),
        L.Field(D.OLD_OPEN_CLOSE, "OldOpenClose", T.Char(1), 165),
        # A one-byte CHAR here, not the order structure's SHORT BookType --
        # the value domain is the same, transcribed with a different wire
        # type where the document gives it one (dictionary.BOOK_TYPE covers
        # both representations).
        L.Field(D.BOOK_TYPE, "BookType", T.Char(1), 166),
        # A second odd-offset pad (167): BookType ends at 167, and the
        # following Reserved LONG needs even alignment. Not called out in
        # the transcription by name but required by the same pack(2) rule.
        L.Reserved(167, 1),
        L.Reserved(168, 4),
        L.Field(D.OLD_ACCOUNT_NUMBER, "OldAccountNumber", T.Char(10), 172),
        L.Field(D.PARTICIPANT, "Participant", T.Char(12), 182),
        L.Field(D.OLD_PARTICIPANT, "OldParticipant", T.Char(12), 194),
        _additional_order_flags(206),
        L.Reserved(207, 1),
        L.Reserved(208, 1),
        L.Reserved(209, 1, "ReservedFiller2"),
        L.Field(D.PAN, "PAN", T.Char(10), 210),
        L.Field(D.OLD_PAN, "OldPAN", T.Char(10), 220),
        L.Field(D.ALGO_ID, "AlgoID", T.LONG, 230),
        L.Reserved(234, 2),
        L.Field(D.LAST_ACTIVITY_REFERENCE, "LastActivityReference",
                T.LONG_LONG, 236),
        L.Reserved(244, 52),
    )


def _signon_fields():
    """``MS_SIGNON``, Tables 6/7 -- 278 bytes, the *same* request and
    response transaction codes Capital Market uses (2300/2301) but its own,
    incompatible structure -- a critical safety finding (transcription §1.7).
    One shared tuple serves both directions, the same way Capital Market's
    own ``_signon_fields`` does, even though a few fields carry a different
    meaning by direction (``Batch2StartTime`` on the request is ``EndTime``
    on the response; ``HostSwitchContext`` and ``WsClassName`` are request
    -only and reserved on the response).
    """
    return (
        L.Field(D.SIGNON_USER_ID, "UserId", T.LONG, 40),
        L.Reserved(44, 8),
        L.Field(D.PASSWORD, "Password", T.Char(8), 52, redact=True),
        L.Reserved(60, 8),
        L.Field(D.NEW_PASSWORD, "NewPassword", T.Char(8), 68, redact=True),
        L.Field(D.TRADER_NAME, "TraderName", T.Char(26), 76),
        L.Field(D.LAST_PASSWORD_CHANGE, "LastPasswordChangeDate", T.LONG, 102),
        L.Field(D.BROKER_ID, "BrokerId", T.Char(5), 106),
        L.Reserved(111, 1),
        L.Field(D.BRANCH_ID, "BranchId", T.SHORT, 112),
        L.Field(D.VERSION_NUMBER, "VersionNumber", T.LONG, 114),
        L.Field(D.BATCH2_START_TIME, "Batch2StartTimeOrEndTime", T.LONG, 118),
        L.Field(D.HOST_SWITCH_CONTEXT, "HostSwitchContext", T.Char(1), 122),
        L.Field(D.COLOUR, "Colour", T.Char(50), 123),
        L.Reserved(173, 1),
        L.Field(D.USER_TYPE, "UserType", T.SHORT, 174),
        L.Field(D.SEQUENCE_NUMBER, "SequenceNumber", T.DOUBLE, 176),
        L.Field(D.WS_CLASS_NAME, "WsClassName", T.Char(14), 184),
        L.Field(D.BROKER_STATUS, "BrokerStatus", T.Char(1), 198),
        L.Field(D.SHOW_INDEX, "ShowIndex", T.Char(1), 199),
        # ST_BROKER_ELIGIBILITY_PER_MKT: no per-bit table found in the pages
        # read (unlike ST_ORDER_FLAGS and ADDITIONAL_ORDER_FLAGS, which cite
        # Tables 28/29 explicitly). Transcribed as reserved rather than
        # guessed at, consistent with this project's rule of refusing to
        # invent venue behaviour a spec does not state.
        L.Reserved(200, 2, "ST_BROKER_ELIGIBILITY_PER_MKT"),
        L.Field(D.MEMBER_TYPE, "MemberType", T.SHORT, 202),
        L.Field(D.CLEARING_STATUS, "ClearingStatus", T.Char(1), 204),
        # 25 bytes here, not Capital Market's 26 -- one of the concrete
        # field-width differences the "same codes, different structure"
        # finding rests on.
        L.Field(D.BROKER_NAME, "BrokerName", T.Char(25), 205),
        L.Reserved(230, 16),
        L.Reserved(246, 16),
        L.Reserved(262, 16),
    )


def _system_information_fields():
    """``MS_SYSTEM_INFO_DATA``, Table 9 -- 106 bytes, against Capital
    Market's 94. The extra space is three per-market-status sub-structures
    where Capital Market has one flat set of fields, plus an options-pricing
    input (``RiskFreeInterestRate``) Capital Market has no use for.
    """
    return (
        _market_status(40, D.NORMAL_STATUS, D.ODDLOT_STATUS, D.SPOT_STATUS,
                       D.AUCTION_STATUS)
        + _market_status(48, D.EX_NORMAL_STATUS, D.EX_ODDLOT_STATUS,
                         D.EX_SPOT_STATUS, D.EX_AUCTION_STATUS)
        + _market_status(56, D.PL_NORMAL_STATUS, D.PL_ODDLOT_STATUS,
                         D.PL_SPOT_STATUS, D.PL_AUCTION_STATUS)
        + (
            L.Field(D.UPDATE_PORTFOLIO, "UpdatePortfolio", T.Char(1), 64),
            L.Field(D.MARKET_INDEX, "MarketIndex", T.LONG, 65),
            L.Field(D.DEFAULT_SETTLEMENT_NORMAL,
                    "DefaultSettlementPeriodNormal", T.SHORT, 69),
            L.Field(D.DEFAULT_SETTLEMENT_SPOT, "DefaultSettlementPeriodSpot",
                    T.SHORT, 71),
            L.Field(D.DEFAULT_SETTLEMENT_AUCTION,
                    "DefaultSettlementPeriodAuction", T.SHORT, 73),
            L.Field(D.COMPETITOR_PERIOD, "CompetitorPeriod", T.SHORT, 75),
            L.Field(D.SOLICITOR_PERIOD, "SolicitorPeriod", T.SHORT, 77),
            L.Field(D.WARNING_PERCENT, "WarningPercent", T.SHORT, 79),
            L.Field(D.VOLUME_FREEZE_PERCENT, "VolumeFreezePercent",
                    T.SHORT, 81),
            L.Field(D.SNAP_QUOTE_TIME, "SnapQuoteTime", T.SHORT, 83),
            L.Reserved(85, 2),
            L.Field(D.BOARD_LOT_QUANTITY, "BoardLotQuantity", T.LONG, 87),
            L.Field(D.TICK_SIZE, "TickSize", T.LONG, 91, PAISE),
            L.Field(D.MAXIMUM_GTC_DAYS, "MaximumGtcDays", T.SHORT, 95),
            _security_eligibility(97),
            L.Field(D.DISCLOSED_QUANTITY_PERCENT,
                    "DisclosedQuantityPercentAllowed", T.SHORT, 99),
            L.Field(D.RISK_FREE_INTEREST_RATE, "RiskFreeInterestRate",
                    T.LONG, 101),
            L.Reserved(105, 1),
        )
    )


def build_fo():
    """Every Futures & Options structure, by transaction code."""
    layouts = L.NnfDictionary(HEADER)

    # -- the box connection, byte-for-byte identical to Capital Market's ----
    layouts.define(X.GR_REQUEST, "MS_GR_REQUEST", 48, (
        L.Field(D.BOX_ID, "BoxId", T.SHORT, 40),
        L.Field(D.BROKER_ID, "BrokerID", T.Char(5), 42),
        L.Reserved(47, 1, "Filler"),
    ))
    layouts.define(X.GR_RESPONSE, "MS_GR_RESPONSE", 136, (
        L.Field(D.BOX_ID, "BoxId", T.SHORT, 40),
        L.Field(D.BROKER_ID, "BrokerID", T.Char(5), 42),
        L.Reserved(47, 1, "Filler"),
        L.Field(D.IP_ADDRESS, "IPAddress", T.Char(16), 48),
        L.Field(D.PORT, "Port", T.LONG, 64),
        L.Field(D.SESSION_KEY, "SessionKey", T.Char(8), 68, redact=True),
        L.Field(D.CRYPTOGRAPHIC_KEY, "CryptographicKey", T.Raw(32), 76,
                redact=True),
        L.Field(D.STATIC_IV, "StaticCryptographicIV", T.Raw(8), 108,
                redact=True),
        L.Field(D.DYNAMIC_IV, "DynamicCryptographicIV", T.LONG_LONG, 116,
                redact=True),
        L.Field(D.ADDITIONAL_KEY, "CryptographicAdditionalKey", T.Raw(12),
                124, redact=True),
    ))
    layouts.define(X.SECURE_BOX_REGISTRATION_REQUEST_IN,
                   "MS_SECURE_BOX_REGISTRATION_REQUEST_IN", 42, (
                       L.Field(D.BOX_ID, "BoxId", T.SHORT, 40),))
    layouts.define(X.SECURE_BOX_REGISTRATION_REQUEST_OUT,
                   "MS_SECURE_BOX_REGISTRATION_RESPONSE_OUT", 40, ())
    layouts.define(X.BOX_SIGN_ON_REQUEST_IN, "MS_BOX_SIGN_ON_REQUEST_IN", 60, (
        L.Field(D.BOX_ID, "BoxId", T.SHORT, 40),
        L.Field(D.BROKER_ID, "BrokerID", T.Char(5), 42),
        L.Reserved(47, 5),
        L.Field(D.SESSION_KEY, "SessionKey", T.Char(8), 52, redact=True),
    ))
    # 52 bytes per the detailed table (matching Capital Market exactly); the
    # appendix summary table says 54 -- see the module docstring.
    layouts.define(X.BOX_SIGN_ON_REQUEST_OUT, "MS_BOX_SIGN_ON_REQUEST_OUT", 52, (
        L.Field(D.BOX_ID, "BoxId", T.SHORT, 40),
        L.Reserved(42, 10),
    ))
    # New in this venue: how a box learns why the exchange terminated its
    # connection. Not present in Capital Market's transactions.py at all.
    layouts.define(X.BOX_SIGN_OFF, "MS_BOX_SIGN_OFF", 42, (
        L.Field(D.BOX_ID, "BoxId", T.SHORT, 40),
    ))

    # -- the user session -------------------------------------------------
    layouts.define(X.SIGN_ON_REQUEST_IN, "MS_SIGNON", 278, _signon_fields())
    layouts.define(X.SIGN_ON_REQUEST_OUT, "MS_SIGNON", 278, _signon_fields())
    layouts.define(X.SIGN_OFF_REQUEST_IN, "SIGN_OFF_REQUEST_IN", 40, ())
    # 190 bytes: the best reading of a structure that disagrees with itself
    # across its own two tables -- see the module docstring.
    layouts.define(X.SIGN_OFF_REQUEST_OUT, "SIGNOFF_OUT", 190, (
        L.Field(D.SIGNOFF_USER_ID, "UserId", T.LONG, 40),
        L.Reserved(44, 146),
    ))
    layouts.define(X.HEARTBEAT, "HEARTBEAT", 40, ())
    # Keyed by a bare contract token ("Key"), not Capital Market's
    # Symbol+Series SEC_INFO -- transcription §4.
    layouts.define(X.ERROR_RESPONSE_OUT, "MS_ERROR_RESPONSE", 182, (
        L.Field(D.KEY, "Key", T.Char(14), 40),
        L.Field(D.ERROR_MESSAGE, "ErrorMessage", T.Char(128), 54),
    ))
    layouts.define(X.SYSTEM_INFORMATION_IN, "MS_SYSTEM_INFO_REQ", 44, (
        L.Field(D.LAST_UPDATE_PORTFOLIO_TIME, "LastUpdatePortfolioTime",
                T.LONG, 40),
    ))
    layouts.define(X.SYSTEM_INFORMATION_OUT, "MS_SYSTEM_INFO_DATA", 106,
                   _system_information_fields())

    # -- order entry: ten transaction codes, one 316-byte structure --------
    for code in (X.BOARD_LOT_IN, X.ORDER_MOD_IN, X.ORDER_CANCEL_IN,
                 X.ORDER_CONFIRMATION, X.ORDER_MOD_CONFIRMATION,
                 X.ORDER_CANCEL_CONFIRMATION, X.ORDER_ERROR,
                 X.ORDER_MOD_REJECT, X.ORDER_CANCEL_REJECT,
                 X.PRICE_CONFIRMATION):
        layouts.define(code, "MS_OE_REQUEST", 316, _order_request_fields())

    # -- the price-only modification, new in this venue --------------------
    layouts.define(X.PRICE_MOD_IN, "PRICE_MOD", 106, _price_mod_fields())

    # -- trades --------------------------------------------------------
    layouts.define(X.TRADE_CONFIRMATION, "MS_TRADE_CONFIRM", 296,
                   _trade_fields())

    # -- recovery -------------------------------------------------------
    #
    # The request is defined so it can be read and refused by name; the
    # download response records are not, for the same reason Capital
    # Market's own layouts.py gives: they are wrapped in INNER_MESSAGE_HEADER
    # and range from 80 to 548 bytes, and every structure here is fixed
    # width.
    layouts.define(X.DOWNLOAD_REQUEST, "MS_MESSAGE_DOWNLOAD", 48, (
        L.Field(D.DOWNLOAD_SEQUENCE, "SequenceNumber", T.DOUBLE, 40),))
    # The download's three answers. Header and trailer are a bare header and
    # nothing else; the record carries one recovered message whole, which is
    # why it is the one structure here with no published length -- see
    # nnf/layout.py:RecordLayout for the inner header it wraps.
    layouts.define(X.HEADER_RECORD, "MESSAGE_HEADER", 40, ())
    layouts.define(X.TRAILER_RECORD, "MESSAGE_HEADER", 40, ())
    layouts.define_record(X.MESSAGE_RECORD, "MESSAGE_RECORD", D.DOWNLOAD_DATA)

    return layouts
