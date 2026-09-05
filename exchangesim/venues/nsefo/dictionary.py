"""The Futures & Options dialect: what each field is called and what it may hold.

This is the counterpart of :mod:`exchangesim.venues.nsefo.layouts`, the way
:mod:`exchangesim.venues.nse.dictionary` is the counterpart of that venue's own
``layouts.py``. That module says where a field sits in the bytes; this one
says what it is named, what values it accepts and what those values mean.

**A fixed-width protocol changes what a dictionary is for**, exactly as it
does for Capital Market: every field of a structure is on the wire on every
message, always, so ``MessageDef.required`` can never fail and "tag not
defined for this message" can never fire. The job here is value and
enumeration checking, and naming -- nothing else. Messages are therefore
validated with ``check_unknown=False`` and declare their fields as optional.

**Tags stay in this venue's own reserved range, deliberately not Capital
Market's.** Capital Market's ``dictionary.py`` documents ``9400+`` as
"reserved, so Futures & Options can be added without collision", and that is
exactly where every private tag below lives -- nothing here is below 9400,
and nothing in Capital Market's dictionary is at or above it. That makes the
two tag spaces disjoint by construction rather than by convention, which is
what ``tests/test_nsefo_dictionary.py`` checks: no tag number may mean one
thing in one venue's dictionary and something else in the other's.

The transcription (``docs/specs/NSE_FO_TRANSCRIPTION.md`` §5) suggested going
further and literally reusing several of Capital Market's own private tags for
structures the two venues share byte-for-byte -- the message header, the
Gateway Router/box family, and even each bit of ``ST_ORDER_FLAGS`` the two
bitfields have in common. That recommendation is **not** followed here, for
two reasons. First, it would import ``venues.nse.dictionary`` into this
package for no wire-level benefit -- the numbers only need to agree on
*meaning*, never on the literal Python object, and two independently-numbered
private ranges already guarantee that trivially. Second, and more seriously,
one part of that suggestion is an outright bug waiting to happen: it proposed
reusing Capital Market's ``NORMAL_STATUS``/``ODDLOT_STATUS``/``SPOT_STATUS``/
``AUCTION_STATUS`` tags **three times over** for this venue's three
per-market-status sub-structures (``ST_MARKET_STATUS``, ``ST_EX_MARKET_STATUS``
and ``ST_PL_MARKET_STATUS``, all three present on one message,
``SYSTEM_INFORMATION_OUT``). A :class:`~exchangesim.fix.message.Message` is
keyed by tag, so decoding a message that has *three* two-byte structures
mapped to the *same four tags* would silently overwrite the first two with the
third -- exactly the kind of data loss this project's testing philosophy
exists to catch, not reintroduce. Only the first sub-structure reuses tags
that also mean "market status" in Capital Market's dialect (a genuine concept
match); the second and third mint their own, since Capital Market has no
equivalent for either and there is nothing to collide with anyway.

Where a concept genuinely coincides with a FIX field -- a symbol, a side, a
quantity, a price, an exchange-assigned order identifier -- that field's tag
is reused from FIX, exactly as Capital Market's dictionary does:

======  ==========================================================
9400s   the message header
9410s   ``CONTRACT_DESC``, the instrument identity
9440s   ``MS_OE_REQUEST`` fields beyond the FIX reuse list
9490s   ``PRICE_MOD`` and ``ERROR_RESPONSE`` fields
9500s   ``MS_TRADE_CONFIRM`` fields beyond what it shares with the order
9530s   ``MS_SIGNON`` fields
9550s   ``SYSTEM_INFORMATION_OUT`` fields, including the three market-status
        sub-structures
9600s   ``ADDITIONAL_ORDER_FLAGS``, one tag per bit
9620s   ``ST_ORDER_FLAGS``, one tag per bit
9650s   the Gateway Router and the secure box
======  ==========================================================

Four FIX tags are deliberately **not** defined here, for the same reasons
Capital Market's dictionary gives, restated because this venue is a
completely separate protocol from FIX's own derivatives dialect and it would
be easy to assume otherwise. ``ClOrdID(11)`` and ``OrigClOrdID(41)``, because
NNF has no client-supplied order handle at all -- modification and
cancellation address the exchange's own ``OrderNumber``. ``OrdType(40)`` and
``TimeInForce(59)``, because this venue spells both as bits of
``ST_ORDER_FLAGS`` rather than as scalar fields; a scalar ``OrderType`` field
does exist on the wire (offset 96 of ``MS_OE_REQUEST``) but every description
of it in the Order Entry chapter says only "should be set to blank" for a
Regular Lot transaction, so it carries no value domain FIX's ``OrdType``
would recognise and is transcribed as a field that must be blank, not as
``OrdType`` under another name. ``StrikePx(202)`` and ``PutOrCall(201)`` are
also avoided: F&O's strike carries the sentinel ``-1`` for a futures contract,
which is not a legal FIX ``StrikePx`` value, and F&O's option type is a
genuine three-valued enumeration (``CE``/``PE``/``XX``) against FIX's
two-valued ``PutOrCall``.

A handful of field value domains here -- ``BuySell``, ``ProClient``,
``ModCxlBy`` and ``UserType`` -- are carried over from Capital Market's own
values on the reading that this document uses the same role vocabulary
(Corporate Manager, Branch Manager, Dealer) and the same buy/sell and
pro/client concepts, but every page the transcription found describing these
domains directly had its actual value list elided by the PDF extractor. They
are marked **ASSUMPTION, unverified** below and should be revisited before a
real F&O client's traffic is trusted against them; see
``docs/specs/NSE_FO_TRANSCRIPTION.md`` §6 for the reasoning.
"""

from ...fix.constants import FieldType
from ...fix.dictionary import Dictionary, FieldDef, MessageDef, enum_labels
from ...nnf.layout import TRANSACTION_CODE
from . import transactions as X

#: Names this dialect, in the slot a FIX dictionary uses for its BeginString.
BEGIN_STRING = "NNF.FO"

#: ASSUMPTION: the transcription does not restate the "multiply by 100 before
#: sending" rule for this document specifically (it is Capital Market's own
#: Chapter 2 that says so), but every money field here is the same LONG
#: integer shape as Capital Market's, and nothing in the F&O document
#: contradicts the convention. Two decimal places, matching Capital Market,
#: until a real client's traffic says otherwise.
PRICE_DECIMALS = 2


# -- tags ----------------------------------------------------------------

# The message header (transcription §1.1, forty bytes, byte-for-byte
# identical to Capital Market's -- restated independently rather than
# imported, because the two source documents are independent and a future
# revision of one must not silently change the other's tags. Only the
# constructed name of tag 9402 differs by convention: this document's own
# table calls the offset-8 LONG "TraderId", not "UserId".
MSG_SEQ_NUM = 34                # the packet's sequence number, not the message's
LOG_TIME = 9400
ALPHA_CHAR = 9401
USER_ID = 9402                  # header's TraderId; see the module docstring
ERROR_CODE = 9403
TIMESTAMP = 9404
TIMESTAMP1 = 9405
TIMESTAMP2 = 9406
MESSAGE_LENGTH = 9407

# CONTRACT_DESC: the instrument identity (transcription §1.2).
SYMBOL = 55                     # Symbol: the underlying's name
INSTRUMENT_NAME = 9410
EXPIRY_DATE = 9411
STRIKE_PRICE = 9412
OPTION_TYPE = 9413
CA_LEVEL = 9414

# MS_OE_REQUEST (transcription §1.4).
PARTICIPANT_TYPE = 9440
COMPETITOR_PERIOD = 9441
SOLICITOR_PERIOD = 9442
MOD_CXL_BY = 9443
REASON_CODE = 9444
TOKEN_NO = 9445
COUNTERPARTY_BROKER_ID = 9446
CLOSEOUT_FLAG = 9447
ORDER_TYPE = 9448
BOOK_TYPE = 9449
DISCLOSED_VOL = 9450
DISCLOSED_VOL_REMAINING = 9451
TOTAL_VOL_REMAINING = 151       # LeavesQty
VOLUME = 38                     # OrderQty
VOLUME_FILLED_TODAY = 14        # CumQty
PRICE = 44
TRIGGER_PRICE = 9452
GOOD_TILL_DATE = 9453
ENTRY_DATE_TIME = 9454
MIN_FILL_AON = 9455
LAST_MODIFIED = 9456
BRANCH_ID = 9457
TRADER_ID = 9458                # the order's own TraderId; distinct from USER_ID
BROKER_ID = 9459
OPEN_CLOSE = 9460
SETTLOR = 9461
PRO_CLIENT = 9462
SETTLEMENT_PERIOD = 9463
NNF_FIELD = 9464
MKT_REPLAY = 9465                # LONG LONG, not CM's DOUBLE ExecTimeStamp
PAN = 9466
ALGO_ID = 9467
LAST_ACTIVITY_REFERENCE = 9468
ORDER_NUMBER = 37                # OrderID: exchange-assigned
ACCOUNT_NUMBER = 1               # Account
BUY_SELL = 54                    # Side

# PRICE_MOD, an F&O-only structure (transcription §1.5).
REFERENCE = 9490

# The order and trade download, the recovery this protocol has instead of a
# resend request -- defined so the request can be read and refused by name,
# matching Capital Market's own treatment (see build_fo()).
DOWNLOAD_SEQUENCE = 9492

# ERROR_RESPONSE_OUT: keyed by a bare contract token, not SEC_INFO.
KEY = 9491
ERROR_MESSAGE = 58               # Text

# MS_TRADE_CONFIRM, beyond what it shares with the order structure
# (transcription §1.6).
RESPONSE_ORDER_NUMBER = 9500
TRADER_NUMBER = 9501
FILL_NUMBER = 9502
FILL_QTY = 32                    # LastQty
FILL_PRICE = 31                  # LastPx
ACTIVITY_TYPE = 9503
ACTIVITY_TIME = 9504
COUNTER_TRADER_ORDER_NUMBER = 9505
COUNTER_BROKER_ID = 9506
OLD_OPEN_CLOSE = 9507
OLD_ACCOUNT_NUMBER = 9508
PARTICIPANT = 9509
OLD_PARTICIPANT = 9510
OLD_PAN = 9511

# MS_SIGNON (transcription §1.7 -- its own transcription, not Capital
# Market's, even though the transaction codes collide).
SIGNON_USER_ID = 9530
PASSWORD = 9531
NEW_PASSWORD = 9532
TRADER_NAME = 9533
LAST_PASSWORD_CHANGE = 9534
VERSION_NUMBER = 9535
BATCH2_START_TIME = 9536         # also EndTime, on the OUT direction
HOST_SWITCH_CONTEXT = 9537
COLOUR = 9538
USER_TYPE = 9539
SEQUENCE_NUMBER = 9540
WS_CLASS_NAME = 9541
BROKER_STATUS = 9542
SHOW_INDEX = 9543
MEMBER_TYPE = 9544
CLEARING_STATUS = 9545
BROKER_NAME = 9546

# SIGN_OFF_REQUEST_OUT's own undocumented payload (transcription §1.11).
SIGNOFF_USER_ID = 9547

# SYSTEM_INFORMATION_OUT (transcription §1.8).
MARKET_INDEX = 9550
DEFAULT_SETTLEMENT_NORMAL = 9551
DEFAULT_SETTLEMENT_SPOT = 9552
DEFAULT_SETTLEMENT_AUCTION = 9553
WARNING_PERCENT = 9554
VOLUME_FREEZE_PERCENT = 9555
SNAP_QUOTE_TIME = 9556
BOARD_LOT_QUANTITY = 9557
TICK_SIZE = 9558
MAXIMUM_GTC_DAYS = 9559
DISCLOSED_QUANTITY_PERCENT = 9560
RISK_FREE_INTEREST_RATE = 9561
UPDATE_PORTFOLIO = 9562
LAST_UPDATE_PORTFOLIO_TIME = 9563
# ST_MARKET_STATUS: the one sub-structure with a genuine Capital Market
# analogue (its own per-market status), so it is the one that could
# legitimately share a *concept* -- it still mints its own tags (see the
# module docstring for why reusing Capital Market's literally would corrupt
# the other two sub-structures sharing this message).
NORMAL_STATUS = 9564
ODDLOT_STATUS = 9565
SPOT_STATUS = 9566
AUCTION_STATUS = 9567
# ST_EX_MARKET_STATUS: no Capital Market equivalent at all ("EX" is not
# expanded in the pages read -- transcription §6).
EX_NORMAL_STATUS = 9568
EX_ODDLOT_STATUS = 9569
EX_SPOT_STATUS = 9570
EX_AUCTION_STATUS = 9571
# ST_PL_MARKET_STATUS: likewise ("PL" not expanded either).
PL_NORMAL_STATUS = 9572
PL_ODDLOT_STATUS = 9573
PL_SPOT_STATUS = 9574
PL_AUCTION_STATUS = 9575
# ST_STOCK_ELIGIBLE_INDICATORS.
SECURITY_AON = 9576
SECURITY_MIN_FILL = 9577
SECURITY_BOOKS_MERGED = 9578

# ADDITIONAL_ORDER_FLAGS, one tag per bit (transcription §1.3) -- a whole
# structure Capital Market has no equivalent of at all.
FLAG_BOC = 9600
FLAG_COL = 9601
FLAG_STPC_ADDITIONAL = 9602

# ST_ORDER_FLAGS, one tag per bit, in the order the big-endian table prints
# (transcription §1.3). Bit-for-bit different from Capital Market's own
# ST_ORDER_FLAGS despite the identical structure name: SL and MIT replace
# Capital Market's single OnStop bit, MF moves to byte 1, and there is no
# STPC bit at all -- that moved to ADDITIONAL_ORDER_FLAGS above.
FLAG_ATO = 9620
FLAG_MARKET = 9621
FLAG_SL = 9622
FLAG_MIT = 9623
FLAG_DAY = 9624
FLAG_GTC = 9625
FLAG_IOC = 9626
FLAG_AON = 9627
FLAG_MF = 9628
FLAG_MATCHED_IND = 9629
FLAG_TRADED = 9630
FLAG_MODIFIED = 9631
FLAG_FROZEN = 9632
FLAG_PREOPEN = 9633

# The Gateway Router and the secure box (transcription §1.10 -- byte-for-byte
# identical to Capital Market's own structures).
BOX_ID = 9650
IP_ADDRESS = 9651
PORT = 9652
SESSION_KEY = 9653
CRYPTOGRAPHIC_KEY = 9654
STATIC_IV = 9655
DYNAMIC_IV = 9656
ADDITIONAL_KEY = 9657


# -- value domains ---------------------------------------------------------

#: What a numeric field holds when nobody set it -- the same "zero means
#: absent" convention Capital Market's dictionary documents, and the same
#: caveat: it is added only to enumerations whose own domain does not already
#: use zero for something real.
NOT_SET = "0"


class InstrumentName(object):
    """``CONTRACT_DESC.InstrumentName``: the four instrument families in
    scope. Left-justified, blank-padded CHAR(6) on the wire."""

    FUTURES_INDEX = "FUTIDX"
    FUTURES_STOCK = "FUTSTK"
    OPTIONS_INDEX = "OPTIDX"
    OPTIONS_STOCK = "OPTSTK"


class OptionType(object):
    """``CONTRACT_DESC.OptionType``: a genuine three-valued enumeration, not
    a call/put boolean. ``FUTURES`` is the sentinel a futures contract
    carries in this field -- meaningless, but still populated."""

    CALL = "CE"
    PUT = "PE"
    FUTURES = "XX"


class BuySell(object):
    """The same 1/2 FIX spells on tag 54.

    ASSUMPTION, unverified (transcription §6): the value list itself was
    elided by the PDF extractor everywhere it appears in this document; this
    is carried over from Capital Market's identical convention on the
    reading that the two documents share the same buy/sell vocabulary.
    """

    BUY = "1"
    SELL = "2"


class BookType(object):
    """The seven books, of which this venue trades exactly one.

    Book 3 (Stop Loss / MIT) conflates two order *types* under one book ID,
    distinguished by the ``SL``/``MIT`` bits of ``ST_ORDER_FLAGS`` rather than
    by book type -- unlike Capital Market, which has no such conflation.
    Everything but ``REGULAR_LOT`` is rejected rather than promoted, the same
    choice Capital Market's dictionary makes for its own book types.
    """

    REGULAR_LOT = "1"
    SPECIAL_TERMS = "2"
    STOP_LOSS_OR_MIT = "3"
    NEGOTIATED = "4"
    ODD_LOT = "5"
    SPOT = "6"
    AUCTION = "7"


class MarketStatus(object):
    """What ``SYSTEM_INFORMATION_OUT`` reports per market.

    A fifth status relative to Capital Market: ``POST_CLOSE``, a genuine
    additional trading phase (transcription §2.2) in which only market
    orders in RL/ST book types are accepted at all.
    """

    PRE_OPEN = "0"
    OPEN = "1"
    CLOSED = "2"
    PRE_OPEN_ENDED = "3"
    POST_CLOSE = "4"


class ProClient(object):
    """Whose account an order is for: the member's own, or a client's.

    ASSUMPTION, unverified (transcription §6) -- see :class:`BuySell`.
    """

    CLIENT = "1"
    PRO = "2"


class UserType(object):
    """ASSUMPTION, unverified (transcription §6) -- see :class:`BuySell`."""

    CORPORATE_MANAGER = "0"
    BRANCH_MANAGER = "1"
    DEALER = "2"


class ModCxlBy(object):
    """Who modified or cancelled an order.

    ASSUMPTION, unverified (transcription §6) -- see :class:`BuySell`.
    """

    TRADER = "T"
    BRANCH_MANAGER = "B"
    CORPORATE_MANAGER = "M"
    EXCHANGE = "E"


class ActivityType(object):
    """``MS_TRADE_CONFIRM.ActivityType`` -- the full published table
    (transcription §2.5), verbatim including the numbering gap (there is no
    code between 20 and 43)."""

    ORIGINAL_ORDER = "1"
    ACTIVITY_TRADE = "2"
    ACTIVITY_ORDER_CXL = "3"
    ACTIVITY_ORDER_MOD = "4"
    ACTIVITY_TRADE_MOD = "5"
    ACTIVITY_TRADE_CXL_1 = "6"
    ACTIVITY_TRADE_CXL_2 = "7"
    ACTIVITY_BATCH_ORDER_CXL = "8"
    ACTIVITY_ORDER_MOD_REJECT = "9"
    ACTIVITY_TRADE_MOD_REJECT = "10"
    ACTIVITY_TRADE_CXL_REJECT = "11"
    ACTIVITY_ORDER_REJECTED = "12"
    ACTIVITY_ORDER_IN_BOOK = "13"
    ACTIVITY_ORDER_CXL_REJECT = "14"
    ACTIVITY_PRICE_FREEZE_IN = "15"
    ACTIVITY_PRICE_FREEZE_CXLD = "16"
    ACTIVITY_FREEZE_ADMIN_SUSP = "17"
    ACTIVITY_QTY_FREEZE_IN = "18"
    ACTIVITY_QTY_FREEZE_CXLD = "19"
    ACTIVITY_ORD_BROKER_SUSP = "20"
    ACTIVITY_SPREAD_TRADE_CXL = "43"


# -- fields ----------------------------------------------------------------

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


def _market_status(tag, name):
    return FieldDef(tag, name, FieldType.STRING, labels=enum_labels(MarketStatus))


def header_fields():
    """The forty-byte message header, plus the packet's sequence number."""
    return [
        FieldDef(TRANSACTION_CODE, "TransactionCode", FieldType.STRING),
        _number(MSG_SEQ_NUM, "SequenceNumber"),
        _number(LOG_TIME, "LogTime"),
        _text(ALPHA_CHAR, "AlphaChar", 2),
        # Named "UserId" here (matching Capital Market's convention and
        # avoiding a name collision with the order's own, separate TraderId
        # field below) even though this document's own table calls the same
        # offset-8 LONG "TraderId" -- see the module and layouts docstrings.
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
        # -- the contract -----------------------------------------------
        _text(SYMBOL, "Symbol", 10),
        FieldDef(INSTRUMENT_NAME, "InstrumentName", FieldType.STRING,
                 values=(InstrumentName.FUTURES_INDEX,
                         InstrumentName.FUTURES_STOCK,
                         InstrumentName.OPTIONS_INDEX,
                         InstrumentName.OPTIONS_STOCK),
                 labels=enum_labels(InstrumentName)),
        _number(EXPIRY_DATE, "ExpiryDate"),
        # -1 is a literal sentinel for a futures contract, not "unset" --
        # see layouts.StrikeScaled. No value constraint here beyond the
        # ordinary PRICE format check, which already accepts a leading '-'.
        FieldDef(STRIKE_PRICE, "StrikePrice", FieldType.PRICE,
                 max_decimals=PRICE_DECIMALS),
        FieldDef(OPTION_TYPE, "OptionType", FieldType.STRING,
                 values=(OptionType.CALL, OptionType.PUT, OptionType.FUTURES),
                 labels=enum_labels(OptionType)),
        _number(CA_LEVEL, "CALevel"),

        # -- the order ----------------------------------------------------
        _text(PARTICIPANT_TYPE, "ParticipantType", 1),
        _number(COMPETITOR_PERIOD, "CompetitorPeriod"),
        _number(SOLICITOR_PERIOD, "SolicitorPeriod"),
        FieldDef(MOD_CXL_BY, "ModCxlBy", FieldType.STRING,
                 labels=enum_labels(ModCxlBy)),
        FieldDef(REASON_CODE, "ReasonCode", FieldType.INT,
                 labels={str(code): name
                         for code, name in X.ERROR_CODES.items()}),
        _number(TOKEN_NO, "TokenNo"),
        _text(COUNTERPARTY_BROKER_ID, "CounterPartyBrokerId", 5),
        _text(CLOSEOUT_FLAG, "CloseoutFlag", 1),
        # ASSUMPTION (transcription §6): every RL transaction code's own
        # description says this field "should be set to blank" -- accept
        # only the unset value until a page describing a real domain for it
        # turns up.
        FieldDef(ORDER_TYPE, "OrderType", FieldType.STRING,
                 values=(NOT_SET,), labels={NOT_SET: "NOT_SET"}),
        _text(ORDER_NUMBER, "OrderNumber", 20),
        _text(ACCOUNT_NUMBER, "AccountNumber", 10),
        FieldDef(BOOK_TYPE, "BookType", FieldType.STRING,
                 values=(NOT_SET, BookType.REGULAR_LOT, BookType.SPECIAL_TERMS,
                         BookType.STOP_LOSS_OR_MIT, BookType.NEGOTIATED,
                         BookType.ODD_LOT, BookType.SPOT, BookType.AUCTION),
                 labels=_with_not_set(enum_labels(BookType))),
        FieldDef(BUY_SELL, "BuySell", FieldType.STRING,
                 values=(NOT_SET, BuySell.BUY, BuySell.SELL),
                 labels=_with_not_set(enum_labels(BuySell))),
        FieldDef(DISCLOSED_VOL, "DisclosedVolume", FieldType.QTY),
        FieldDef(DISCLOSED_VOL_REMAINING, "DisclosedVolumeRemaining",
                 FieldType.QTY),
        FieldDef(TOTAL_VOL_REMAINING, "TotalVolRemaining", FieldType.QTY),
        FieldDef(VOLUME, "Volume", FieldType.QTY),
        FieldDef(VOLUME_FILLED_TODAY, "VolumeFilledToday", FieldType.QTY),
        FieldDef(PRICE, "Price", FieldType.PRICE, max_decimals=PRICE_DECIMALS),
        FieldDef(TRIGGER_PRICE, "TriggerPrice", FieldType.PRICE,
                 max_decimals=PRICE_DECIMALS),
        _number(GOOD_TILL_DATE, "GoodTillDate"),
        _number(ENTRY_DATE_TIME, "EntryDateTime"),
        FieldDef(MIN_FILL_AON, "MinimumFillOrAonVolume", FieldType.QTY),
        _number(LAST_MODIFIED, "LastModified"),
        _number(BRANCH_ID, "BranchId"),
        _number(TRADER_ID, "TraderId"),
        _text(BROKER_ID, "BrokerId", 5),
        # ASSUMPTION: no value domain found for Open/Close in the pages
        # read; Capital Market's cash market has no equivalent concept at
        # all, since equities have no notion of opening or closing a
        # derivatives position.
        _text(OPEN_CLOSE, "OpenClose", 1),
        _text(SETTLOR, "Settlor", 12),
        FieldDef(PRO_CLIENT, "ProClient", FieldType.STRING,
                 values=(NOT_SET, ProClient.CLIENT, ProClient.PRO),
                 labels=_with_not_set(enum_labels(ProClient))),
        _number(SETTLEMENT_PERIOD, "SettlementPeriod"),
        _text(NNF_FIELD, "NNFField", 20),
        _number(MKT_REPLAY, "MktReplay"),
        _text(PAN, "PAN", 10),
        _number(ALGO_ID, "AlgoID"),
        _number(LAST_ACTIVITY_REFERENCE, "LastActivityReference"),

        # -- PRICE_MOD ------------------------------------------------------
        _text(REFERENCE, "Reference", 4),

        # -- recovery ---------------------------------------------------
        _number(DOWNLOAD_SEQUENCE, "DownloadSequence"),

        # -- ERROR_RESPONSE_OUT ----------------------------------------------
        _text(KEY, "Key", 14),
        _text(ERROR_MESSAGE, "ErrorMessage", 128),

        # -- the trade ------------------------------------------------------
        _text(RESPONSE_ORDER_NUMBER, "ResponseOrderNumber", 20),
        _number(TRADER_NUMBER, "TraderNumber"),
        _number(FILL_NUMBER, "FillNumber"),
        FieldDef(FILL_QTY, "FillQty", FieldType.QTY),
        FieldDef(FILL_PRICE, "FillPrice", FieldType.PRICE,
                 max_decimals=PRICE_DECIMALS),
        FieldDef(ACTIVITY_TYPE, "ActivityType", FieldType.STRING,
                 labels=enum_labels(ActivityType)),
        _number(ACTIVITY_TIME, "ActivityTime"),
        _text(COUNTER_TRADER_ORDER_NUMBER, "CounterTraderOrderNumber", 20),
        _text(COUNTER_BROKER_ID, "CounterBrokerId", 5),
        _text(OLD_OPEN_CLOSE, "OldOpenClose", 1),
        _text(OLD_ACCOUNT_NUMBER, "OldAccountNumber", 10),
        _text(PARTICIPANT, "Participant", 12),
        _text(OLD_PARTICIPANT, "OldParticipant", 12),
        _text(OLD_PAN, "OldPAN", 10),

        # -- sign-on ----------------------------------------------------
        _number(SIGNON_USER_ID, "UserId"),
        _text(PASSWORD, "Password", 8, redact=True),
        _text(NEW_PASSWORD, "NewPassword", 8, redact=True),
        _text(TRADER_NAME, "TraderName", 26),
        _number(LAST_PASSWORD_CHANGE, "LastPasswordChangeDate"),
        _number(VERSION_NUMBER, "VersionNumber"),
        _number(BATCH2_START_TIME, "Batch2StartTimeOrEndTime"),
        _text(HOST_SWITCH_CONTEXT, "HostSwitchContext", 1),
        # "should be set to blank" (transcription §1.7) -- no domain given.
        _text(COLOUR, "Colour", 50),
        FieldDef(USER_TYPE, "UserType", FieldType.STRING,
                 labels=enum_labels(UserType)),
        _text(SEQUENCE_NUMBER, "SequenceNumber", 20),
        _text(WS_CLASS_NAME, "WsClassName", 14),
        _text(BROKER_STATUS, "BrokerStatus", 1),
        _text(SHOW_INDEX, "ShowIndex", 1),
        _number(MEMBER_TYPE, "MemberType"),
        _text(CLEARING_STATUS, "ClearingStatus", 1),
        _text(BROKER_NAME, "BrokerName", 25),
        _number(SIGNOFF_USER_ID, "SignOffUserId"),

        # -- system information -------------------------------------------
        _number(MARKET_INDEX, "MarketIndex"),
        _number(DEFAULT_SETTLEMENT_NORMAL, "DefaultSettlementPeriodNormal"),
        _number(DEFAULT_SETTLEMENT_SPOT, "DefaultSettlementPeriodSpot"),
        _number(DEFAULT_SETTLEMENT_AUCTION, "DefaultSettlementPeriodAuction"),
        _number(WARNING_PERCENT, "WarningPercent"),
        _number(VOLUME_FREEZE_PERCENT, "VolumeFreezePercent"),
        _number(SNAP_QUOTE_TIME, "SnapQuoteTime"),
        FieldDef(BOARD_LOT_QUANTITY, "BoardLotQuantity", FieldType.QTY),
        FieldDef(TICK_SIZE, "TickSize", FieldType.PRICE,
                 max_decimals=PRICE_DECIMALS),
        _number(MAXIMUM_GTC_DAYS, "MaximumGtcDays"),
        _number(DISCLOSED_QUANTITY_PERCENT, "DisclosedQuantityPercentAllowed"),
        # ASSUMPTION: no decimal scale given for an options-pricing input
        # Capital Market has no use for; carried as a bare integer.
        _number(RISK_FREE_INTEREST_RATE, "RiskFreeInterestRate"),
        _text(UPDATE_PORTFOLIO, "UpdatePortfolio", 1),
        _number(LAST_UPDATE_PORTFOLIO_TIME, "LastUpdatePortfolioTime"),
        _market_status(NORMAL_STATUS, "NormalMarketStatus"),
        _market_status(ODDLOT_STATUS, "OddlotMarketStatus"),
        _market_status(SPOT_STATUS, "SpotMarketStatus"),
        _market_status(AUCTION_STATUS, "AuctionMarketStatus"),
        _market_status(EX_NORMAL_STATUS, "ExNormalMarketStatus"),
        _market_status(EX_ODDLOT_STATUS, "ExOddlotMarketStatus"),
        _market_status(EX_SPOT_STATUS, "ExSpotMarketStatus"),
        _market_status(EX_AUCTION_STATUS, "ExAuctionMarketStatus"),
        _market_status(PL_NORMAL_STATUS, "PlNormalMarketStatus"),
        _market_status(PL_ODDLOT_STATUS, "PlOddlotMarketStatus"),
        _market_status(PL_SPOT_STATUS, "PlSpotMarketStatus"),
        _market_status(PL_AUCTION_STATUS, "PlAuctionMarketStatus"),
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

        # -- ADDITIONAL_ORDER_FLAGS ------------------------------------------
        _flag(FLAG_BOC, "BOC"),
        _flag(FLAG_COL, "COL"),
        _flag(FLAG_STPC_ADDITIONAL, "STPC"),

        # -- ST_ORDER_FLAGS ---------------------------------------------------
        _flag(FLAG_ATO, "ATO"),
        _flag(FLAG_MARKET, "Market"),
        _flag(FLAG_SL, "SL"),
        _flag(FLAG_MIT, "MIT"),
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
    ]


# -- messages ----------------------------------------------------------------
#
# ``optional`` throughout, and ``required`` nowhere -- see the module
# docstring and Capital Market's own ``dictionary.py`` for why.

def _message(code, name, tags, inbound=True):
    return MessageDef(str(code), name, optional=tuple(tags), inbound=inbound)


_CONTRACT_TAGS = (SYMBOL, INSTRUMENT_NAME, EXPIRY_DATE, STRIKE_PRICE,
                  OPTION_TYPE, CA_LEVEL)

_ORDER_TAGS = _CONTRACT_TAGS + (
    PARTICIPANT_TYPE, COMPETITOR_PERIOD, SOLICITOR_PERIOD, MOD_CXL_BY,
    REASON_CODE, TOKEN_NO, COUNTERPARTY_BROKER_ID, CLOSEOUT_FLAG, ORDER_TYPE,
    ORDER_NUMBER, ACCOUNT_NUMBER, BOOK_TYPE, BUY_SELL, DISCLOSED_VOL,
    DISCLOSED_VOL_REMAINING, TOTAL_VOL_REMAINING, VOLUME, VOLUME_FILLED_TODAY,
    PRICE, TRIGGER_PRICE, GOOD_TILL_DATE, ENTRY_DATE_TIME, MIN_FILL_AON,
    LAST_MODIFIED, BRANCH_ID, TRADER_ID, BROKER_ID, OPEN_CLOSE, SETTLOR,
    PRO_CLIENT, SETTLEMENT_PERIOD, NNF_FIELD, MKT_REPLAY, PAN, ALGO_ID,
    LAST_ACTIVITY_REFERENCE,
    FLAG_ATO, FLAG_MARKET, FLAG_SL, FLAG_MIT, FLAG_DAY, FLAG_GTC, FLAG_IOC,
    FLAG_AON, FLAG_MF, FLAG_MATCHED_IND, FLAG_TRADED, FLAG_MODIFIED,
    FLAG_FROZEN, FLAG_PREOPEN,
    FLAG_BOC, FLAG_COL, FLAG_STPC_ADDITIONAL,
)

_PRICE_MOD_TAGS = (
    TOKEN_NO, TRADER_ID, ORDER_NUMBER, BUY_SELL, PRICE, VOLUME, LAST_MODIFIED,
    REFERENCE, LAST_ACTIVITY_REFERENCE,
)

_TRADE_TAGS = _CONTRACT_TAGS + (
    RESPONSE_ORDER_NUMBER, BROKER_ID, TRADER_NUMBER, ACCOUNT_NUMBER, BUY_SELL,
    VOLUME, DISCLOSED_VOL, TOTAL_VOL_REMAINING, DISCLOSED_VOL_REMAINING, PRICE,
    GOOD_TILL_DATE, FILL_NUMBER, FILL_QTY, FILL_PRICE, VOLUME_FILLED_TODAY,
    ACTIVITY_TYPE, ACTIVITY_TIME, COUNTER_TRADER_ORDER_NUMBER,
    COUNTER_BROKER_ID, TOKEN_NO, OPEN_CLOSE, OLD_OPEN_CLOSE, BOOK_TYPE,
    OLD_ACCOUNT_NUMBER, PARTICIPANT, OLD_PARTICIPANT, PAN,
    OLD_PAN, ALGO_ID, LAST_ACTIVITY_REFERENCE,
    FLAG_ATO, FLAG_MARKET, FLAG_SL, FLAG_MIT, FLAG_DAY, FLAG_GTC, FLAG_IOC,
    FLAG_AON, FLAG_MF, FLAG_MATCHED_IND, FLAG_TRADED, FLAG_MODIFIED,
    FLAG_FROZEN, FLAG_PREOPEN,
    FLAG_BOC, FLAG_COL, FLAG_STPC_ADDITIONAL,
)

_SIGNON_TAGS = (
    SIGNON_USER_ID, PASSWORD, NEW_PASSWORD, TRADER_NAME, LAST_PASSWORD_CHANGE,
    BROKER_ID, BRANCH_ID, VERSION_NUMBER, BATCH2_START_TIME,
    HOST_SWITCH_CONTEXT, COLOUR, USER_TYPE, SEQUENCE_NUMBER, WS_CLASS_NAME,
    BROKER_STATUS, SHOW_INDEX, MEMBER_TYPE, CLEARING_STATUS, BROKER_NAME,
)

_SYSTEM_TAGS = (
    NORMAL_STATUS, ODDLOT_STATUS, SPOT_STATUS, AUCTION_STATUS,
    EX_NORMAL_STATUS, EX_ODDLOT_STATUS, EX_SPOT_STATUS, EX_AUCTION_STATUS,
    PL_NORMAL_STATUS, PL_ODDLOT_STATUS, PL_SPOT_STATUS, PL_AUCTION_STATUS,
    UPDATE_PORTFOLIO, MARKET_INDEX, DEFAULT_SETTLEMENT_NORMAL,
    DEFAULT_SETTLEMENT_SPOT, DEFAULT_SETTLEMENT_AUCTION, COMPETITOR_PERIOD,
    SOLICITOR_PERIOD, WARNING_PERCENT, VOLUME_FREEZE_PERCENT,
    SNAP_QUOTE_TIME, BOARD_LOT_QUANTITY, TICK_SIZE, MAXIMUM_GTC_DAYS,
    SECURITY_AON, SECURITY_MIN_FILL, SECURITY_BOOKS_MERGED,
    DISCLOSED_QUANTITY_PERCENT, RISK_FREE_INTEREST_RATE,
)


def application_messages():
    return [
        # -- the box connection ---------------------------------------------
        _message(X.SECURE_BOX_REGISTRATION_REQUEST_IN,
                 "SECURE_BOX_REGISTRATION_REQUEST_IN", (BOX_ID,)),
        _message(X.SECURE_BOX_REGISTRATION_REQUEST_OUT,
                 "SECURE_BOX_REGISTRATION_RESPONSE_OUT", (), inbound=False),
        _message(X.BOX_SIGN_ON_REQUEST_IN, "BOX_SIGN_ON_REQUEST_IN",
                 (BOX_ID, BROKER_ID, SESSION_KEY)),
        _message(X.BOX_SIGN_ON_REQUEST_OUT, "BOX_SIGN_ON_REQUEST_OUT",
                 (BOX_ID,), inbound=False),
        _message(X.BOX_SIGN_OFF, "BOX_SIGN_OFF", (BOX_ID,), inbound=False),
        _message(X.GR_REQUEST, "GR_REQUEST", (BOX_ID, BROKER_ID)),
        _message(X.GR_RESPONSE, "GR_RESPONSE",
                 (BOX_ID, BROKER_ID, IP_ADDRESS, PORT, SESSION_KEY,
                  CRYPTOGRAPHIC_KEY, STATIC_IV, DYNAMIC_IV, ADDITIONAL_KEY),
                 inbound=False),

        # -- the user session -------------------------------------------
        _message(X.SIGN_ON_REQUEST_IN, "MS_SIGNON", _SIGNON_TAGS),
        _message(X.SIGN_ON_REQUEST_OUT, "MS_SIGNON", _SIGNON_TAGS,
                 inbound=False),
        _message(X.SIGN_OFF_REQUEST_IN, "SIGN_OFF_REQUEST_IN", ()),
        _message(X.SIGN_OFF_REQUEST_OUT, "SIGNOFF_OUT", (SIGNOFF_USER_ID,),
                 inbound=False),
        _message(X.HEARTBEAT, "HEARTBEAT", ()),
        _message(X.ERROR_RESPONSE_OUT, "MS_ERROR_RESPONSE", (KEY, ERROR_MESSAGE),
                 inbound=False),
        _message(X.SYSTEM_INFORMATION_IN, "MS_SYSTEM_INFO_REQ",
                 (LAST_UPDATE_PORTFOLIO_TIME,)),
        _message(X.SYSTEM_INFORMATION_OUT, "MS_SYSTEM_INFO_DATA", _SYSTEM_TAGS,
                 inbound=False),

        # -- order entry ----------------------------------------------------
        _message(X.BOARD_LOT_IN, "MS_OE_REQUEST", _ORDER_TAGS),
        _message(X.ORDER_MOD_IN, "MS_OE_REQUEST", _ORDER_TAGS),
        _message(X.ORDER_CANCEL_IN, "MS_OE_REQUEST", _ORDER_TAGS),
        _message(X.ORDER_CONFIRMATION, "MS_OE_REQUEST", _ORDER_TAGS,
                 inbound=False),
        _message(X.ORDER_MOD_CONFIRMATION, "MS_OE_REQUEST", _ORDER_TAGS,
                 inbound=False),
        _message(X.ORDER_CANCEL_CONFIRMATION, "MS_OE_REQUEST", _ORDER_TAGS,
                 inbound=False),
        _message(X.ORDER_ERROR, "MS_OE_REQUEST", _ORDER_TAGS, inbound=False),
        _message(X.ORDER_MOD_REJECT, "MS_OE_REQUEST", _ORDER_TAGS,
                 inbound=False),
        _message(X.ORDER_CANCEL_REJECT, "MS_OE_REQUEST", _ORDER_TAGS,
                 inbound=False),
        _message(X.PRICE_CONFIRMATION, "MS_OE_REQUEST", _ORDER_TAGS,
                 inbound=False),
        _message(X.PRICE_MOD_IN, "PRICE_MOD", _PRICE_MOD_TAGS),

        # -- trades ---------------------------------------------------------
        _message(X.TRADE_CONFIRMATION, "MS_TRADE_CONFIRM", _TRADE_TAGS,
                 inbound=False),

        # -- recovery -------------------------------------------------------
        _message(X.DOWNLOAD_REQUEST, "MESSAGE_DOWNLOAD", (DOWNLOAD_SEQUENCE,)),
    ]


def build_fo():
    """The Futures & Options dialect."""
    return Dictionary(
        begin_string=BEGIN_STRING,
        fields=header_fields() + application_fields(),
        messages=application_messages(),
        header=tuple(field.tag for field in header_fields()))
