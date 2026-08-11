"""The Japannext FIX 4.2 dialect.

Transcribed from JNX_FIX_Trading_Specification_Equities_3.00. Field lengths and
enumerations are the specification's own -- section 7 lists the length limits,
and each message table lists the permitted values -- so a client sending
anything the real venue would refuse gets refused here too.

The specification follows FIX 4.2 but backports a handful of fields from 4.4
(``ExecRestatementReason(378)``, ``CxlRejResponseTo(434)``, ``CashMargin(544)``,
``LastLiquidityInd(851)``, ``TrdMatchID(880)``) and adds one user-defined tag,
``MarginTransactionType(8214)``.
"""

from ...fix import constants as C
from ...fix.dictionary import FieldDef, MessageDef, enum_labels
from ...fix.standard import build_session_dictionary

T = C.FieldType

# -- tag numbers -------------------------------------------------------------

ACCOUNT = 1
AVG_PX = 6
CL_ORD_ID = 11
CUM_QTY = 14
EXEC_ID = 17
EXEC_INST = 18
EXEC_TRANS_TYPE = 20
HANDL_INST = 21
LAST_PX = 31
LAST_SHARES = 32
ORDER_ID = 37
ORDER_QTY = 38
ORD_STATUS = 39
ORD_TYPE = 40
ORIG_CL_ORD_ID = 41
PRICE = 44
RULE_80A = 47
SIDE = 54
SYMBOL = 55
TIME_IN_FORCE = 59
TRANSACT_TIME = 60
CXL_REJ_REASON = 102
ORD_REJ_REASON = 103
CLIENT_ID = 109
MIN_QTY = 110
EXEC_TYPE = 150
LEAVES_QTY = 151
TRADING_SESSION_ID = 336
TRAD_SES_MODE = 339
TRAD_SES_STATUS = 340
EXEC_RESTATEMENT_REASON = 378
CXL_REJ_RESPONSE_TO = 434
CASH_MARGIN = 544
LAST_LIQUIDITY_IND = 851
TRD_MATCH_ID = 880
MARGIN_TRANSACTION_TYPE = 8214


# -- enumerations, exactly as the specification lists them -------------------

class OrdType(object):
    LIMIT = "2"          # the only value Japannext supports


class TimeInForce(object):
    DAY = "0"
    IOC = "3"
    FOK = "4"


class SideValue(object):
    BUY = "1"
    SELL = "2"
    SELL_SHORT = "5"
    SELL_SHORT_EXEMPT = "6"


class ExecInstValue(object):
    POST_ONLY = "6"              # "Participate, don't initiate"
    IGNORE_NOTIONAL = "x"


class OrdStatus(object):
    NEW = "0"
    PARTIALLY_FILLED = "1"
    FILLED = "2"
    CANCELED = "4"
    REPLACED = "5"
    PENDING_CANCEL = "6"
    REJECTED = "8"
    PENDING_NEW = "A"
    PENDING_REPLACE = "E"


class ExecType(object):
    NEW = "0"
    PARTIAL_FILL = "1"
    FILL = "2"
    CANCELED = "4"
    REPLACED = "5"
    REJECTED = "8"
    ORDER_STATUS = "I"


class ExecTransType(object):
    NEW = "0"
    STATUS = "3"


class OrdRejReason(object):
    BROKER_OPTION = 0
    UNKNOWN_SYMBOL = 1
    VENUE_CLOSED = 2
    EXCEEDS_LIMIT = 3
    DUPLICATE_ORDER = 6
    STALE_ORDER = 8
    UNSUPPORTED_CHARACTERISTIC = 11
    SURVEILLANCE_OPTION = 12
    INCORRECT_QUANTITY = 13
    PRICE_EXCEEDS_BAND = 16
    OTHER = 99


class CxlRejReason(object):
    TOO_LATE_TO_CANCEL = 0
    UNKNOWN_ORDER = 1
    VENUE_OPTION = 2
    ALREADY_PENDING = 3
    DUPLICATE_CL_ORD_ID = 6
    PRICE_EXCEEDS_BAND = 8
    OTHER = 99


class CxlRejResponseTo(object):
    CANCEL_REQUEST = "1"
    CANCEL_REPLACE_REQUEST = "2"


class ExecRestatementReason(object):
    VERBAL_CHANGE = 2
    CANCEL_ON_CONNECTION_LOSS = 12
    OTHER = 99
    TRADE_PREVENTION = 100
    CANCEL_ON_MARGIN_RESTRICTION = 102


class LastLiquidityInd(object):
    ADDED = "1"
    REMOVED = "2"


class Rule80A(object):
    AGENCY = "A"
    PRINCIPAL = "P"


class CashMargin(object):
    CASH = "1"
    MARGIN_OPEN = "2"
    MARGIN_CLOSE = "3"


class MarginTransactionType(object):
    NEGOTIABLE = "1"
    STANDARDIZED = "2"


class TradSesStatus(object):
    HALTED = "1"
    OPEN = "2"
    CLOSED = "3"


class TradSesMode(object):
    TESTING = "1"
    PRODUCTION = "3"


#: TargetSubID(57) / SenderSubID(50) values, one per market.
class SubID(object):
    J_MARKET_DAY = "DAY"
    J_MARKET_NIGHT = "NGHT"
    X_MARKET = "DAYX"
    U_MARKET = "DAYU"

    ALL = (J_MARKET_DAY, J_MARKET_NIGHT, X_MARKET, U_MARKET)

    LABELS = {
        J_MARKET_DAY: "J-Market Daytime Session",
        J_MARKET_NIGHT: "J-Market Nighttime Session",
        X_MARKET: "X-Market",
        U_MARKET: "U-Market",
    }


# -- field definitions -------------------------------------------------------

def application_fields():
    """Every application-level field, with the specification's limits.

    Section 7 of the specification caps Account at 10 characters, ClOrdID and
    OrigClOrdID at 32, OrderID and ExecID at 20, Symbol at 9, quantities at 9
    whole digits, prices at 8 whole digits and 1 decimal, and AvgPx at 8 and 4.
    """
    return [
        FieldDef(ACCOUNT, "Account", T.STRING, max_length=10),
        FieldDef(AVG_PX, "AvgPx", T.PRICE, max_digits=8, max_decimals=4),
        FieldDef(CL_ORD_ID, "ClOrdID", T.STRING, max_length=32),
        FieldDef(CUM_QTY, "CumQty", T.QTY, max_digits=9),
        FieldDef(EXEC_ID, "ExecID", T.STRING, max_length=20),
        FieldDef(EXEC_INST, "ExecInst", T.MULTI_VALUE_STRING,
                 values=(ExecInstValue.POST_ONLY, ExecInstValue.IGNORE_NOTIONAL),
                 labels=enum_labels(ExecInstValue)),
        FieldDef(EXEC_TRANS_TYPE, "ExecTransType", T.CHAR,
                 values=(ExecTransType.NEW, ExecTransType.STATUS),
                 labels=enum_labels(ExecTransType)),
        FieldDef(HANDL_INST, "HandlInst", T.CHAR, values=("1",)),
        FieldDef(LAST_PX, "LastPx", T.PRICE, max_digits=8, max_decimals=1),
        FieldDef(LAST_SHARES, "LastShares", T.QTY, max_digits=9),
        FieldDef(ORDER_ID, "OrderID", T.STRING, max_length=20),
        FieldDef(ORDER_QTY, "OrderQty", T.QTY, max_digits=9),
        FieldDef(ORD_STATUS, "OrdStatus", T.CHAR,
                 values=(OrdStatus.NEW, OrdStatus.PARTIALLY_FILLED,
                         OrdStatus.FILLED, OrdStatus.CANCELED,
                         OrdStatus.REPLACED, OrdStatus.PENDING_CANCEL,
                         OrdStatus.REJECTED, OrdStatus.PENDING_NEW,
                         OrdStatus.PENDING_REPLACE),
                 labels=enum_labels(OrdStatus)),
        FieldDef(ORD_TYPE, "OrdType", T.CHAR, values=(OrdType.LIMIT,),
                 labels=enum_labels(OrdType)),
        FieldDef(ORIG_CL_ORD_ID, "OrigClOrdID", T.STRING, max_length=32),
        FieldDef(PRICE, "Price", T.PRICE, max_digits=8, max_decimals=1),
        FieldDef(RULE_80A, "Rule80A", T.CHAR,
                 values=(Rule80A.AGENCY, Rule80A.PRINCIPAL),
                 labels=enum_labels(Rule80A)),
        FieldDef(SIDE, "Side", T.CHAR,
                 values=(SideValue.BUY, SideValue.SELL, SideValue.SELL_SHORT,
                         SideValue.SELL_SHORT_EXEMPT),
                 labels=enum_labels(SideValue)),
        FieldDef(SYMBOL, "Symbol", T.STRING, max_length=9),
        FieldDef(TIME_IN_FORCE, "TimeInForce", T.CHAR,
                 values=(TimeInForce.DAY, TimeInForce.IOC, TimeInForce.FOK),
                 labels=enum_labels(TimeInForce)),
        FieldDef(TRANSACT_TIME, "TransactTime", T.UTC_TIMESTAMP),
        FieldDef(CXL_REJ_REASON, "CxlRejReason", T.INT,
                 labels=enum_labels(CxlRejReason)),
        FieldDef(ORD_REJ_REASON, "OrdRejReason", T.INT,
                 labels=enum_labels(OrdRejReason)),
        FieldDef(CLIENT_ID, "ClientID", T.STRING, max_length=9),
        FieldDef(MIN_QTY, "MinQty", T.QTY, max_digits=9),
        FieldDef(EXEC_TYPE, "ExecType", T.CHAR, labels=enum_labels(ExecType)),
        FieldDef(LEAVES_QTY, "LeavesQty", T.QTY, max_digits=9),
        FieldDef(TRADING_SESSION_ID, "TradingSessionID", T.STRING, max_length=16),
        FieldDef(TRAD_SES_MODE, "TradSesMode", T.INT,
                 labels=enum_labels(TradSesMode)),
        FieldDef(TRAD_SES_STATUS, "TradSesStatus", T.INT,
                 labels=enum_labels(TradSesStatus)),
        FieldDef(EXEC_RESTATEMENT_REASON, "ExecRestatementReason", T.INT,
                 labels=enum_labels(ExecRestatementReason)),
        FieldDef(C.BUSINESS_REJECT_REF_ID, "BusinessRejectRefID", T.STRING,
                 max_length=32),
        FieldDef(C.BUSINESS_REJECT_REASON, "BusinessRejectReason", T.INT,
                 labels=enum_labels(C.BusinessRejectReason)),
        FieldDef(CXL_REJ_RESPONSE_TO, "CxlRejResponseTo", T.CHAR,
                 values=(CxlRejResponseTo.CANCEL_REQUEST,
                         CxlRejResponseTo.CANCEL_REPLACE_REQUEST),
                 labels=enum_labels(CxlRejResponseTo)),
        FieldDef(CASH_MARGIN, "CashMargin", T.CHAR,
                 values=(CashMargin.CASH, CashMargin.MARGIN_OPEN,
                         CashMargin.MARGIN_CLOSE),
                 labels=enum_labels(CashMargin)),
        FieldDef(LAST_LIQUIDITY_IND, "LastLiquidityInd", T.INT,
                 labels=enum_labels(LastLiquidityInd)),
        FieldDef(TRD_MATCH_ID, "TrdMatchID", T.STRING, max_length=32),
        FieldDef(MARGIN_TRANSACTION_TYPE, "MarginTransactionType", T.CHAR,
                 values=(MarginTransactionType.NEGOTIABLE,
                         MarginTransactionType.STANDARDIZED),
                 labels=enum_labels(MarginTransactionType)),
    ]


def application_messages():
    """The three inbound application messages, plus the outbound set.

    Requirements follow the specification's Reqd column, where both ``Y`` and
    ``R`` mean the field must be present -- ``R`` marking those Japannext
    requires that standard FIX 4.2 does not.
    """
    return [
        MessageDef(
            C.NEW_ORDER_SINGLE, "NewOrderSingle",
            required=(CL_ORD_ID, ORDER_QTY, ORD_TYPE, PRICE, SIDE, SYMBOL,
                      TRANSACT_TIME),
            optional=(ACCOUNT, EXEC_INST, HANDL_INST, RULE_80A, TIME_IN_FORCE,
                      CLIENT_ID, MIN_QTY, CASH_MARGIN, MARGIN_TRANSACTION_TYPE),
            inbound=True),

        MessageDef(
            C.ORDER_CANCEL_REQUEST, "OrderCancelRequest",
            required=(CL_ORD_ID, ORDER_QTY, ORIG_CL_ORD_ID, SIDE, SYMBOL,
                      TRANSACT_TIME),
            inbound=True),

        MessageDef(
            C.ORDER_CANCEL_REPLACE_REQUEST, "OrderCancelReplaceRequest",
            required=(CL_ORD_ID, ORDER_QTY, ORD_TYPE, ORIG_CL_ORD_ID, PRICE,
                      SIDE, SYMBOL, TRANSACT_TIME),
            optional=(EXEC_INST, HANDL_INST, RULE_80A, TIME_IN_FORCE, MIN_QTY),
            inbound=True),

        # Outbound only: defined so the dialect is complete and so that a
        # client sending one is refused with "cannot be sent to the venue".
        MessageDef(
            C.EXECUTION_REPORT, "ExecutionReport",
            optional=(ACCOUNT, AVG_PX, CL_ORD_ID, CUM_QTY, EXEC_ID,
                      EXEC_TRANS_TYPE, LAST_PX, LAST_SHARES, ORDER_ID,
                      ORDER_QTY, ORD_STATUS, ORD_TYPE, ORIG_CL_ORD_ID, PRICE,
                      RULE_80A, SIDE, SYMBOL, C.TEXT, TIME_IN_FORCE,
                      TRANSACT_TIME, ORD_REJ_REASON, CLIENT_ID, MIN_QTY,
                      EXEC_TYPE, LEAVES_QTY, EXEC_RESTATEMENT_REASON,
                      CASH_MARGIN, LAST_LIQUIDITY_IND, TRD_MATCH_ID,
                      MARGIN_TRANSACTION_TYPE),
            inbound=False),

        MessageDef(
            C.ORDER_CANCEL_REJECT, "OrderCancelReject",
            optional=(CL_ORD_ID, ORDER_ID, ORD_STATUS, ORIG_CL_ORD_ID, C.TEXT,
                      CXL_REJ_REASON, CXL_REJ_RESPONSE_TO),
            inbound=False),

        MessageDef(
            C.BUSINESS_MESSAGE_REJECT, "BusinessMessageReject",
            optional=(C.REF_SEQ_NUM, C.TEXT, C.REF_MSG_TYPE,
                      C.BUSINESS_REJECT_REF_ID, C.BUSINESS_REJECT_REASON),
            inbound=False),

        MessageDef(
            C.TRADING_SESSION_STATUS, "TradingSessionStatus",
            optional=(TRADING_SESSION_ID, TRAD_SES_MODE, TRAD_SES_STATUS),
            inbound=False),
    ]


def build():
    """The complete Japannext dialect: session layer plus application messages."""
    return build_session_dictionary(
        begin_string="FIX.4.2",
        fields=application_fields(),
        messages=application_messages(),
        sub_id_labels=SubID.LABELS)
