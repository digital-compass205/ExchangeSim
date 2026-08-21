"""The HKEX OCG-C dialect: FIX 5.0 SP2 semantics over a FIXT.1.1 session.

Transcribed from HKEX_OCGC_FIX_Trading_Protocol_3.12 (19 July 2023). Section 7
lists every message and its fields; the enumerations below are the
specification's own, so a client sending a value the real venue would refuse is
refused here too.

Four things differ structurally from a FIX 4.2 dialect such as Japannext's, and
between them they are why this venue needed session-layer work rather than only
a new module:

* ``BeginString`` is ``FIXT.1.1``. The application version travels in
  ``DefaultApplVerID(1137)`` on Logon and ``ApplVerID(1128)`` per message.
* Logon requires ``NextExpectedMsgSeqNum(789)`` -- sequence negotiation rather
  than Japannext's pure ResendRequest recovery -- and ``EncryptedPassword(1402)``.
* Instruments are named by ``SecurityID(48)`` with ``SecurityIDSource(22)=8``,
  not by ``Symbol(55)``; ``Symbol`` does not appear in this dialect at all.
* Repeating groups are used in earnest: every business message carries a
  ``<Parties>`` block and the order messages a ``<DisclosureInstructionGrp>``.
  The codec keeps fields in order and offers ``get_all``, so the groups are read
  positionally rather than needing a group model.

There are no SubID header tags: HKEX addresses one market per session and the
market *segment* an order lands on is a property of the security, not of the
message. :class:`~exchangesim.venues.hkex.handlers.HkexApplication` resolves it
from reference data.
"""

from ...fix import constants as C
from ...fix.dictionary import FieldDef, MessageDef, enum_labels
from ...fix.standard import build_session_dictionary

T = C.FieldType

# -- message types -----------------------------------------------------------

ORDER_MASS_CANCEL_REQUEST = "q"
ORDER_MASS_CANCEL_REPORT = "r"
PARTY_ENTITLEMENT_REQUEST = "CU"
PARTY_ENTITLEMENT_REPORT = "CV"

#: Fractional places OCG-C timestamps carry: microseconds, not milliseconds.
TIMESTAMP_DECIMALS = 6

# -- tag numbers -------------------------------------------------------------

CL_ORD_ID = 11
CUM_QTY = 14
EXEC_ID = 17
EXEC_INST = 18
SECURITY_ID_SOURCE = 22
LAST_PX = 31
LAST_QTY = 32
ORDER_ID = 37
ORDER_QTY = 38
ORD_STATUS = 39
ORD_TYPE = 40
ORIG_CL_ORD_ID = 41
PRICE = 44
SECURITY_ID = 48
SIDE = 54
TIME_IN_FORCE = 59
TRANSACT_TIME = 60
POSITION_EFFECT = 77
CXL_REJ_REASON = 102
ORD_REJ_REASON = 103
EXEC_TYPE = 150
LEAVES_QTY = 151
SECURITY_EXCHANGE = 207
EXEC_RESTATEMENT_REASON = 378
CXL_REJ_RESPONSE_TO = 434
PARTY_ID_SOURCE = 447
PARTY_ID = 448
PARTY_ROLE = 452
NO_PARTY_IDS = 453
ORDER_CAPACITY = 528
ORDER_RESTRICTIONS = 529
MASS_CANCEL_REQUEST_TYPE = 530
MASS_CANCEL_RESPONSE = 531
MASS_CANCEL_REJECT_REASON = 532
MATCH_TYPE = 574
TRD_MATCH_ID = 880
AGGRESSOR_INDICATOR = 1057
MAX_PRICE_LEVELS = 1090
LOT_TYPE = 1093
ORDER_CATEGORY = 1115
MARKET_SEGMENT_ID = 1300
REJECT_TEXT = 1328
MASS_ACTION_REPORT_ID = 1369
NO_DISCLOSURE_INSTRUCTIONS = 1812
DISCLOSURE_TYPE = 1813
DISCLOSURE_INSTRUCTION = 1814
SELF_MATCH_PREVENTION_ID = 2362

# -- party entitlements, section 7.10 ----------------------------------------
#
# The two encodings arrange these differently -- FIX wraps the broker in
# <PartyEntitlementGrp><PartyDetailGrp>, binary carries it as a flat Broker ID
# and hangs the entitlements straight off it -- but they are the same fields,
# so the tags are the FIX ones in both.

LAST_FRAGMENT = 893
REQUEST_RESULT = 1511
TOT_NO_PARTY_LIST = 1512
NO_PARTY_DETAILS = 1671
PARTY_DETAIL_ID = 1691
PARTY_DETAIL_ID_SOURCE = 1692
PARTY_DETAIL_ROLE = 1693
ENTITLEMENTS_REQUEST_ID = 1770
ENTITLEMENTS_REPORT_ID = 1771
NO_PARTY_ENTITLEMENTS = 1772
NO_ENTITLEMENTS = 1773
ENTITLEMENT_INDICATOR = 1774
ENTITLEMENT_TYPE = 1775
ENTITLEMENT_ID = 1776
NO_ENTITLEMENT_ATTRIB = 1777
ENTITLEMENT_ATTRIB_TYPE = 1778
ENTITLEMENT_ATTRIB_DATA_TYPE = 1779
ENTITLEMENT_ATTRIB_VALUE = 1780
NO_INSTRUMENT_SCOPES = 1656
INSTRUMENT_SCOPE_OPERATOR = 1535
INSTRUMENT_SCOPE_SECURITY_ID = 1538
INSTRUMENT_SCOPE_SECURITY_ID_SOURCE = 1539
INSTRUMENT_SCOPE_SECURITY_EXCHANGE = 1616


# -- enumerations, exactly as the specification lists them -------------------

class OrdType(object):
    MARKET = "1"
    LIMIT = "2"


class TimeInForce(object):
    DAY = "0"
    IOC = "3"
    FOK = "4"
    AT_CROSSING = "9"        # auction sessions only; see ASSUMPTIONS


class SideValue(object):
    BUY = "1"
    SELL = "2"
    SELL_SHORT = "5"


class ExecInstValue(object):
    IGNORE_PRICE_CHECKS = "c"
    IGNORE_NOTIONAL = "x"


class OrdStatus(object):
    NEW = "0"
    PARTIALLY_FILLED = "1"
    FILLED = "2"
    CANCELED = "4"
    PENDING_CANCEL = "6"
    REJECTED = "8"
    PENDING_NEW = "A"
    PENDING_REPLACE = "E"
    EXPIRED = "C"


class ExecType(object):
    NEW = "0"
    CANCELED = "4"
    REPLACED = "5"
    REJECTED = "8"
    EXPIRED = "C"
    TRADE = "F"              # FIX 5.0 spells a fill 'F', not '1'/'2'
    TRADE_CANCEL = "H"

    # The binary encoding has no OrderCancelReject: it reports a refused cancel
    # or amend as an Execution Report carrying one of these two. They never
    # appear on a FIX session, where 35=9 says the same thing.
    CANCEL_REJECT = "X"
    AMEND_REJECT = "Y"


class OrdRejReason(object):
    EXCEEDS_LIMIT = 3
    DUPLICATE_ORDER = 6
    INCORRECT_QUANTITY = 13
    PRICE_EXCEEDS_BAND = 16
    REFERENCE_PRICE_UNAVAILABLE = 19
    NOTIONAL_EXCEEDS_THRESHOLD = 20
    OTHER = 99
    PRICE_EXCEEDS_BAND_NO_OVERRIDE = 101
    PRICE_EXCEEDS_BAND_OVERRIDE = 102


class CxlRejReason(object):
    TOO_LATE_TO_CANCEL = 0
    UNKNOWN_ORDER = 1
    ALREADY_PENDING = 3
    DUPLICATE_CL_ORD_ID = 6
    PRICE_EXCEEDS_BAND = 8
    OTHER = 99


class CxlRejResponseTo(object):
    CANCEL_REQUEST = "1"
    CANCEL_REPLACE_REQUEST = "2"


class ExecRestatementReason(object):
    """``ExecRestatementReason(378)``, section 7.7.7.4.

    Note that HKEX distinguishes the *two* sides of a self-match prevention
    cancel, which Japannext does not: the aggressive and the passive order carry
    different reasons.
    """

    CANCEL_ON_TRADING_HALT = 6
    MARKET_OPERATION = 8
    SMP_CANCEL_AGGRESSIVE = 17
    SMP_CANCEL_PASSIVE = 18
    UNSOLICITED_CANCEL_OF_ORIGINAL = 100
    MASS_CANCELLED_BY_BROKER = 103
    CANCEL_ON_DISCONNECT = 104
    BROKER_SUSPENSION = 105
    EXCHANGE_PARTICIPANT_SUSPENSION = 106


class OrderCapacity(object):
    AGENCY = "A"
    PRINCIPAL = "P"


class OrderRestrictions(object):
    INDEX_ARBITRAGE = "2"
    MARKET_MAKER = "5"
    MARKET_MAKER_UNDERLYING = "6"


class PositionEffect(object):
    CLOSE = "C"


class PartyIDSource(object):
    PROPRIETARY = "D"


class PartyRole(object):
    EXECUTING_FIRM = "1"     # Broker Number
    CONTRA_FIRM = "17"       # counterparty Broker Number, on a trade
    CLIENT_ID = "3"          # BCAN
    ENTERING_TRADER = "36"   # submitting broker, on an OBO request
    LOCATION_ID = "75"       # BS User ID


class DisclosureType(object):
    NONE = "100"


class DisclosureInstruction(object):
    YES = "1"


class LotType(object):
    ODD_LOT = "1"
    ROUND_LOT = "2"


class MatchType(object):
    AUTO_MATCH = "4"
    CROSS_AUCTION = "5"


class OrderCategory(object):
    INTERNAL_CROSS = "A"


class MassCancelRequestType(object):
    SECURITY = "1"
    ALL = "7"
    MARKET_SEGMENT = "9"


class MassCancelResponse(object):
    REJECTED = "0"
    SECURITY = "1"
    ALL = "7"
    MARKET_SEGMENT = "9"


class MassCancelRejectReason(object):
    INVALID_MARKET_SEGMENT = 8
    OTHER = 99


class RequestResult(object):
    """``RequestResult(1511)`` on a Party Entitlement Report."""

    VALID = "0"
    INVALID_OR_UNSUPPORTED = "1"
    NO_DATA_FOUND = "2"
    NOT_AUTHORIZED = "3"
    TEMPORARILY_UNAVAILABLE = "4"
    NOT_SUPPORTED = "5"
    OTHER = "99"


class EntitlementType(object):
    """``EntitlementType(1775)``. A simulator grants the first and not the
    second: market making brings quote obligations, and quotes are not built."""

    TRADE = "0"
    MAKE_MARKETS = "1"


class PartyDetailRole(object):
    """``PartyDetailRole(1693)`` -- the roles an entitlement report names."""

    EXECUTING_FIRM = "1"
    LIQUIDITY_PROVIDER = "35"


class SecurityIDSource(object):
    EXCHANGE_SYMBOL = "8"


#: ``SecurityExchange(207)``; the securities market's MIC.
SECURITY_EXCHANGE_VALUE = "XHKG"


class MarketSegment(object):
    """``MarketSegmentID(1300)`` -- and therefore this venue's market names.

    Unlike Japannext, where the client picks the market with ``TargetSubID(57)``,
    a security belongs to exactly one segment and the message never names it.
    """

    MAIN = "MAIN"
    GEM = "GEM"
    NASD = "NASD"
    ETS = "ETS"

    ALL = (MAIN, GEM, NASD, ETS)

    LABELS = {
        MAIN: "Main Board",
        GEM: "GEM",
        NASD: "Nasdaq-Amex Pilot",
        ETS: "Extended Trading Securities",
    }


#: Tags legal in any message. FIXT.1.1 adds ApplVerID; HKEX has no SubIDs.
HEADER_TAGS = (
    C.MSG_TYPE, C.MSG_SEQ_NUM, C.SENDER_COMP_ID, C.SENDING_TIME,
    C.TARGET_COMP_ID, C.POSS_DUP_FLAG, C.POSS_RESEND, C.ORIG_SENDING_TIME,
    C.APPL_VER_ID,
)

#: What the binary header carries, section 7.2 -- and what it does not. There
#: is no SendingTime, no OrigSendingTime and no ApplVerID: the encoding is not
#: FIXT, so there is no session-layer version to name. TargetCompID is here
#: because the codec fills the venue's own identity in; the wire has one Comp
#: ID, the client's.
BINARY_HEADER_TAGS = (
    C.MSG_TYPE, C.MSG_SEQ_NUM, C.SENDER_COMP_ID, C.TARGET_COMP_ID,
    C.POSS_DUP_FLAG, C.POSS_RESEND,
)

#: A binary session states no BeginString, so this is an identity rather than a
#: version -- it keeps the two encodings' session keys and stores apart.
BINARY_BEGIN_STRING = "OCGC.BINARY"


# -- field definitions -------------------------------------------------------

def session_extensions():
    """Fields FIXT.1.1 adds to the session layer, plus HKEX's password fields."""
    return [
        FieldDef(C.NEXT_EXPECTED_MSG_SEQ_NUM, "NextExpectedMsgSeqNum", T.INT),
        FieldDef(C.APPL_VER_ID, "ApplVerID", T.STRING, max_length=6,
                 values=(C.ApplVerID.FIX50SP2,),
                 labels=enum_labels(C.ApplVerID)),
        FieldDef(C.DEFAULT_APPL_VER_ID, "DefaultApplVerID", T.STRING,
                 max_length=6, values=(C.ApplVerID.FIX50SP2,),
                 labels=enum_labels(C.ApplVerID)),
        FieldDef(C.ENCRYPTED_PASSWORD_METHOD, "EncryptedPasswordMethod", T.INT,
                 values=("101",)),
        # The simulator cannot decrypt these -- it holds no private key -- so
        # they are checked for presence and shape only. See ASSUMPTIONS.
        # redact: they are still a client's credential, and the audit view would
        # otherwise put every Logon's password on a web page.
        FieldDef(C.ENCRYPTED_PASSWORD, "EncryptedPassword", T.STRING,
                 max_length=1024, redact=True),
        FieldDef(C.ENCRYPTED_NEW_PASSWORD, "EncryptedNewPassword", T.STRING,
                 max_length=1024, redact=True),
        FieldDef(C.SESSION_STATUS, "SessionStatus", T.INT,
                 labels=enum_labels(C.SessionStatus)),
        FieldDef(C.TEST_MESSAGE_INDICATOR, "TestMessageIndicator", T.BOOLEAN),
    ]


def application_fields():
    """Every application-level field the dialect defines."""
    return [
        FieldDef(CL_ORD_ID, "ClOrdID", T.STRING, max_length=20),
        FieldDef(CUM_QTY, "CumQty", T.QTY, max_digits=16),
        FieldDef(EXEC_ID, "ExecID", T.STRING, max_length=20),
        FieldDef(EXEC_INST, "ExecInst", T.MULTI_VALUE_STRING,
                 values=(ExecInstValue.IGNORE_PRICE_CHECKS,
                         ExecInstValue.IGNORE_NOTIONAL),
                 labels=enum_labels(ExecInstValue)),
        FieldDef(SECURITY_ID_SOURCE, "SecurityIDSource", T.STRING, max_length=2,
                 values=(SecurityIDSource.EXCHANGE_SYMBOL,),
                 labels=enum_labels(SecurityIDSource)),
        FieldDef(LAST_PX, "LastPx", T.PRICE, max_digits=8, max_decimals=3),
        FieldDef(LAST_QTY, "LastQty", T.QTY, max_digits=16),
        FieldDef(ORDER_ID, "OrderID", T.STRING, max_length=20),
        FieldDef(ORDER_QTY, "OrderQty", T.QTY, max_digits=16),
        FieldDef(ORD_STATUS, "OrdStatus", T.CHAR,
                 values=(OrdStatus.NEW, OrdStatus.PARTIALLY_FILLED,
                         OrdStatus.FILLED, OrdStatus.CANCELED,
                         OrdStatus.PENDING_CANCEL, OrdStatus.REJECTED,
                         OrdStatus.PENDING_NEW, OrdStatus.PENDING_REPLACE,
                         OrdStatus.EXPIRED),
                 labels=enum_labels(OrdStatus)),
        FieldDef(ORD_TYPE, "OrdType", T.CHAR,
                 values=(OrdType.MARKET, OrdType.LIMIT),
                 labels=enum_labels(OrdType)),
        FieldDef(ORIG_CL_ORD_ID, "OrigClOrdID", T.STRING, max_length=20),
        FieldDef(PRICE, "Price", T.PRICE, max_digits=8, max_decimals=3),
        FieldDef(SECURITY_ID, "SecurityID", T.STRING, max_length=12),
        FieldDef(SIDE, "Side", T.CHAR,
                 values=(SideValue.BUY, SideValue.SELL, SideValue.SELL_SHORT),
                 labels=enum_labels(SideValue)),
        FieldDef(TIME_IN_FORCE, "TimeInForce", T.CHAR,
                 values=(TimeInForce.DAY, TimeInForce.IOC, TimeInForce.FOK,
                         TimeInForce.AT_CROSSING),
                 labels=enum_labels(TimeInForce)),
        # Section 8: YYYYMMDD-HH:MM:SS.ssssss, in both encodings. A client
        # sending microseconds is sending what its specification asks for.
        FieldDef(TRANSACT_TIME, "TransactTime", T.UTC_TIMESTAMP,
                 max_decimals=TIMESTAMP_DECIMALS),
        FieldDef(POSITION_EFFECT, "PositionEffect", T.CHAR,
                 values=(PositionEffect.CLOSE,),
                 labels=enum_labels(PositionEffect)),
        FieldDef(CXL_REJ_REASON, "CxlRejReason", T.INT,
                 labels=enum_labels(CxlRejReason)),
        FieldDef(ORD_REJ_REASON, "OrdRejReason", T.INT,
                 labels=enum_labels(OrdRejReason)),
        FieldDef(EXEC_TYPE, "ExecType", T.CHAR, labels=enum_labels(ExecType)),
        FieldDef(LEAVES_QTY, "LeavesQty", T.QTY, max_digits=16),
        FieldDef(SECURITY_EXCHANGE, "SecurityExchange", T.STRING, max_length=8,
                 values=(SECURITY_EXCHANGE_VALUE,)),
        FieldDef(EXEC_RESTATEMENT_REASON, "ExecRestatementReason", T.INT,
                 labels=enum_labels(ExecRestatementReason)),
        FieldDef(CXL_REJ_RESPONSE_TO, "CxlRejResponseTo", T.CHAR,
                 values=(CxlRejResponseTo.CANCEL_REQUEST,
                         CxlRejResponseTo.CANCEL_REPLACE_REQUEST),
                 labels=enum_labels(CxlRejResponseTo)),
        FieldDef(PARTY_ID_SOURCE, "PartyIDSource", T.CHAR,
                 values=(PartyIDSource.PROPRIETARY,),
                 labels=enum_labels(PartyIDSource)),
        FieldDef(PARTY_ID, "PartyID", T.STRING, max_length=20),
        FieldDef(PARTY_ROLE, "PartyRole", T.INT,
                 values=(PartyRole.EXECUTING_FIRM, PartyRole.CLIENT_ID,
                         PartyRole.CONTRA_FIRM, PartyRole.ENTERING_TRADER,
                         PartyRole.LOCATION_ID),
                 labels=enum_labels(PartyRole)),
        FieldDef(NO_PARTY_IDS, "NoPartyIDs", T.INT),
        FieldDef(ORDER_CAPACITY, "OrderCapacity", T.CHAR,
                 values=(OrderCapacity.AGENCY, OrderCapacity.PRINCIPAL),
                 labels=enum_labels(OrderCapacity)),
        FieldDef(ORDER_RESTRICTIONS, "OrderRestrictions", T.MULTI_VALUE_STRING,
                 values=(OrderRestrictions.INDEX_ARBITRAGE,
                         OrderRestrictions.MARKET_MAKER,
                         OrderRestrictions.MARKET_MAKER_UNDERLYING),
                 labels=enum_labels(OrderRestrictions)),
        FieldDef(MASS_CANCEL_REQUEST_TYPE, "MassCancelRequestType", T.CHAR,
                 values=(MassCancelRequestType.SECURITY,
                         MassCancelRequestType.ALL,
                         MassCancelRequestType.MARKET_SEGMENT),
                 labels=enum_labels(MassCancelRequestType)),
        FieldDef(MASS_CANCEL_RESPONSE, "MassCancelResponse", T.CHAR,
                 labels=enum_labels(MassCancelResponse)),
        FieldDef(MASS_CANCEL_REJECT_REASON, "MassCancelRejectReason", T.INT,
                 labels=enum_labels(MassCancelRejectReason)),
        FieldDef(MATCH_TYPE, "MatchType", T.STRING, max_length=2,
                 labels=enum_labels(MatchType)),
        FieldDef(TRD_MATCH_ID, "TrdMatchID", T.STRING, max_length=20),
        FieldDef(AGGRESSOR_INDICATOR, "AggressorIndicator", T.BOOLEAN),
        FieldDef(MAX_PRICE_LEVELS, "MaxPriceLevels", T.INT),
        FieldDef(LOT_TYPE, "LotType", T.CHAR,
                 values=(LotType.ODD_LOT, LotType.ROUND_LOT),
                 labels=enum_labels(LotType)),
        FieldDef(ORDER_CATEGORY, "OrderCategory", T.CHAR,
                 values=(OrderCategory.INTERNAL_CROSS,),
                 labels=enum_labels(OrderCategory)),
        # MarketSegment already carries a value -> label map: these are market
        # names, not constant names, so it is used as it stands.
        FieldDef(MARKET_SEGMENT_ID, "MarketSegmentID", T.STRING, max_length=8,
                 values=MarketSegment.ALL, labels=MarketSegment.LABELS),
        FieldDef(REJECT_TEXT, "RejectText", T.STRING, max_length=255),
        FieldDef(MASS_ACTION_REPORT_ID, "MassActionReportID", T.STRING,
                 max_length=20),
        FieldDef(NO_DISCLOSURE_INSTRUCTIONS, "NoDisclosureInstructions", T.INT),
        FieldDef(DISCLOSURE_TYPE, "DisclosureType", T.INT,
                 values=(DisclosureType.NONE,),
                 labels=enum_labels(DisclosureType)),
        FieldDef(DISCLOSURE_INSTRUCTION, "DisclosureInstruction", T.CHAR,
                 values=(DisclosureInstruction.YES,),
                 labels=enum_labels(DisclosureInstruction)),
        FieldDef(SELF_MATCH_PREVENTION_ID, "SelfMatchPreventionID", T.STRING,
                 max_length=20),
        FieldDef(C.BUSINESS_REJECT_REF_ID, "BusinessRejectRefID", T.STRING,
                 max_length=20),
        FieldDef(C.BUSINESS_REJECT_REASON, "BusinessRejectReason", T.INT,
                 labels=enum_labels(C.BusinessRejectReason)),

        # -- party entitlements, section 7.10 --
        FieldDef(ENTITLEMENTS_REQUEST_ID, "EntitlementsRequestID", T.STRING,
                 max_length=20),
        FieldDef(ENTITLEMENTS_REPORT_ID, "EntitlementsReportID", T.STRING,
                 max_length=20),
        FieldDef(REQUEST_RESULT, "RequestResult", T.INT,
                 labels=enum_labels(RequestResult)),
        FieldDef(TOT_NO_PARTY_LIST, "TotNoPartyList", T.INT),
        FieldDef(LAST_FRAGMENT, "LastFragment", T.BOOLEAN),
        FieldDef(NO_PARTY_ENTITLEMENTS, "NoPartyEntitlements", T.INT),
        FieldDef(NO_PARTY_DETAILS, "NoPartyDetails", T.INT),
        FieldDef(PARTY_DETAIL_ID, "PartyDetailID", T.STRING, max_length=11),
        FieldDef(PARTY_DETAIL_ID_SOURCE, "PartyDetailIDSource", T.CHAR,
                 values=(PartyIDSource.PROPRIETARY,),
                 labels=enum_labels(PartyIDSource)),
        FieldDef(PARTY_DETAIL_ROLE, "PartyDetailRole", T.INT,
                 values=(PartyDetailRole.EXECUTING_FIRM,
                         PartyDetailRole.LIQUIDITY_PROVIDER),
                 labels=enum_labels(PartyDetailRole)),
        FieldDef(NO_ENTITLEMENTS, "NoEntitlements", T.INT),
        FieldDef(ENTITLEMENT_INDICATOR, "EntitlementIndicator", T.BOOLEAN),
        FieldDef(ENTITLEMENT_TYPE, "EntitlementType", T.INT,
                 values=(EntitlementType.TRADE, EntitlementType.MAKE_MARKETS),
                 labels=enum_labels(EntitlementType)),
        FieldDef(ENTITLEMENT_ID, "EntitlementID", T.STRING, max_length=20),
        FieldDef(NO_ENTITLEMENT_ATTRIB, "NoEntitlementAttrib", T.INT),
        FieldDef(ENTITLEMENT_ATTRIB_TYPE, "EntitlementAttribType", T.INT),
        FieldDef(ENTITLEMENT_ATTRIB_DATA_TYPE, "EntitlementAttribDataType",
                 T.INT),
        FieldDef(ENTITLEMENT_ATTRIB_VALUE, "EntitlementAttribValue", T.STRING,
                 max_length=20),
        FieldDef(NO_INSTRUMENT_SCOPES, "NoInstrumentScopes", T.INT),
        FieldDef(INSTRUMENT_SCOPE_OPERATOR, "InstrumentScopeOperator", T.INT),
        FieldDef(INSTRUMENT_SCOPE_SECURITY_ID, "InstrumentScopeSecurityID",
                 T.STRING, max_length=12),
        FieldDef(INSTRUMENT_SCOPE_SECURITY_ID_SOURCE,
                 "InstrumentScopeSecurityIDSource", T.STRING, max_length=2),
        FieldDef(INSTRUMENT_SCOPE_SECURITY_EXCHANGE,
                 "InstrumentScopeSecurityExchange", T.STRING, max_length=8),
    ]


#: The <Parties> block, present on every business message.
_PARTIES = (NO_PARTY_IDS, PARTY_ID, PARTY_ID_SOURCE, PARTY_ROLE)

#: The <Instrument> block.
_INSTRUMENT = (SECURITY_ID, SECURITY_ID_SOURCE, SECURITY_EXCHANGE)

#: The <DisclosureInstructionGrp> block.
_DISCLOSURE = (NO_DISCLOSURE_INSTRUCTIONS, DISCLOSURE_TYPE,
               DISCLOSURE_INSTRUCTION)


#: The <PartyEntitlementGrp> and everything under it. One tuple, because both
#: dialects accept exactly these tags on a report -- what differs is how each
#: encoding lays them out, which is the codec's business, not the dictionary's.
_ENTITLEMENT_GROUPS = (
    NO_PARTY_ENTITLEMENTS, NO_PARTY_DETAILS, PARTY_DETAIL_ID,
    PARTY_DETAIL_ID_SOURCE, PARTY_DETAIL_ROLE, NO_ENTITLEMENTS,
    ENTITLEMENT_INDICATOR, ENTITLEMENT_TYPE, ENTITLEMENT_ID,
    NO_ENTITLEMENT_ATTRIB, ENTITLEMENT_ATTRIB_TYPE,
    ENTITLEMENT_ATTRIB_DATA_TYPE, ENTITLEMENT_ATTRIB_VALUE,
    NO_INSTRUMENT_SCOPES, INSTRUMENT_SCOPE_OPERATOR,
    INSTRUMENT_SCOPE_SECURITY_ID, INSTRUMENT_SCOPE_SECURITY_ID_SOURCE,
    INSTRUMENT_SCOPE_SECURITY_EXCHANGE,
)


def entitlement_messages():
    """Party Entitlement Request and Report, section 7.10.

    A client asks what its Broker IDs may do, and the venue answers one report
    per Broker ID, the last carrying LastFragment. Both encodings define both
    messages, so this pair is shared.
    """
    return [
        MessageDef(
            PARTY_ENTITLEMENT_REQUEST, "PartyEntitlementRequest",
            required=(ENTITLEMENTS_REQUEST_ID,),
            inbound=True),

        MessageDef(
            PARTY_ENTITLEMENT_REPORT, "PartyEntitlementReport",
            optional=((ENTITLEMENTS_REPORT_ID, ENTITLEMENTS_REQUEST_ID,
                       REQUEST_RESULT, TOT_NO_PARTY_LIST, LAST_FRAGMENT)
                      + _ENTITLEMENT_GROUPS),
            inbound=False),
    ]


def session_messages():
    """Administrative messages whose shape FIXT.1.1 and HKEX change.

    These replace the FIX 4.2 definitions by MsgType when the dictionary is
    composed. Only Logon and Logout differ; the rest are unchanged.
    """
    return [
        MessageDef(
            C.LOGON, "Logon",
            required=(C.ENCRYPT_METHOD, C.HEART_BT_INT,
                      C.NEXT_EXPECTED_MSG_SEQ_NUM, C.DEFAULT_APPL_VER_ID),
            optional=(C.ENCRYPTED_PASSWORD_METHOD, C.ENCRYPTED_PASSWORD,
                      C.ENCRYPTED_NEW_PASSWORD, C.SESSION_STATUS,
                      C.TEST_MESSAGE_INDICATOR, C.TEXT, C.RESET_SEQ_NUM_FLAG)),

        MessageDef(
            C.LOGOUT, "Logout",
            optional=(C.SESSION_STATUS, C.TEXT)),
    ]


def application_messages():
    """The inbound business messages, plus the outbound set."""
    return [
        MessageDef(
            C.NEW_ORDER_SINGLE, "NewOrderSingle",
            required=((CL_ORD_ID, ORD_TYPE, SIDE, ORDER_QTY, TRANSACT_TIME)
                      + _PARTIES + _INSTRUMENT + _DISCLOSURE),
            optional=(EXEC_INST, C.TEXT, TIME_IN_FORCE, PRICE, POSITION_EFFECT,
                      ORDER_CAPACITY, ORDER_RESTRICTIONS, MAX_PRICE_LEVELS,
                      SELF_MATCH_PREVENTION_ID, LOT_TYPE),
            inbound=True),

        MessageDef(
            C.ORDER_CANCEL_REQUEST, "OrderCancelRequest",
            required=((CL_ORD_ID, ORIG_CL_ORD_ID, ORDER_QTY, SIDE,
                       TRANSACT_TIME) + _PARTIES + _INSTRUMENT),
            optional=(ORDER_ID, C.TEXT),
            inbound=True),

        MessageDef(
            C.ORDER_CANCEL_REPLACE_REQUEST, "OrderCancelReplaceRequest",
            required=((CL_ORD_ID, ORIG_CL_ORD_ID, ORD_TYPE, SIDE, ORDER_QTY,
                       TRANSACT_TIME) + _PARTIES + _INSTRUMENT + _DISCLOSURE),
            optional=(ORDER_ID, EXEC_INST, C.TEXT, TIME_IN_FORCE, PRICE,
                      POSITION_EFFECT, ORDER_CAPACITY, ORDER_RESTRICTIONS,
                      MAX_PRICE_LEVELS),
            inbound=True),

        MessageDef(
            ORDER_MASS_CANCEL_REQUEST, "OrderMassCancelRequest",
            required=((CL_ORD_ID, MASS_CANCEL_REQUEST_TYPE, TRANSACT_TIME)
                      + _PARTIES),
            optional=_INSTRUMENT + (SIDE, MARKET_SEGMENT_ID),
            inbound=True),

        # Outbound only: defined so the dialect is complete and so a client
        # sending one is refused with "cannot be sent to the venue".
        MessageDef(
            C.EXECUTION_REPORT, "ExecutionReport",
            optional=((CL_ORD_ID, ORIG_CL_ORD_ID, ORDER_ID, EXEC_ID,
                       TRD_MATCH_ID, ORD_TYPE, TIME_IN_FORCE, SIDE, ORDER_QTY,
                       PRICE, TRANSACT_TIME, ORDER_CAPACITY, ORDER_RESTRICTIONS,
                       MAX_PRICE_LEVELS, SELF_MATCH_PREVENTION_ID,
                       POSITION_EFFECT, ORD_STATUS, EXEC_TYPE, LAST_PX,
                       LAST_QTY, CUM_QTY, LEAVES_QTY, MATCH_TYPE,
                       ORDER_CATEGORY, AGGRESSOR_INDICATOR, LOT_TYPE,
                       ORD_REJ_REASON, EXEC_RESTATEMENT_REASON, REJECT_TEXT,
                       C.TEXT) + _PARTIES + _INSTRUMENT),
            inbound=False),

        MessageDef(
            C.ORDER_CANCEL_REJECT, "OrderCancelReject",
            optional=((CL_ORD_ID, ORIG_CL_ORD_ID, ORDER_ID, TRANSACT_TIME,
                       ORD_STATUS, CXL_REJ_RESPONSE_TO, CXL_REJ_REASON,
                       REJECT_TEXT, C.TEXT) + _PARTIES),
            inbound=False),

        MessageDef(
            ORDER_MASS_CANCEL_REPORT, "OrderMassCancelReport",
            optional=((CL_ORD_ID, MASS_ACTION_REPORT_ID,
                       MASS_CANCEL_REQUEST_TYPE, MASS_CANCEL_RESPONSE,
                       MASS_CANCEL_REJECT_REASON, TRANSACT_TIME, C.TEXT)
                      + _PARTIES + _INSTRUMENT),
            inbound=False),

        MessageDef(
            C.BUSINESS_MESSAGE_REJECT, "BusinessMessageReject",
            optional=(C.REF_SEQ_NUM, C.TEXT, C.REF_MSG_TYPE,
                      C.BUSINESS_REJECT_REF_ID, C.BUSINESS_REJECT_REASON),
            inbound=False),
    ]


def binary_session_messages():
    """The administrative messages as the *binary* specification tables them.

    HKEX publishes the same protocol twice, and the two tables differ in what
    they require. The binary Logon has no EncryptMethod, no HeartBtInt and no
    DefaultApplVerID -- the encoding fixes the first, configuration the second,
    and there is no FIXT session layer to name a version of -- while its Reject
    may carry the ClOrdID of what it refused, which the FIX one may not.
    """
    return [
        MessageDef(
            C.LOGON, "Logon",
            required=(C.NEXT_EXPECTED_MSG_SEQ_NUM, C.ENCRYPTED_PASSWORD),
            optional=(C.ENCRYPTED_NEW_PASSWORD, C.SESSION_STATUS,
                      C.TEST_MESSAGE_INDICATOR, C.TEXT)),

        MessageDef(
            C.LOGOUT, "Logout",
            optional=(C.SESSION_STATUS, C.TEXT)),

        MessageDef(
            C.REJECT, "Reject",
            required=(C.SESSION_REJECT_REASON, C.REF_SEQ_NUM),
            optional=(C.TEXT, C.REF_TAG_ID, C.REF_MSG_TYPE, CL_ORD_ID)),
    ]


def binary_application_messages():
    """The business messages as the binary specification tables them.

    Three rows differ from the FIX tables and each is a real difference, not a
    transcription slip:

    * a Cancel Request carries no ``OrderQty`` -- the FIX encoding requires one;
    * ``SecurityExchange`` is optional throughout, where FIX's ``<Instrument>``
      block requires it alongside the source;
    * an Execution Report may carry a cancel or amend reject code, because this
      encoding has no separate OrderCancelReject message to put one on.
    """
    instrument_required = (SECURITY_ID, SECURITY_ID_SOURCE)
    parties = _PARTIES
    disclosure = _DISCLOSURE

    return [
        MessageDef(
            C.NEW_ORDER_SINGLE, "NewOrderSingle",
            required=((CL_ORD_ID, ORD_TYPE, SIDE, ORDER_QTY, TRANSACT_TIME)
                      + parties + instrument_required + disclosure),
            optional=(SECURITY_EXCHANGE, EXEC_INST, C.TEXT, TIME_IN_FORCE,
                      PRICE, POSITION_EFFECT, ORDER_CAPACITY,
                      ORDER_RESTRICTIONS, MAX_PRICE_LEVELS,
                      SELF_MATCH_PREVENTION_ID, LOT_TYPE),
            inbound=True),

        MessageDef(
            C.ORDER_CANCEL_REPLACE_REQUEST, "OrderCancelReplaceRequest",
            required=((CL_ORD_ID, ORIG_CL_ORD_ID, ORD_TYPE, SIDE, ORDER_QTY,
                       TRANSACT_TIME) + parties + instrument_required
                      + disclosure),
            optional=(SECURITY_EXCHANGE, ORDER_ID, EXEC_INST, C.TEXT,
                      TIME_IN_FORCE, PRICE, POSITION_EFFECT, ORDER_CAPACITY,
                      ORDER_RESTRICTIONS, MAX_PRICE_LEVELS),
            inbound=True),

        MessageDef(
            C.ORDER_CANCEL_REQUEST, "OrderCancelRequest",
            required=((CL_ORD_ID, ORIG_CL_ORD_ID, SIDE, TRANSACT_TIME)
                      + parties + instrument_required),
            optional=(SECURITY_EXCHANGE, ORDER_ID, C.TEXT),
            inbound=True),

        MessageDef(
            ORDER_MASS_CANCEL_REQUEST, "OrderMassCancelRequest",
            required=((CL_ORD_ID, MASS_CANCEL_REQUEST_TYPE, TRANSACT_TIME)
                      + parties),
            optional=_INSTRUMENT + (SIDE, MARKET_SEGMENT_ID),
            inbound=True),

        MessageDef(
            C.EXECUTION_REPORT, "ExecutionReport",
            optional=((CL_ORD_ID, ORIG_CL_ORD_ID, ORDER_ID, EXEC_ID,
                       TRD_MATCH_ID, ORD_TYPE, TIME_IN_FORCE, SIDE, ORDER_QTY,
                       PRICE, TRANSACT_TIME, ORDER_CAPACITY, ORDER_RESTRICTIONS,
                       MAX_PRICE_LEVELS, SELF_MATCH_PREVENTION_ID,
                       POSITION_EFFECT, ORD_STATUS, EXEC_TYPE, LAST_PX,
                       LAST_QTY, CUM_QTY, LEAVES_QTY, MATCH_TYPE,
                       ORDER_CATEGORY, AGGRESSOR_INDICATOR, LOT_TYPE,
                       ORD_REJ_REASON, CXL_REJ_REASON, EXEC_RESTATEMENT_REASON,
                       REJECT_TEXT, C.TEXT) + _PARTIES + _INSTRUMENT),
            inbound=False),

        MessageDef(
            ORDER_MASS_CANCEL_REPORT, "OrderMassCancelReport",
            optional=((CL_ORD_ID, MASS_ACTION_REPORT_ID,
                       MASS_CANCEL_REQUEST_TYPE, MASS_CANCEL_RESPONSE,
                       MASS_CANCEL_REJECT_REASON, TRANSACT_TIME, C.TEXT)
                      + _PARTIES + _INSTRUMENT),
            inbound=False),

        MessageDef(
            C.BUSINESS_MESSAGE_REJECT, "BusinessMessageReject",
            optional=(C.REF_SEQ_NUM, C.TEXT, C.REF_MSG_TYPE, C.REF_TAG_ID,
                      C.BUSINESS_REJECT_REF_ID, C.BUSINESS_REJECT_REASON),
            inbound=False),
    ]


def build():
    """The complete HKEX dialect: FIXT.1.1 session layer plus business messages."""
    return build_session_dictionary(
        begin_string="FIXT.1.1",
        fields=session_extensions() + application_fields(),
        messages=(session_messages() + application_messages()
                  + entitlement_messages()),
        header=HEADER_TAGS,
        timestamp_decimals=TIMESTAMP_DECIMALS)


def build_binary():
    """The same dialect as the binary specification tables it.

    A separate dictionary rather than a flag on the other one, because it is a
    transcription of a separate published document: where the two disagree, each
    session is validated against the table its own client was written from.

    ``begin_string`` is nominal here -- the binary encoding carries no
    BeginString -- but it still identifies the session, so it names the
    encoding rather than claiming a FIX version the wire never states.
    """
    return build_session_dictionary(
        begin_string=BINARY_BEGIN_STRING,
        fields=session_extensions() + application_fields(),
        messages=(binary_session_messages() + binary_application_messages()
                  + entitlement_messages()),
        header=BINARY_HEADER_TAGS,
        timestamp_decimals=TIMESTAMP_DECIMALS)
