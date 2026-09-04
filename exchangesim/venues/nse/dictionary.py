"""The Capital Market dialect: what each field is called and what it may hold.

This is the counterpart of :mod:`exchangesim.venues.nse.layouts`. That module
says where a field sits in the bytes; this one says what it is named, what
values it accepts and what those values mean. Keeping them apart is what lets
the audit label a value without the codec knowing anything about it, exactly as
the two HKEX dictionaries do for their two encodings.

**A fixed-width protocol changes what a dictionary is for.** Every field of a
structure is on the wire on every message, always, so ``MessageDef.required``
can never fail and "tag not defined for this message" can never fire. The job
here is value and enumeration checking, and naming -- nothing else. Messages are
therefore validated with ``check_unknown=False`` and declare their fields as
optional. Reading it as presence checking would give false confidence.

**Tags are numbers because a Message is keyed by numbers, not because this is
FIX.** Where a concept genuinely coincides with a FIX field -- a symbol, a side,
a quantity, a price, an exchange-assigned order identifier -- that field's tag is
reused, so ``fix/render.py`` lifts a readable summary line and the board's
symbol column works. Where it does not, the tag comes from a private range:

======  ==========================================================
9000s   the message header
9100s   the Capital Market structures
9200s   one tag per bit of a bitfield
9300s   the order and trade download
9400+   reserved, so Futures & Options can be added without collision
======  ==========================================================

Three FIX tags are deliberately **not** defined here. ``ClOrdID(11)`` and
``OrigClOrdID(41)``, because NNF has no client-supplied order handle at all --
modification and cancellation address the exchange's own ``OrderNumber``, and
inventing a ClOrdID would put an identifier no client ever sent into the audit,
the order record and the board. ``OrdType(40)`` and ``TimeInForce(59)``, because
NSE spells both as bits of ``ST_ORDER_FLAGS`` rather than as scalar fields with
FIX's value domain; each bit is named in its own right instead.
"""

from ...fix.constants import FieldType
from ...fix.dictionary import Dictionary, FieldDef, MessageDef, enum_labels
from ...nnf.layout import TRANSACTION_CODE
from . import transactions as X

#: Names this dialect, in the slot a FIX dictionary uses for its BeginString.
#: Nothing on the wire carries it; it appears in logs and audit entries.
BEGIN_STRING = "NNF.CM"

#: Prices are paise, so two decimal places, and the timestamps this dialect
#: renders are seconds -- there is no fractional-second field anywhere in it.
PRICE_DECIMALS = 2


# -- tags --------------------------------------------------------------------

# The message header (Chapter 10, forty bytes).
MSG_SEQ_NUM = 34                # the packet's sequence number, not the message's
LOG_TIME = 9001
ALPHA_CHAR = 9002
USER_ID = 9003
ERROR_CODE = 9004
TIMESTAMP = 9005
TIMESTAMP1 = 9006
TIMESTAMP2 = 9007
MESSAGE_LENGTH = 9008

# SEC_INFO, and the identity of a security.
SYMBOL = 55
SERIES = 9100

# ORDER_ENTRY_REQUEST.
PARTICIPANT_TYPE = 9101
COMPETITOR_PERIOD = 9102
SOLICITOR_PERIOD = 9103
MOD_CXL_BY = 9104
REASON_CODE = 9105
AUCTION_NUMBER = 9106
OP_BROKER_ID = 9107
SUSPENDED = 9108
ORDER_NUMBER = 37               # OrderID: exchange-assigned, which is tag 37's job
ACCOUNT_NUMBER = 1              # Account: the client code the order is for
BOOK_TYPE = 9110
BUY_SELL = 54
DISCLOSED_VOL = 9111
DISCLOSED_VOL_REMAINING = 9112
TOTAL_VOL_REMAINING = 151       # LeavesQty
VOLUME = 38                     # OrderQty
VOLUME_FILLED_TODAY = 14        # CumQty
PRICE = 44
TRIGGER_PRICE = 9113
GOOD_TILL_DATE = 9114
ENTRY_DATE_TIME = 9115
MIN_FILL_AON = 9116
LAST_MODIFIED = 9117
BRANCH_ID = 9118
TRADER_ID = 9119
BROKER_ID = 9120
OE_REMARKS = 9121
SETTLOR = 9122
PRO_CLIENT = 9123
SETTLEMENT_TYPE = 9124
NNF_FIELD = 9125
EXEC_TIMESTAMP = 9126
PAN = 9127
ALGO_ID = 9128
LAST_ACTIVITY_REFERENCE = 9129

# MS_TRADE_CONFIRM, beyond what it shares with the order structure.
RESPONSE_ORDER_NUMBER = 9130
TRADER_NUM = 9131
ORIGINAL_VOL = 9132
REMAINING_VOL = 9133
GTD = 9134
FILL_NUMBER = 9135
FILL_QTY = 32                   # LastQty
FILL_PRICE = 31                 # LastPx
ACTIVITY_TYPE = 9136
ACTIVITY_TIME = 9137
OP_ORDER_NUMBER = 9138
NEW_VOLUME = 9139

# SIGNON_IN / SIGNON_OUT.
SIGNON_USER_ID = 9140
PASSWORD = 9141
NEW_PASSWORD = 9142
TRADER_NAME = 9143
LAST_PASSWORD_CHANGE = 9144
VERSION_NUMBER = 9146
USER_TYPE = 9147
LAST_MARKET_CLOSE = 9148
WORKSTATION_NUMBER = 9149
BROKER_STATUS = 9150
SHOW_INDEX = 9151
BROKER_NAME = 9152

# SYSTEM_INFORMATION_DATA.
NORMAL_STATUS = 9160
ODDLOT_STATUS = 9161
SPOT_STATUS = 9162
AUCTION_STATUS = 9163
CALL_AUCTION1_STATUS = 9164
CALL_AUCTION2_STATUS = 9165
MARKET_INDEX = 9166
DEFAULT_SETTLEMENT_NORMAL = 9167
DEFAULT_SETTLEMENT_SPOT = 9168
DEFAULT_SETTLEMENT_AUCTION = 9169
WARNING_PERCENT = 9153
VOLUME_FREEZE_PERCENT = 9154
TERMINAL_IDLE_TIME = 9155
BOARD_LOT_QUANTITY = 9156
TICK_SIZE = 9157
MAXIMUM_GTC_DAYS = 9158
DISCLOSED_QUANTITY_PERCENT = 9159

# The Gateway Router and the secure box.
BOX_ID = 9170
IP_ADDRESS = 9171
PORT = 9172
SESSION_KEY = 9173
CRYPTOGRAPHIC_KEY = 9174
STATIC_IV = 9175
DYNAMIC_IV = 9176
ADDITIONAL_KEY = 9177

# ERROR_RESPONSE.
ERROR_MESSAGE = 58              # Text

# ST_ORDER_FLAGS, one tag per bit, in the order the big-endian table prints.
FLAG_ATO = 9201
FLAG_MARKET = 9202
FLAG_ON_STOP = 9203
FLAG_DAY = 9204
FLAG_GTC = 9205
FLAG_IOC = 9206
FLAG_AON = 9207
FLAG_MF = 9208
FLAG_MATCHED_IND = 9209
FLAG_TRADED = 9210
FLAG_MODIFIED = 9211
FLAG_FROZEN = 9212
FLAG_PREOPEN = 9213
FLAG_STPC = 9214

# BrokerEligibilityPerMarket, likewise.
ELIGIBLE_NORMAL = 9220
ELIGIBLE_ODDLOT = 9221
ELIGIBLE_SPOT = 9222
ELIGIBLE_AUCTION = 9223
ELIGIBLE_CALL_AUCTION1 = 9224
ELIGIBLE_CALL_AUCTION2 = 9225
ELIGIBLE_PREOPEN = 9226

# SECURITY ELIGIBLE INDICATORS.
SECURITY_AON = 9230
SECURITY_MIN_FILL = 9231
SECURITY_BOOKS_MERGED = 9232

# The order and trade download.
DOWNLOAD_SEQUENCE = 9300
DOWNLOAD_COUNT = 9301


# -- value domains -----------------------------------------------------------

#: What a numeric field holds when nobody set it. Chapter 2 is explicit -- "all
#: numeric data must be set to zero (0) before sending to the host, unless a
#: value is assigned to it" -- and in a fixed-width protocol there is no other
#: way to say "absent": the field travels either way. So zero is a legal value
#: of every enumeration below whose own domain does not already use it, and it
#: is labelled rather than left as a bare 0 in the audit.
#:
#: It is *not* added to MarketStatus or UserType, where zero means PreOpen and
#: Corporate Manager -- real values that would be hidden by pretending
#: otherwise. Refusing an order that names no side is the gateway's business,
#: and it does: the side maps to None and the engine rejects it.
NOT_SET = "0"


class BuySell(object):
    """``BuySell``: the same 1/2 FIX spells on tag 54, so no mapping is needed."""

    BUY = "1"
    SELL = "2"


class BookType(object):
    """The seven books, of which this venue trades exactly one.

    Everything but ``REGULAR_LOT`` is rejected rather than promoted -- an odd
    lot silently treated as a board lot is the failure this project refuses to
    ship, and the same reasoning covers Special Terms, Stop Loss and the rest.
    """

    REGULAR_LOT = "1"
    SPECIAL_TERMS = "2"
    STOP_LOSS = "3"
    ODD_LOT = "5"
    SPOT = "6"
    AUCTION = "7"
    CALL_AUCTION1 = "11"
    CALL_AUCTION2 = "12"


class MarketStatus(object):
    """What ``SYSTEM_INFORMATION_OUT`` reports per market."""

    PRE_OPEN = "0"
    OPEN = "1"
    CLOSED = "2"
    PRE_OPEN_ENDED = "3"


class ProClient(object):
    """Whose account an order is for: the member's own, or a client's."""

    CLIENT = "1"
    PRO = "2"


class UserType(object):
    CORPORATE_MANAGER = "0"
    BRANCH_MANAGER = "1"
    DEALER = "2"


class ModCxlBy(object):
    """Who modified or cancelled an order."""

    TRADER = "T"
    BRANCH_MANAGER = "B"
    CORPORATE_MANAGER = "M"
    EXCHANGE = "E"


class Flag(object):
    """A bit of ``ST_ORDER_FLAGS``, exploded by the codec into Y or N."""

    YES = "Y"
    NO = "N"


# -- fields ------------------------------------------------------------------

def _with_not_set(labels):
    """Name the zero an unset numeric field carries, rather than showing a 0."""
    named = dict(labels)
    named.setdefault(NOT_SET, "NOT_SET")
    return named


def _number(tag, name, digits=None):
    return FieldDef(tag, name, FieldType.INT, max_digits=digits)


def _text(tag, name, length=None, redact=False):
    return FieldDef(tag, name, FieldType.STRING, max_length=length,
                    redact=redact)


def _flag(tag, name):
    return FieldDef(tag, name, FieldType.BOOLEAN, labels={"Y": "YES", "N": "NO"})


def header_fields():
    """The forty-byte message header, plus the packet's sequence number."""
    return [
        FieldDef(TRANSACTION_CODE, "TransactionCode", FieldType.STRING),
        _number(MSG_SEQ_NUM, "SequenceNumber"),
        _number(LOG_TIME, "LogTime"),
        _text(ALPHA_CHAR, "AlphaChar", 2),
        _number(USER_ID, "UserId"),
        FieldDef(ERROR_CODE, "ErrorCode", FieldType.INT,
                 labels={str(code): name
                         for code, name in X.ERROR_CODES.items()}),
        _number(TIMESTAMP, "Timestamp"),
        _text(TIMESTAMP1, "TimeStamp1", 8),
        _text(TIMESTAMP2, "TimeStamp2", 8),
        _number(MESSAGE_LENGTH, "MessageLength"),
    ]


def application_fields():
    return [
        # -- the security ---------------------------------------------------
        _text(SYMBOL, "Symbol", 10),
        _text(SERIES, "Series", 2),

        # -- the order ------------------------------------------------------
        _text(PARTICIPANT_TYPE, "ParticipantType", 1),
        _number(COMPETITOR_PERIOD, "CompetitorPeriod"),
        _number(SOLICITOR_PERIOD, "SolicitorPeriod"),
        FieldDef(MOD_CXL_BY, "ModCxlBy", FieldType.STRING,
                 labels=enum_labels(ModCxlBy)),
        FieldDef(REASON_CODE, "ReasonCode", FieldType.INT,
                 labels={str(code): name
                         for code, name in X.ERROR_CODES.items()}),
        _number(AUCTION_NUMBER, "AuctionNumber"),
        _text(OP_BROKER_ID, "OpBrokerId", 5),
        _text(SUSPENDED, "Suspended", 1),
        _text(ORDER_NUMBER, "OrderNumber", 20),
        _text(ACCOUNT_NUMBER, "AccountNumber", 10),
        FieldDef(BOOK_TYPE, "BookType", FieldType.STRING,
                 values=(NOT_SET, BookType.REGULAR_LOT, BookType.SPECIAL_TERMS,
                         BookType.STOP_LOSS, BookType.ODD_LOT, BookType.SPOT,
                         BookType.AUCTION, BookType.CALL_AUCTION1,
                         BookType.CALL_AUCTION2),
                 labels=_with_not_set(enum_labels(BookType))),
        FieldDef(BUY_SELL, "BuySell", FieldType.STRING,
                 values=(NOT_SET, BuySell.BUY, BuySell.SELL),
                 labels=_with_not_set(enum_labels(BuySell))),
        FieldDef(DISCLOSED_VOL, "DisclosedVol", FieldType.QTY),
        FieldDef(DISCLOSED_VOL_REMAINING, "DisclosedVolRemaining", FieldType.QTY),
        FieldDef(TOTAL_VOL_REMAINING, "TotalVolRemaining", FieldType.QTY),
        FieldDef(VOLUME, "Volume", FieldType.QTY),
        FieldDef(VOLUME_FILLED_TODAY, "VolumeFilledToday", FieldType.QTY),
        FieldDef(PRICE, "Price", FieldType.PRICE, max_decimals=PRICE_DECIMALS),
        FieldDef(TRIGGER_PRICE, "TriggerPrice", FieldType.PRICE,
                 max_decimals=PRICE_DECIMALS),
        _number(GOOD_TILL_DATE, "GoodTillDate"),
        _number(ENTRY_DATE_TIME, "EntryDateTime"),
        FieldDef(MIN_FILL_AON, "MinFillAon", FieldType.QTY),
        _number(LAST_MODIFIED, "LastModified"),
        _number(BRANCH_ID, "BranchId"),
        _number(TRADER_ID, "TraderId"),
        _text(BROKER_ID, "BrokerId", 5),
        _text(OE_REMARKS, "OERemarks", 25),
        _text(SETTLOR, "Settlor", 12),
        FieldDef(PRO_CLIENT, "ProClient", FieldType.STRING,
                 values=(NOT_SET, ProClient.CLIENT, ProClient.PRO),
                 labels=_with_not_set(enum_labels(ProClient))),
        _number(SETTLEMENT_TYPE, "SettlementType"),
        _text(NNF_FIELD, "NNFField", 20),
        _text(EXEC_TIMESTAMP, "ExecTimeStamp", 20),
        _text(PAN, "PAN", 10),
        _number(ALGO_ID, "AlgoID"),
        _number(LAST_ACTIVITY_REFERENCE, "LastActivityReference"),

        # -- the trade ------------------------------------------------------
        _text(RESPONSE_ORDER_NUMBER, "ResponseOrderNumber", 20),
        _number(TRADER_NUM, "TraderNum"),
        FieldDef(ORIGINAL_VOL, "OriginalVol", FieldType.QTY),
        FieldDef(REMAINING_VOL, "RemainingVol", FieldType.QTY),
        _number(GTD, "Gtd"),
        _number(FILL_NUMBER, "FillNumber"),
        FieldDef(FILL_QTY, "FillQty", FieldType.QTY),
        FieldDef(FILL_PRICE, "FillPrice", FieldType.PRICE,
                 max_decimals=PRICE_DECIMALS),
        _text(ACTIVITY_TYPE, "ActivityType", 2),
        _number(ACTIVITY_TIME, "ActivityTime"),
        _text(OP_ORDER_NUMBER, "OpOrderNumber", 20),
        FieldDef(NEW_VOLUME, "NewVolume", FieldType.QTY),

        # -- sign-on --------------------------------------------------------
        _number(SIGNON_USER_ID, "UserId"),
        _text(PASSWORD, "Password", 8, redact=True),
        _text(NEW_PASSWORD, "NewPassword", 8, redact=True),
        _text(TRADER_NAME, "TraderName", 26),
        _number(LAST_PASSWORD_CHANGE, "LastPasswordChangeDateTime"),
        _number(VERSION_NUMBER, "VersionNumber"),
        FieldDef(USER_TYPE, "UserType", FieldType.STRING,
                 labels=enum_labels(UserType)),
        _text(LAST_MARKET_CLOSE, "SequenceNumber", 20),
        _text(WORKSTATION_NUMBER, "WorkstationNumber", 14),
        _text(BROKER_STATUS, "BrokerStatus", 1),
        _text(SHOW_INDEX, "ShowIndex", 1),
        _text(BROKER_NAME, "BrokerName", 26),
        _flag(ELIGIBLE_NORMAL, "EligibleNormalMarket"),
        _flag(ELIGIBLE_ODDLOT, "EligibleOddlotMarket"),
        _flag(ELIGIBLE_SPOT, "EligibleSpotMarket"),
        _flag(ELIGIBLE_AUCTION, "EligibleAuctionMarket"),
        _flag(ELIGIBLE_CALL_AUCTION1, "EligibleCallAuction1"),
        _flag(ELIGIBLE_CALL_AUCTION2, "EligibleCallAuction2"),
        _flag(ELIGIBLE_PREOPEN, "EligiblePreopen"),

        # -- system information ---------------------------------------------
        FieldDef(NORMAL_STATUS, "NormalMarketStatus", FieldType.STRING,
                 labels=enum_labels(MarketStatus)),
        FieldDef(ODDLOT_STATUS, "OddlotMarketStatus", FieldType.STRING,
                 labels=enum_labels(MarketStatus)),
        FieldDef(SPOT_STATUS, "SpotMarketStatus", FieldType.STRING,
                 labels=enum_labels(MarketStatus)),
        FieldDef(AUCTION_STATUS, "AuctionMarketStatus", FieldType.STRING,
                 labels=enum_labels(MarketStatus)),
        FieldDef(CALL_AUCTION1_STATUS, "CallAuction1Status", FieldType.STRING,
                 labels=enum_labels(MarketStatus)),
        FieldDef(CALL_AUCTION2_STATUS, "CallAuction2Status", FieldType.STRING,
                 labels=enum_labels(MarketStatus)),
        _number(MARKET_INDEX, "MarketIndex"),
        _number(DEFAULT_SETTLEMENT_NORMAL, "DefaultSettlementPeriodNormal"),
        _number(DEFAULT_SETTLEMENT_SPOT, "DefaultSettlementPeriodSpot"),
        _number(DEFAULT_SETTLEMENT_AUCTION, "DefaultSettlementPeriodAuction"),
        _number(WARNING_PERCENT, "WarningPercent"),
        _number(VOLUME_FREEZE_PERCENT, "VolumeFreezePercent"),
        _number(TERMINAL_IDLE_TIME, "TerminalIdleTime"),
        FieldDef(BOARD_LOT_QUANTITY, "BoardLotQuantity", FieldType.QTY),
        FieldDef(TICK_SIZE, "TickSize", FieldType.PRICE,
                 max_decimals=PRICE_DECIMALS),
        _number(MAXIMUM_GTC_DAYS, "MaximumGtcDays"),
        _number(DISCLOSED_QUANTITY_PERCENT, "DisclosedQuantityPercentAllowed"),
        _flag(SECURITY_AON, "SecurityAllowsAON"),
        _flag(SECURITY_MIN_FILL, "SecurityAllowsMinimumFill"),
        _flag(SECURITY_BOOKS_MERGED, "SecurityBooksMerged"),

        # -- the box and the Gateway Router ---------------------------------
        _number(BOX_ID, "BoxId"),
        _text(IP_ADDRESS, "IpAddress", 16),
        _number(PORT, "Port"),
        _text(SESSION_KEY, "SessionKey", 8, redact=True),
        _text(CRYPTOGRAPHIC_KEY, "CryptographicKey", 32, redact=True),
        _text(STATIC_IV, "StaticCryptographicIV", 8, redact=True),
        _text(DYNAMIC_IV, "DynamicCryptographicIV", 8, redact=True),
        _text(ADDITIONAL_KEY, "CryptographicAdditionalKey", 12, redact=True),

        # -- errors and downloads -------------------------------------------
        _text(ERROR_MESSAGE, "ErrorMessage", 128),
        _number(DOWNLOAD_SEQUENCE, "DownloadSequence"),
        _number(DOWNLOAD_COUNT, "DownloadCount"),

        # -- ST_ORDER_FLAGS --------------------------------------------------
        _flag(FLAG_ATO, "ATO"),
        _flag(FLAG_MARKET, "Market"),
        _flag(FLAG_ON_STOP, "OnStop"),
        _flag(FLAG_DAY, "Day"),
        _flag(FLAG_GTC, "GTC"),
        _flag(FLAG_IOC, "IOC"),
        _flag(FLAG_AON, "AON"),
        _flag(FLAG_MF, "MF"),
        _flag(FLAG_MATCHED_IND, "MatchedInd"),
        _flag(FLAG_TRADED, "Traded"),
        _flag(FLAG_MODIFIED, "Modified"),
        _flag(FLAG_FROZEN, "Frozen"),
        _flag(FLAG_PREOPEN, "Preopen"),
        _flag(FLAG_STPC, "STPC"),
    ]


# -- messages ----------------------------------------------------------------
#
# ``optional`` throughout, and ``required`` nowhere: in a fixed-width protocol
# every field of a structure travels on every message, so a required-field check
# could never fail. What a definition does say is which transaction codes a
# client may *send* -- ``inbound=False`` on the rest -- and that is a real check.

def _message(code, name, tags, inbound=True):
    return MessageDef(str(code), name, optional=tuple(tags), inbound=inbound)


_ORDER_TAGS = (
    SYMBOL, SERIES, PARTICIPANT_TYPE, COMPETITOR_PERIOD, SOLICITOR_PERIOD,
    MOD_CXL_BY, REASON_CODE, AUCTION_NUMBER, OP_BROKER_ID, SUSPENDED,
    ORDER_NUMBER, ACCOUNT_NUMBER, BOOK_TYPE, BUY_SELL, DISCLOSED_VOL,
    DISCLOSED_VOL_REMAINING, TOTAL_VOL_REMAINING, VOLUME, VOLUME_FILLED_TODAY,
    PRICE, TRIGGER_PRICE, GOOD_TILL_DATE, ENTRY_DATE_TIME, MIN_FILL_AON,
    LAST_MODIFIED, BRANCH_ID, TRADER_ID, BROKER_ID, OE_REMARKS, SETTLOR,
    PRO_CLIENT, SETTLEMENT_TYPE, NNF_FIELD, EXEC_TIMESTAMP, PAN, ALGO_ID,
    LAST_ACTIVITY_REFERENCE,
    FLAG_ATO, FLAG_MARKET, FLAG_ON_STOP, FLAG_DAY, FLAG_GTC, FLAG_IOC,
    FLAG_AON, FLAG_MF, FLAG_MATCHED_IND, FLAG_TRADED, FLAG_MODIFIED,
    FLAG_FROZEN, FLAG_PREOPEN, FLAG_STPC,
)

_TRADE_TAGS = (
    SYMBOL, SERIES, RESPONSE_ORDER_NUMBER, BROKER_ID, TRADER_NUM,
    ACCOUNT_NUMBER, BUY_SELL, ORIGINAL_VOL, DISCLOSED_VOL, REMAINING_VOL,
    DISCLOSED_VOL_REMAINING, PRICE, GTD, FILL_NUMBER, FILL_QTY, FILL_PRICE,
    VOLUME_FILLED_TODAY, ACTIVITY_TYPE, ACTIVITY_TIME, OP_ORDER_NUMBER,
    OP_BROKER_ID, BOOK_TYPE, NEW_VOLUME, PRO_CLIENT, PAN, ALGO_ID,
    LAST_ACTIVITY_REFERENCE,
    FLAG_ATO, FLAG_MARKET, FLAG_ON_STOP, FLAG_DAY, FLAG_GTC, FLAG_IOC,
    FLAG_AON, FLAG_MF, FLAG_MATCHED_IND, FLAG_TRADED, FLAG_MODIFIED,
    FLAG_FROZEN, FLAG_PREOPEN, FLAG_STPC,
)

_SIGNON_TAGS = (
    SIGNON_USER_ID, PASSWORD, NEW_PASSWORD, TRADER_NAME, LAST_PASSWORD_CHANGE,
    BROKER_ID, BRANCH_ID, VERSION_NUMBER, USER_TYPE, LAST_MARKET_CLOSE,
    WORKSTATION_NUMBER, BROKER_STATUS, SHOW_INDEX, BROKER_NAME,
    ELIGIBLE_NORMAL, ELIGIBLE_ODDLOT, ELIGIBLE_SPOT, ELIGIBLE_AUCTION,
    ELIGIBLE_CALL_AUCTION1, ELIGIBLE_CALL_AUCTION2, ELIGIBLE_PREOPEN,
)

_SYSTEM_TAGS = (
    NORMAL_STATUS, ODDLOT_STATUS, SPOT_STATUS, AUCTION_STATUS,
    CALL_AUCTION1_STATUS, CALL_AUCTION2_STATUS, MARKET_INDEX,
    DEFAULT_SETTLEMENT_NORMAL, DEFAULT_SETTLEMENT_SPOT,
    DEFAULT_SETTLEMENT_AUCTION, COMPETITOR_PERIOD, SOLICITOR_PERIOD,
    WARNING_PERCENT, VOLUME_FREEZE_PERCENT, TERMINAL_IDLE_TIME,
    BOARD_LOT_QUANTITY, TICK_SIZE, MAXIMUM_GTC_DAYS,
    DISCLOSED_QUANTITY_PERCENT, SECURITY_AON, SECURITY_MIN_FILL,
    SECURITY_BOOKS_MERGED,
)


def application_messages():
    return [
        # -- the box connection ---------------------------------------------
        _message(X.SECURE_BOX_REGISTRATION_REQUEST_IN,
                 "SECURE_BOX_REGISTRATION_REQUEST_IN", (BOX_ID,)),
        _message(X.SECURE_BOX_REGISTRATION_REQUEST_OUT,
                 "SECURE_BOX_REGISTRATION_REQUEST_OUT", (), inbound=False),
        _message(X.BOX_SIGN_ON_REQUEST_IN, "BOX_SIGN_ON_REQUEST_IN",
                 (BOX_ID, BROKER_ID, SESSION_KEY)),
        _message(X.BOX_SIGN_ON_REQUEST_OUT, "BOX_SIGN_ON_REQUEST_OUT",
                 (BOX_ID,), inbound=False),
        _message(X.GR_REQUEST, "GR_REQUEST", (BOX_ID, BROKER_ID)),
        _message(X.GR_RESPONSE, "GR_RESPONSE",
                 (BOX_ID, BROKER_ID, IP_ADDRESS, PORT, SESSION_KEY,
                  CRYPTOGRAPHIC_KEY, STATIC_IV, DYNAMIC_IV, ADDITIONAL_KEY),
                 inbound=False),

        # -- the user session -----------------------------------------------
        _message(X.SIGN_ON_REQUEST_IN, "SIGN_ON_REQUEST_IN", _SIGNON_TAGS),
        _message(X.SIGN_ON_REQUEST_OUT, "SIGN_ON_REQUEST_OUT", _SIGNON_TAGS,
                 inbound=False),
        # Both halves of the log-off are the bare message header: "The
        # structure sent is: MESSAGE HEADER", and the response likewise.
        _message(X.SIGN_OFF_REQUEST_IN, "SIGN_OFF_REQUEST_IN", ()),
        _message(X.SIGN_OFF_REQUEST_OUT, "SIGN_OFF_REQUEST_OUT", (),
                 inbound=False),
        _message(X.HEARTBEAT, "HEARTBEAT", ()),
        _message(X.ERROR_RESPONSE_OUT, "ERROR_RESPONSE_OUT",
                 (SYMBOL, SERIES, ERROR_MESSAGE), inbound=False),
        _message(X.SYSTEM_INFORMATION_IN, "SYSTEM_INFORMATION_IN", ()),
        _message(X.SYSTEM_INFORMATION_OUT, "SYSTEM_INFORMATION_OUT",
                 _SYSTEM_TAGS, inbound=False),

        # -- order entry ----------------------------------------------------
        _message(X.BOARD_LOT_IN, "BOARD_LOT_IN", _ORDER_TAGS),
        _message(X.ORDER_MOD_IN, "ORDER_MOD_IN", _ORDER_TAGS),
        _message(X.ORDER_CANCEL_IN, "ORDER_CANCEL_IN", _ORDER_TAGS),
        _message(X.ORDER_CONFIRMATION, "ORDER_CONFIRMATION", _ORDER_TAGS,
                 inbound=False),
        _message(X.ORDER_MOD_CONFIRMATION, "ORDER_MOD_CONFIRMATION",
                 _ORDER_TAGS, inbound=False),
        _message(X.ORDER_CANCEL_CONFIRMATION, "ORDER_CANCEL_CONFIRMATION",
                 _ORDER_TAGS, inbound=False),
        _message(X.ORDER_ERROR, "ORDER_ERROR", _ORDER_TAGS, inbound=False),
        _message(X.ORDER_MOD_REJECT, "ORDER_MOD_REJECT", _ORDER_TAGS,
                 inbound=False),
        _message(X.ORDER_CANCEL_REJECT, "ORDER_CANCEL_REJECT", _ORDER_TAGS,
                 inbound=False),
        _message(X.PRICE_CONFIRMATION, "PRICE_CONFIRMATION", _ORDER_TAGS,
                 inbound=False),

        # -- trades ---------------------------------------------------------
        _message(X.TRADE_CONFIRMATION, "TRADE_CONFIRMATION", _TRADE_TAGS,
                 inbound=False),

        # -- recovery -------------------------------------------------------
        _message(X.DOWNLOAD_REQUEST, "DOWNLOAD_REQUEST", (DOWNLOAD_SEQUENCE,)),
        _message(X.HEADER_RECORD, "HEADER_RECORD", (DOWNLOAD_COUNT,),
                 inbound=False),
        _message(X.MESSAGE_RECORD, "MESSAGE_RECORD", (), inbound=False),
        _message(X.TRAILER_RECORD, "TRAILER_RECORD", (), inbound=False),
    ]


def build_cm():
    """The Capital Market dialect."""
    return Dictionary(
        begin_string=BEGIN_STRING,
        fields=header_fields() + application_fields(),
        messages=application_messages(),
        header=tuple(field.tag for field in header_fields()))
