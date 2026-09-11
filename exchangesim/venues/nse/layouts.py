"""Every Capital Market structure, transcribed offset by offset.

This module is meant to be read beside the PDF: each table below is one table of
the specification, in the same order, with the same offsets. Nothing is
computed. The structures are ``pragma pack 2`` and pad odd runs to even, so a
walk that summed widths would drift, and a drifted layout produces messages that
frame correctly and mean something else.

``tests/test_nse_dictionary.py`` checks the one property that catches almost
every transcription slip mechanically: **the fields of each structure must reach
exactly the packet length the appendix tables for it** -- 290 for an order, 228
for a trade, 276 for a sign-on, 94 for the system information. A field written
at the wrong offset or with the wrong width moves the end of the structure, and
the test names the transaction code.

One discrepancy is deliberate and worth knowing about: the appendix's summary
table gives ``SYSTEM_INFORMATION_DATA`` as 90 bytes, while Chapter 3's own table
for the structure gives 94 and lists fields that reach 94. The detailed table
wins, and the disagreement is recorded in ``rules.ASSUMPTIONS``.
"""

from ...nnf import layout as L
from ...nnf import types as T
from . import dictionary as D
from . import transactions as X

#: Every structure is prefaced with this, and it is the same forty bytes for
#: every transaction code (Chapter 10). ``TimeStamp1`` and ``TimeStamp2`` are
#: eight-byte opaque runs rather than numbers -- the specification says one
#: carries two four-byte values to be echoed back and the other a machine
#: number, so neither is a quantity anything here should do arithmetic on.
HEADER = L.HeaderLayout(40, (
    L.Field(L.TRANSACTION_CODE, "TransactionCode", T.SHORT, 0),
    L.Field(D.LOG_TIME, "LogTime", T.LONG, 2),
    L.Field(D.ALPHA_CHAR, "AlphaChar", T.Char(2), 6),
    L.Field(D.USER_ID, "UserId", T.LONG, 8),
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


def _sec_info(offset):
    """``SEC_INFO``: the symbol and series that name a security, twelve bytes."""
    return (
        L.Field(D.SYMBOL, "Symbol", T.Char(10), offset),
        L.Field(D.SERIES, "Series", T.Char(2), offset + 10),
    )


def _order_flags(offset):
    """``ST_ORDER_FLAGS``, Table 19.2 -- the big-endian one.

    The document publishes this twice, once per byte order, which is what makes
    the bit numbering a transcription rather than the assumption it had to be at
    HKEX. The host is big-endian, so the most significant bit of the first byte
    is ``ATO``.
    """
    return L.Flags("ST_ORDER_FLAGS", offset, 2, (
        D.FLAG_ATO, D.FLAG_MARKET, D.FLAG_ON_STOP, D.FLAG_DAY,
        D.FLAG_GTC, D.FLAG_IOC, D.FLAG_AON, D.FLAG_MF,
        D.FLAG_MATCHED_IND, D.FLAG_TRADED, D.FLAG_MODIFIED, D.FLAG_FROZEN,
        D.FLAG_PREOPEN, None, D.FLAG_STPC, None,
    ))


def _broker_eligibility(offset):
    """``BrokerEligibilityPerMarket``, Table 7.2 -- big-endian."""
    return L.Flags("BrokerEligibilityPerMarket", offset, 2, (
        D.ELIGIBLE_NORMAL, D.ELIGIBLE_ODDLOT, D.ELIGIBLE_SPOT,
        D.ELIGIBLE_AUCTION, D.ELIGIBLE_CALL_AUCTION1, D.ELIGIBLE_CALL_AUCTION2,
        None, None,
        None, None, None, None, None, None, None, D.ELIGIBLE_PREOPEN,
    ))


def _security_eligibility(offset):
    """``SECURITY ELIGIBLE INDICATORS``, Table 10.2 -- big-endian."""
    return L.Flags("SecurityEligibleIndicators", offset, 2, (
        D.SECURITY_AON, D.SECURITY_MIN_FILL, D.SECURITY_BOOKS_MERGED,
        None, None, None, None, None,
        None, None, None, None, None, None, None, None,
    ))


def _order_entry_fields():
    """``ORDER_ENTRY_REQUEST`` / ``RESPONSE``, Table 19 -- 290 bytes.

    One structure for fourteen transaction codes: a fresh order, a modification,
    a cancellation, all three confirmations, both rejects, the error and the
    price confirmation. Which of them a message is says what the fields *mean*,
    never where they sit.
    """
    return (
        L.Field(D.PARTICIPANT_TYPE, "ParticipantType", T.Char(1), 40),
        L.Reserved(41, 1),
        L.Field(D.COMPETITOR_PERIOD, "CompetitorPeriod", T.SHORT, 42),
        L.Field(D.SOLICITOR_PERIOD, "SolicitorPeriod", T.SHORT, 44),
        L.Field(D.MOD_CXL_BY, "ModCxlBy", T.Char(1), 46),
        L.Reserved(47, 1, "Filler"),
        L.Field(D.REASON_CODE, "ReasonCode", T.SHORT, 48),
        L.Reserved(50, 4),
    ) + _sec_info(54) + (
        L.Field(D.AUCTION_NUMBER, "AuctionNumber", T.SHORT, 66),
        L.Field(D.OP_BROKER_ID, "OpBrokerId", T.Char(5), 68),
        L.Field(D.SUSPENDED, "Suspended", T.Char(1), 73),
        L.Field(D.ORDER_NUMBER, "OrderNumber", T.DOUBLE, 74),
        L.Field(D.ACCOUNT_NUMBER, "AccountNumber", T.Char(10), 82),
        L.Field(D.BOOK_TYPE, "BookType", T.SHORT, 92),
        L.Field(D.BUY_SELL, "BuySell", T.SHORT, 94),
        L.Field(D.DISCLOSED_VOL, "DisclosedVol", T.LONG, 96),
        L.Field(D.DISCLOSED_VOL_REMAINING, "DisclosedVolRemaining", T.LONG, 100),
        L.Field(D.TOTAL_VOL_REMAINING, "TotalVolRemaining", T.LONG, 104),
        L.Field(D.VOLUME, "Volume", T.LONG, 108),
        L.Field(D.VOLUME_FILLED_TODAY, "VolumeFilledToday", T.LONG, 112),
        L.Field(D.PRICE, "Price", T.LONG, 116, L.PAISE),
        L.Field(D.TRIGGER_PRICE, "TriggerPrice", T.LONG, 120, L.PAISE),
        L.Field(D.GOOD_TILL_DATE, "GoodTillDate", T.LONG, 124),
        L.Field(D.ENTRY_DATE_TIME, "EntryDateTime", T.LONG, 128),
        L.Field(D.MIN_FILL_AON, "MinFillAon", T.LONG, 132),
        L.Field(D.LAST_MODIFIED, "LastModified", T.LONG, 136),
        _order_flags(140),
        L.Field(D.BRANCH_ID, "BranchId", T.SHORT, 142),
        L.Field(D.TRADER_ID, "TraderId", T.LONG, 144),
        L.Field(D.BROKER_ID, "BrokerId", T.Char(5), 148),
        L.Field(D.OE_REMARKS, "OERemarks", T.Char(25), 153),
        L.Field(D.SETTLOR, "Settlor", T.Char(12), 178),
        L.Field(D.PRO_CLIENT, "ProClient", T.SHORT, 190),
        L.Field(D.SETTLEMENT_TYPE, "SettlementType", T.SHORT, 192),
        L.Field(D.NNF_FIELD, "NNFField", T.DOUBLE, 194),
        L.Field(D.EXEC_TIMESTAMP, "ExecTimeStamp", T.DOUBLE, 202),
        L.Reserved(210, 4),
        L.Field(D.PAN, "PAN", T.Char(10), 214),
        L.Field(D.ALGO_ID, "AlgoID", T.LONG, 224),
        L.Reserved(228, 2, "ReservedFiller"),
        L.Field(D.LAST_ACTIVITY_REFERENCE, "LastActivityReference",
                T.LONG_LONG, 230),
        L.Reserved(238, 52),
    )


def _trade_fields():
    """``MS_TRADE_CONFIRM``, Table 21 -- 228 bytes."""
    return (
        L.Field(D.RESPONSE_ORDER_NUMBER, "ResponseOrderNumber", T.DOUBLE, 40),
        L.Field(D.BROKER_ID, "BrokerId", T.Char(5), 48),
        L.Reserved(53, 1),
        L.Field(D.TRADER_NUM, "TraderNum", T.LONG, 54),
        L.Field(D.ACCOUNT_NUMBER, "AccountNum", T.Char(10), 58),
        L.Field(D.BUY_SELL, "BuySell", T.SHORT, 68),
        L.Field(D.ORIGINAL_VOL, "OriginalVol", T.LONG, 70),
        L.Field(D.DISCLOSED_VOL, "DisclosedVol", T.LONG, 74),
        L.Field(D.REMAINING_VOL, "RemainingVol", T.LONG, 78),
        L.Field(D.DISCLOSED_VOL_REMAINING, "DisclosedVolRemaining", T.LONG, 82),
        L.Field(D.PRICE, "Price", T.LONG, 86, L.PAISE),
        _order_flags(90),
        L.Field(D.GTD, "Gtd", T.LONG, 92),
        L.Field(D.FILL_NUMBER, "FillNumber", T.LONG, 96),
        L.Field(D.FILL_QTY, "FillQty", T.LONG, 100),
        L.Field(D.FILL_PRICE, "FillPrice", T.LONG, 104, L.PAISE),
        L.Field(D.VOLUME_FILLED_TODAY, "VolFilledToday", T.LONG, 108),
        L.Field(D.ACTIVITY_TYPE, "ActivityType", T.Char(2), 112),
        L.Field(D.ACTIVITY_TIME, "ActivityTime", T.LONG, 114),
        L.Field(D.OP_ORDER_NUMBER, "OpOrderNumber", T.DOUBLE, 118),
        L.Field(D.OP_BROKER_ID, "OpBrokerId", T.Char(5), 126),
    ) + _sec_info(131) + (
        L.Reserved(143, 1),
        L.Field(D.BOOK_TYPE, "BookType", T.SHORT, 144),
        L.Field(D.NEW_VOLUME, "NewVolume", T.LONG, 146),
        L.Field(D.PRO_CLIENT, "ProClient", T.SHORT, 150),
        L.Field(D.PAN, "PAN", T.Char(10), 152),
        L.Field(D.ALGO_ID, "AlgoID", T.LONG, 162),
        L.Reserved(166, 2, "ReservedFiller"),
        L.Field(D.LAST_ACTIVITY_REFERENCE, "LastActivityReference",
                T.LONG_LONG, 168),
        L.Reserved(176, 52),
    )


def _signon_fields():
    """``SIGNON_IN`` / ``SIGNON_OUT``, Table 7 -- 276 bytes.

    ``SequenceNumber`` here is not a sequence number at all: the document says
    it "contains the time when the markets closed the previous trading day", and
    a client that sees it change must clear its local database. It is named
    ``LastMarketClose`` above the wire for that reason.
    """
    return (
        L.Field(D.SIGNON_USER_ID, "UserId", T.LONG, 40),
        L.Reserved(44, 8),
        L.Field(D.PASSWORD, "Password", T.Char(8), 52, redact=True),
        L.Reserved(60, 8),
        L.Field(D.NEW_PASSWORD, "NewPassword", T.Char(8), 68, redact=True),
        L.Field(D.TRADER_NAME, "TraderName", T.Char(26), 76),
        L.Field(D.LAST_PASSWORD_CHANGE, "LastPasswordChangeDateTime",
                T.LONG, 102),
        L.Field(D.BROKER_ID, "BrokerId", T.Char(5), 106),
        L.Reserved(111, 1),
        L.Field(D.BRANCH_ID, "BranchId", T.SHORT, 112),
        L.Field(D.VERSION_NUMBER, "VersionNumber", T.LONG, 114),
        L.Reserved(118, 56),
        L.Field(D.USER_TYPE, "UserType", T.SHORT, 174),
        L.Field(D.LAST_MARKET_CLOSE, "SequenceNumber", T.DOUBLE, 176),
        L.Field(D.WORKSTATION_NUMBER, "WorkstationNumber", T.Char(14), 184),
        L.Field(D.BROKER_STATUS, "BrokerStatus", T.Char(1), 198),
        L.Field(D.SHOW_INDEX, "ShowIndex", T.Char(1), 199),
        _broker_eligibility(200),
        L.Field(D.BROKER_NAME, "BrokerName", T.Char(26), 202),
        L.Reserved(228, 16),
        L.Reserved(244, 16),
        L.Reserved(260, 16),
    )


def _system_information_fields():
    """``SYSTEM_INFORMATION_DATA``, Table 10 -- 94 bytes."""
    return (
        L.Field(D.NORMAL_STATUS, "Normal", T.SHORT, 40),
        L.Field(D.ODDLOT_STATUS, "Oddlot", T.SHORT, 42),
        L.Field(D.SPOT_STATUS, "Spot", T.SHORT, 44),
        L.Field(D.AUCTION_STATUS, "Auction", T.SHORT, 46),
        L.Field(D.CALL_AUCTION1_STATUS, "CallAuction1", T.SHORT, 48),
        L.Field(D.CALL_AUCTION2_STATUS, "CallAuction2", T.SHORT, 50),
        L.Field(D.MARKET_INDEX, "MarketIndex", T.LONG, 52),
        L.Field(D.DEFAULT_SETTLEMENT_NORMAL, "DefaultSettlementPeriodNormal",
                T.SHORT, 56),
        L.Field(D.DEFAULT_SETTLEMENT_SPOT, "DefaultSettlementPeriodSpot",
                T.SHORT, 58),
        L.Field(D.DEFAULT_SETTLEMENT_AUCTION,
                "DefaultSettlementPeriodAuction", T.SHORT, 60),
        L.Field(D.COMPETITOR_PERIOD, "CompetitorPeriod", T.SHORT, 62),
        L.Field(D.SOLICITOR_PERIOD, "SolicitorPeriod", T.SHORT, 64),
        L.Field(D.WARNING_PERCENT, "WarningPercent", T.SHORT, 66),
        L.Field(D.VOLUME_FREEZE_PERCENT, "VolumeFreezePercent", T.SHORT, 68),
        L.Reserved(70, 2),
        L.Field(D.TERMINAL_IDLE_TIME, "TerminalIdleTime", T.SHORT, 72),
        L.Field(D.BOARD_LOT_QUANTITY, "BoardLotQuantity", T.LONG, 74),
        L.Field(D.TICK_SIZE, "TickSize", T.LONG, 78, L.PAISE),
        L.Field(D.MAXIMUM_GTC_DAYS, "MaximumGtcDays", T.SHORT, 82),
        _security_eligibility(84),
        L.Field(D.DISCLOSED_QUANTITY_PERCENT,
                "DisclosedQuantityPercentAllowed", T.SHORT, 86),
        L.Reserved(88, 6),
    )


# -- the "trimmed" order flow -------------------------------------------------
#
# Chapter 10 appendix, Tables 57-60, and the shape a real Direct Interface
# gateway actually sends: "the Request messages in transaction codes
# [BOARD_LOT_IN, ORDER_MOD_IN, ORDER_CANCEL_IN] must have BookType 1 or 11 or
# 12", and MS_OE_REQUEST "is not allowed" with those book types at all.
# Regular Lot is BookType 1, the only book this venue trades, so this family
# is not an optional extra -- it is what a member actually sends for every
# order this venue accepts.
#
# None of these carry the forty-byte MESSAGE_HEADER: each opens with a
# compact prefix of its own, so each gets its own `L.HeaderLayout`. The order
# entry request and the modify/cancel pair do not even share a prefix with
# each other -- see the two headers below -- which is the clearest sign that
# "trimmed" describes an outcome, not one shared structure.


def _oe_request_tr_fields():
    """``ORDER_ENTRY_REQUEST_TR``, Table 57 -- 136 bytes."""
    return _sec_info(6) + (
        L.Field(D.ACCOUNT_NUMBER, "AccountNumber", T.Char(10), 18),
        L.Field(D.BOOK_TYPE, "BookType", T.SHORT, 28),
        L.Field(D.BUY_SELL, "BuySell", T.SHORT, 30),
        L.Field(D.DISCLOSED_VOL, "DisclosedVol", T.LONG, 32),
        L.Field(D.VOLUME, "Volume", T.LONG, 36),
        L.Field(D.PRICE, "Price", T.LONG, 40, L.PAISE),
        L.Field(D.GOOD_TILL_DATE, "GoodTillDate", T.LONG, 44),
        _order_flags(48),
        L.Field(D.BRANCH_ID, "BranchId", T.SHORT, 50),
        L.Field(D.TRADER_ID, "TraderId", T.LONG, 52),
        L.Field(D.BROKER_ID, "BrokerId", T.Char(5), 56),
        L.Field(D.SUSPENDED, "Suspended", T.Char(1), 61),
        L.Field(D.SETTLOR, "Settlor", T.Char(12), 62),
        L.Field(D.PRO_CLIENT, "ProClient", T.SHORT, 74),
        L.Field(D.NNF_FIELD, "NNFField", T.DOUBLE, 76),
        L.Field(D.TRANSACTION_ID, "TransactionId", T.LONG, 84),
        L.Field(D.PAN, "PAN", T.Char(10), 88),
        L.Field(D.ALGO_ID, "AlgoID", T.LONG, 98),
        L.Reserved(102, 2, "ReservedFiller"),
        L.Reserved(104, 32),
    )


#: ORDER_ENTRY_REQUEST_TR's prefix: six bytes where the plain header keeps
#: forty.
#: The document calls this field "TraderId", but it plays exactly the role
#: the plain header's "UserId" does -- the one field ``NnfSessionManager``
#: stamps and a box routes an inbound message by -- so it keeps that tag.
HEADER_OE_TR = L.HeaderLayout(6, (
    L.Field(L.TRANSACTION_CODE, "TransactionCode", T.SHORT, 0),
    L.Field(D.USER_ID, "UserId", T.LONG, 2),
))


def _om_body_fields(response):
    """The body ``ORDER_OM_REQUEST_TR`` (Table 58) and ``_RESPONSE_TR``
    (Table 59) share from offset 24 on -- identical but for a nanosecond
    ``Timestamp`` the response carries at 132 and the request does not, and
    the trailing reserved run: 24 bytes in the request, 52 in the response.
    Together those are the 36 bytes between 180 and 216.
    """
    fields = _sec_info(24) + (
        L.Field(D.ORDER_NUMBER, "OrderNumber", T.DOUBLE, 36),
        L.Field(D.ACCOUNT_NUMBER, "AccountNumber", T.Char(10), 44),
        L.Field(D.BOOK_TYPE, "BookType", T.SHORT, 54),
        L.Field(D.BUY_SELL, "BuySell", T.SHORT, 56),
        L.Field(D.DISCLOSED_VOL, "DisclosedVol", T.LONG, 58),
        L.Field(D.DISCLOSED_VOL_REMAINING, "DisclosedVolRemaining",
                T.LONG, 62),
        L.Field(D.TOTAL_VOL_REMAINING, "TotalVolRemaining", T.LONG, 66),
        L.Field(D.VOLUME, "Volume", T.LONG, 70),
        L.Field(D.VOLUME_FILLED_TODAY, "VolumeFilledToday", T.LONG, 74),
        L.Field(D.PRICE, "Price", T.LONG, 78, L.PAISE),
        L.Field(D.ENTRY_DATE_TIME, "EntryDateTime", T.LONG, 82),
        L.Field(D.LAST_MODIFIED, "LastModified", T.LONG, 86),
        _order_flags(90),
        L.Field(D.BRANCH_ID, "BranchId", T.SHORT, 92),
        L.Field(D.TRADER_ID, "TraderId", T.LONG, 94),
        L.Field(D.BROKER_ID, "BrokerId", T.Char(5), 98),
        L.Field(D.SUSPENDED, "Suspended", T.Char(1), 103),
        L.Field(D.SETTLOR, "Settlor", T.Char(12), 104),
        L.Field(D.PRO_CLIENT, "ProClient", T.SHORT, 116),
        L.Field(D.SETTLEMENT_TYPE, "SettlementType", T.SHORT, 118),
        L.Field(D.NNF_FIELD, "NNFField", T.DOUBLE, 120),
        L.Field(D.TRANSACTION_ID, "TransactionId", T.LONG, 128),
    )
    tail = 132
    if response:
        fields += (L.Field(D.TIMESTAMP, "Timestamp", T.LONG_LONG, 132),)
        tail = 140
    fields += (
        L.Field(D.PAN, "PAN", T.Char(10), tail),
        L.Field(D.ALGO_ID, "AlgoID", T.LONG, tail + 10),
        L.Reserved(tail + 14, 2, "ReservedFiller"),
        L.Field(D.LAST_ACTIVITY_REFERENCE, "LastActivityReference",
                T.LONG_LONG, tail + 16),
        L.Reserved(tail + 24, 52 if response else 24),
    )
    return fields


#: ORDER_OM_REQUEST_TR's and ORDER_OM_RESPONSE_TR's shared twenty-four byte
#: prefix -- Tables 58 and 59 open with the identical eight fields, unlike
#: the order entry request above, which shares a header with nothing.
HEADER_OM_TR = L.HeaderLayout(24, (
    L.Field(L.TRANSACTION_CODE, "TransactionCode", T.SHORT, 0),
    L.Field(D.LOG_TIME, "LogTime", T.LONG, 2),
    L.Field(D.USER_ID, "UserId", T.LONG, 6),
    L.Field(D.ERROR_CODE, "ErrorCode", T.SHORT, 10),
    L.Field(D.TIMESTAMP1, "TimeStamp1", T.LONG_LONG, 12),
    L.Field(D.TIMESTAMP2, "TimeStamp2", T.CHAR, 20),
    L.Field(D.MOD_CXL_BY, "ModCxlBy", T.Char(1), 21),
    L.Field(D.REASON_CODE, "ReasonCode", T.SHORT, 22),
))


def _trade_confirm_tr_fields():
    """``MS_TRADE_CONFIRM_TR``, Table 60 -- 192 bytes.

    ``GoodTillDate`` and ``OpOrderNumber``/``OpBrokerId``, which the plain
    ``MS_TRADE_CONFIRM`` carries, do not reappear here -- the running total
    of every other field is what proves it, since the fields below reach
    exactly 192 with nothing left over and nothing missing.
    """
    return (
        L.Field(D.RESPONSE_ORDER_NUMBER, "ResponseOrderNumber", T.DOUBLE, 26),
        L.Field(D.TIMESTAMP2, "TimeStamp2", T.CHAR, 34),
        L.Field(D.BROKER_ID, "BrokerId", T.Char(5), 35),
        L.Field(D.TRADER_NUM, "TraderNum", T.LONG, 40),
        L.Field(D.BUY_SELL, "BuySell", T.SHORT, 44),
        L.Field(D.ACCOUNT_NUMBER, "AccountNum", T.Char(10), 46),
        L.Field(D.ORIGINAL_VOL, "OriginalVol", T.LONG, 56),
        L.Field(D.DISCLOSED_VOL, "DisclosedVol", T.LONG, 60),
        L.Field(D.REMAINING_VOL, "RemainingVol", T.LONG, 64),
        L.Field(D.DISCLOSED_VOL_REMAINING, "DisclosedVolRemaining",
                T.LONG, 68),
        L.Field(D.PRICE, "Price", T.LONG, 72, L.PAISE),
        _order_flags(76),
        L.Field(D.FILL_NUMBER, "FillNumber", T.LONG, 78),
        L.Field(D.FILL_QTY, "FillQty", T.LONG, 82),
        L.Field(D.FILL_PRICE, "FillPrice", T.LONG, 86, L.PAISE),
        L.Field(D.VOLUME_FILLED_TODAY, "VolFilledToday", T.LONG, 90),
        L.Field(D.ACTIVITY_TYPE, "ActivityType", T.Char(2), 94),
        L.Field(D.ACTIVITY_TIME, "ActivityTime", T.LONG, 96),
    ) + _sec_info(100) + (
        L.Field(D.BOOK_TYPE, "BookType", T.SHORT, 112),
        L.Field(D.PRO_CLIENT, "ProClient", T.SHORT, 114),
        L.Field(D.PAN, "PAN", T.Char(10), 116),
        L.Field(D.ALGO_ID, "AlgoID", T.LONG, 126),
        L.Reserved(130, 2, "ReservedFiller"),
        L.Field(D.LAST_ACTIVITY_REFERENCE, "LastActivityReference",
                T.LONG_LONG, 132),
        L.Reserved(140, 52),
    )


#: MS_TRADE_CONFIRM_TR's own twenty-six byte prefix, up to and including
#: TimeStamp1 -- ResponseOrderNumber follows immediately, so TimeStamp2
#: (offset 34) is a body field here rather than a header one, unlike every
#: other structure in this file.
HEADER_TRADE_TR = L.HeaderLayout(26, (
    L.Field(L.TRANSACTION_CODE, "TransactionCode", T.SHORT, 0),
    L.Field(D.LOG_TIME, "LogTime", T.LONG, 2),
    L.Field(D.USER_ID, "UserId", T.LONG, 6),
    L.Field(D.TIMESTAMP, "Timestamp", T.LONG_LONG, 10),
    L.Field(D.TIMESTAMP1, "TimeStamp1", T.LONG_LONG, 18),
))


def _define_trimmed(layouts):
    """Register the trimmed order flow and trade confirmation."""
    layouts.define(X.BOARD_LOT_IN_TR, "ORDER_ENTRY_REQUEST_TR", 136,
                   _oe_request_tr_fields(), header=HEADER_OE_TR)
    for code in (X.ORDER_MOD_IN_TR, X.ORDER_CANCEL_IN_TR):
        layouts.define(code, "ORDER_OM_REQUEST_TR", 180,
                       _om_body_fields(response=False), header=HEADER_OM_TR)
    for code in (X.ORDER_CONFIRMATION_TR, X.ORDER_MOD_CONFIRMATION_TR,
                 X.ORDER_CXL_CONFIRMATION_TR, X.ORDER_ERROR_TR,
                 X.ORDER_MOD_REJECT_TR, X.ORDER_CANCEL_REJECT_TR,
                 X.PRICE_CONFIRMATION_TR):
        layouts.define(code, "ORDER_OM_RESPONSE_TR", 216,
                       _om_body_fields(response=True), header=HEADER_OM_TR)
    layouts.define(X.TRADE_CONFIRMATION_TR, "MS_TRADE_CONFIRM_TR", 192,
                   _trade_confirm_tr_fields(), header=HEADER_TRADE_TR)


def build_cm():
    """Every Capital Market structure, by transaction code."""
    layouts = L.NnfDictionary(HEADER)

    # -- the box connection, Chapter 10 ----------------------------------
    layouts.define(X.GR_REQUEST, "MS_GR_REQUEST", 48, (
        L.Field(D.BOX_ID, "BoxId", T.SHORT, 40),
        L.Field(D.BROKER_ID, "BrokerID", T.Char(5), 42),
        L.Reserved(47, 1, "Filler"),
    ))
    # The 136-byte form, which is the new encryption's. The existing one is 124
    # bytes and stops after a single sixteen-byte IV; a member is entitled to
    # one methodology or the other by which Gateway Router port it dials, and
    # this simulator serves the new one.
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
    layouts.define(X.BOX_SIGN_ON_REQUEST_OUT, "MS_BOX_SIGN_ON_REQUEST_OUT", 52, (
        L.Field(D.BOX_ID, "BoxId", T.SHORT, 40),
        L.Reserved(42, 10),
    ))

    # -- the user session, Chapter 3 -------------------------------------
    layouts.define(X.SIGN_ON_REQUEST_IN, "SIGNON_IN", 276, _signon_fields())
    layouts.define(X.SIGN_ON_REQUEST_OUT, "SIGNON_OUT", 276, _signon_fields())
    layouts.define(X.SIGN_OFF_REQUEST_IN, "MS_SIGNOFF_IN", 40, ())
    layouts.define(X.SIGN_OFF_REQUEST_OUT, "MS_SIGNOFF_OUT", 40, ())
    layouts.define(X.HEARTBEAT, "HEARTBEAT", 40, ())
    layouts.define(X.ERROR_RESPONSE_OUT, "ERROR_RESPONSE", 180,
                   _sec_info(40) + (
                       L.Field(D.ERROR_MESSAGE, "ErrorMessage", T.Char(128), 52),
                   ))
    layouts.define(X.SYSTEM_INFORMATION_IN, "SYSTEM_INFO_REQ", 40, ())
    layouts.define(X.SYSTEM_INFORMATION_OUT, "SYSTEM_INFORMATION_DATA", 94,
                   _system_information_fields())

    # -- order entry, Chapter 4 ------------------------------------------
    for code in (X.BOARD_LOT_IN, X.ORDER_MOD_IN, X.ORDER_CANCEL_IN,
                 X.ORDER_CONFIRMATION, X.ORDER_MOD_CONFIRMATION,
                 X.ORDER_CANCEL_CONFIRMATION, X.ORDER_ERROR,
                 X.ORDER_MOD_REJECT, X.ORDER_CANCEL_REJECT,
                 X.PRICE_CONFIRMATION):
        layouts.define(code, X.NAMES[code], 290, _order_entry_fields())

    # -- trades, Chapter 5 -----------------------------------------------
    layouts.define(X.TRADE_CONFIRMATION, "MS_TRADE_CONFIRM", 228,
                   _trade_fields())

    # -- recovery, Chapter 5 ---------------------------------------------
    #
    # The request is defined so it can be read and refused by name. The three
    # response records are not, because they are not produced: a MESSAGE_RECORD
    # is 80 to 512 bytes -- the actual message wrapped inside an outer header --
    # and every structure here is fixed width. See handlers._on_download_request.
    layouts.define(X.DOWNLOAD_REQUEST, "MESSAGE_DOWNLOAD", 48, (
        L.Field(D.DOWNLOAD_SEQUENCE, "SequenceNumber", T.DOUBLE, 40),))
    # The download's three answers. Header and trailer are a bare header and
    # nothing else; the record carries one recovered message whole, which is
    # why it is the one structure here with no published length -- see
    # nnf/layout.py:RecordLayout for the inner header it wraps.
    layouts.define(X.HEADER_RECORD, "MESSAGE_HEADER", 40, ())
    layouts.define(X.TRAILER_RECORD, "MESSAGE_HEADER", 40, ())
    layouts.define_record(X.MESSAGE_RECORD, "MESSAGE_RECORD", D.DOWNLOAD_DATA)

    # -- the trimmed order flow, Chapter 10 appendix ----------------------
    _define_trimmed(layouts)

    return layouts
