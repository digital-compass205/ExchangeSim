"""The OCG-C binary dialect: what sits at each bit of each message.

Transcribed from HKEX_OCGC_Binary_Trading_Protocol_3.2 section 7, message by
message, and written in the FIX tags the venue already speaks -- so a binary
client reaches the same handlers, the same validation and the same books as a
FIX one, and neither knows about the other.

The two encodings are the same protocol, but they are not a transliteration.
Three differences are real and are handled here rather than pretended away:

* **Values disagree where the types disagree.** ``OrdStatus`` Expired is ``C``
  in FIX and 12 in binary, ``OrderCapacity`` is ``A``/``P`` against 1/2,
  ``ExecInst`` is ``c``/``x`` against ``0``/``1``. Each such field carries the
  converter that knows both spellings; a field whose values genuinely match --
  ``Side``, ``TimeInForce``, ``ExecType`` -- carries the identity one.
* **Binary has no repeating groups here.** FIX's ``<Parties>`` block is four
  flat fields, one per role, and ``<DisclosureInstructionGrp>`` is a bitmap.
  Reading and writing the group is what the getters and setters below do, so
  the handlers keep seeing the group they were written against.
* **Bit positions are per message, not global.** ``Price`` is bit 9 of a New
  Order, bit 11 of an Amend and bit 12 of an Execution Report. There is no
  shared table to factor out and inventing one would be a bug waiting to happen.

Every Execution Report variant in section 7.6.7 -- accepted, rejected,
cancelled, expired, amended, traded, cancel-rejected -- shares one bit
assignment and differs only in which fields it fills, exactly as the single FIX
``ExecutionReport`` does. So there is one layout here, and which fields it sets
stays where it already was: in the handlers.
"""

from ...binary import types as T
from ...binary import values as V
from ...binary.layout import BinaryDictionary, Block, Field, Layout
from ...fix import constants as C
from . import dictionary as D

# -- wire types --------------------------------------------------------------

U8 = T.Unsigned(1)
U16 = T.Unsigned(2)
U32 = T.Unsigned(4)
DECIMAL = T.Decimal()
BYTE = T.Byte()

BROKER_ID = T.AlphaFixed(12)
LOCATION_ID = T.AlphaFixed(11)
IDENTIFIER = T.AlphaFixed(21)            # ClOrdID, OrderID, ExecID, SecurityID
TRADE_ID = T.AlphaFixed(25)
TRANSACT_TIME = T.AlphaFixed(25)
EXCHANGE = T.AlphaFixed(5)
SEGMENT = T.AlphaFixed(20)
SMP_ID = T.AlphaFixed(10)
ENTITLEMENT_ID = T.AlphaFixed(21)
PASSWORD = T.AlphaFixed(450)
FIELD_NAME = T.AlphaFixed(50)
TEXT = T.AlphaVariable(50)
REASON = T.AlphaVariable(75)

# -- value converters --------------------------------------------------------
#
# Only the fields whose two encodings disagree need one of these. Everything
# else is a number that means the same on both sides (Side, TimeInForce,
# OrdType, SecurityIDSource, MassCancelRequestType, the reject codes) or a
# character that does (ExecType), or plain text.

ORD_STATUS = V.Enum({
    D.OrdStatus.NEW: 0,
    D.OrdStatus.PARTIALLY_FILLED: 1,
    D.OrdStatus.FILLED: 2,
    D.OrdStatus.CANCELED: 4,
    D.OrdStatus.PENDING_CANCEL: 6,
    D.OrdStatus.REJECTED: 8,
    D.OrdStatus.PENDING_NEW: 10,
    D.OrdStatus.EXPIRED: 12,
    D.OrdStatus.PENDING_REPLACE: 14,
})

ORDER_CAPACITY = V.Enum({
    D.OrderCapacity.AGENCY: 1,
    D.OrderCapacity.PRINCIPAL: 2,
})

POSITION_EFFECT = V.Enum({D.PositionEffect.CLOSE: 1})

ORDER_CATEGORY = V.Enum({D.OrderCategory.INTERNAL_CROSS: 1})

EXEC_INST = V.MultiEnum({
    D.ExecInstValue.IGNORE_PRICE_CHECKS: "0",
    D.ExecInstValue.IGNORE_NOTIONAL: "1",
})

#: Which bit of the ``Disclosure Instructions`` word each DisclosureType is.
#: The specification names one, "None", and reserves the rest.
DISCLOSURE_BITS = {D.DisclosureType.NONE: 0}


# -- <Parties>, as four flat fields ------------------------------------------

def _party_get(role):
    """Read one PartyID out of the group by its role."""
    def get(message):
        ids = message.get_all(D.PARTY_ID)
        roles = message.get_all(D.PARTY_ROLE)
        for party_id, party_role in zip(ids, roles):
            if party_role == role:
                return party_id
        return None
    return get


def _party_put(role):
    """Append one entry to the group, keeping NoPartyIDs honest.

    Fields arrive in ascending bit position, which for every message here puts
    the broker before the location before the BCAN -- so the group is built in
    the order the FIX encoding would have carried it.
    """
    def put(message, value):
        if not message.has(D.NO_PARTY_IDS):
            # Set first so the count precedes its entries, as it does on the
            # wire; the value is corrected as soon as the entry is appended.
            message.set(D.NO_PARTY_IDS, 0)
        message.append(D.PARTY_ID, value)
        message.append(D.PARTY_ID_SOURCE, D.PartyIDSource.PROPRIETARY)
        message.append(D.PARTY_ROLE, role)
        message.set(D.NO_PARTY_IDS, len(message.get_all(D.PARTY_ID)))
    return put


def _party(bit, name, role, type_=BROKER_ID):
    return Field(bit, name, type_, get=_party_get(role), put=_party_put(role))


# -- <DisclosureInstructionGrp>, as a bitmap ---------------------------------

def _disclosure_get(message):
    types = message.get_all(D.DISCLOSURE_TYPE)
    if not types:
        return None
    instructions = message.get_all(D.DISCLOSURE_INSTRUCTION)
    word = 0
    for disclosure_type, instruction in zip(types, instructions):
        if instruction != D.DisclosureInstruction.YES:
            continue
        position = DISCLOSURE_BITS.get(disclosure_type)
        if position is not None:
            word |= 1 << position
    return word


def _disclosure_put(message, value):
    entries = [disclosure_type
               for disclosure_type, position in sorted(DISCLOSURE_BITS.items())
               if value & (1 << position)]
    message.set(D.NO_DISCLOSURE_INSTRUCTIONS, len(entries))
    for disclosure_type in entries:
        message.append(D.DISCLOSURE_TYPE, disclosure_type)
        message.append(D.DISCLOSURE_INSTRUCTION, D.DisclosureInstruction.YES)


def _disclosure(bit):
    return Field(bit, "DisclosureInstructions", U16,
                 get=_disclosure_get, put=_disclosure_put)


# -- reusable field groups ---------------------------------------------------
#
# Bit positions differ per message, so these take theirs as arguments rather
# than being shared tuples: the fields repeat, the numbering does not.

def _instrument(security_id, source, exchange):
    return [
        Field(security_id, "SecurityID", IDENTIFIER, D.SECURITY_ID),
        Field(source, "SecurityIDSource", U8, D.SECURITY_ID_SOURCE, V.NUMBER),
        Field(exchange, "SecurityExchange", EXCHANGE, D.SECURITY_EXCHANGE),
    ]


def _order_terms(ord_type, price, order_qty, tif):
    return [
        Field(ord_type, "OrderType", U8, D.ORD_TYPE, V.NUMBER),
        Field(price, "Price", DECIMAL, D.PRICE, V.QUANTITY),
        Field(order_qty, "OrderQuantity", DECIMAL, D.ORDER_QTY, V.QUANTITY),
        Field(tif, "TIF", U8, D.TIME_IN_FORCE, V.NUMBER),
    ]


def _handling(position_effect, restrictions, max_levels, capacity, text):
    return [
        Field(position_effect, "PositionEffect", U8, D.POSITION_EFFECT,
              POSITION_EFFECT),
        Field(restrictions, "OrderRestrictions", IDENTIFIER,
              D.ORDER_RESTRICTIONS),
        Field(max_levels, "MaxPriceLevels", U8, D.MAX_PRICE_LEVELS, V.NUMBER),
        Field(capacity, "OrderCapacity", U8, D.ORDER_CAPACITY, ORDER_CAPACITY),
        Field(text, "Text", TEXT, C.TEXT),
    ]


# -- session messages, section 7.5 -------------------------------------------

def _session_layouts():
    return [
        Layout(5, "Logon", C.LOGON, [
            Field(0, "Password", PASSWORD, C.ENCRYPTED_PASSWORD, redact=True),
            Field(1, "NewPassword", PASSWORD, C.ENCRYPTED_NEW_PASSWORD,
                  redact=True),
            Field(2, "NextExpectedMessageSequence", U32,
                  C.NEXT_EXPECTED_MSG_SEQ_NUM, V.NUMBER),
            Field(3, "SessionStatus", U8, C.SESSION_STATUS, V.NUMBER),
            Field(4, "Text", TEXT, C.TEXT),
            Field(5, "TestMessageIndicator", U8, C.TEST_MESSAGE_INDICATOR,
                  V.FLAG),
        ]),

        Layout(6, "Logout", C.LOGOUT, [
            Field(0, "LogoutText", REASON, C.TEXT),
            Field(1, "SessionStatus", U8, C.SESSION_STATUS, V.NUMBER),
        ]),

        Layout(0, "Heartbeat", C.HEARTBEAT, [
            Field(0, "ReferenceTestRequestID", U16, C.TEST_REQ_ID, V.NUMBER),
        ]),

        Layout(1, "TestRequest", C.TEST_REQUEST, [
            Field(0, "TestRequestID", U16, C.TEST_REQ_ID, V.NUMBER),
        ]),

        Layout(2, "ResendRequest", C.RESEND_REQUEST, [
            Field(0, "StartSequence", U32, C.BEGIN_SEQ_NO, V.NUMBER),
            Field(1, "EndSequence", U32, C.END_SEQ_NO, V.NUMBER),
        ]),

        Layout(4, "SequenceReset", C.SEQUENCE_RESET, [
            # ASSUMPTION: the specification's value list for this Byte field is
            # a graphic the PDF's text layer does not carry. Y/N, as FIX spells
            # GapFillFlag and as the Byte type implies -- every genuinely
            # numeric flag in this dictionary is a UInt8 instead.
            Field(0, "GapFill", BYTE, C.GAP_FILL_FLAG, V.CHARACTER),
            Field(1, "NewSequenceNumber", U32, C.NEW_SEQ_NO, V.NUMBER),
        ]),
    ]


# -- business messages, sections 7.6 and 7.9 ---------------------------------

def _new_order():
    """New Order (11), section 7.6.1, with the odd-lot variant's bit 19.

    Section 7.6.2 gives odd and special lots their own table over the same
    message type; the only field it adds is ``Lot Type``, so one layout carries
    both and the venue refuses the odd lot exactly where it already did.
    """
    return Layout(11, "NewOrder", C.NEW_ORDER_SINGLE, [
        Field(0, "ClientOrderID", IDENTIFIER, D.CL_ORD_ID),
        _party(1, "SubmittingBrokerID", D.PartyRole.EXECUTING_FIRM),
    ] + _instrument(2, 3, 4) + [
        _party(5, "BrokerLocationID", D.PartyRole.LOCATION_ID, LOCATION_ID),
        Field(6, "TransactionTime", TRANSACT_TIME, D.TRANSACT_TIME),
        Field(7, "Side", U8, D.SIDE, V.NUMBER),
    ] + _order_terms(8, 9, 10, 11)
      + _handling(12, 13, 14, 15, 16) + [
        Field(17, "ExecutionInstructions", IDENTIFIER, D.EXEC_INST, EXEC_INST),
        _disclosure(18),
        Field(19, "LotType", U8, D.LOT_TYPE, V.NUMBER),
        _party(22, "SubmittingBCANField", D.PartyRole.CLIENT_ID, IDENTIFIER),
        Field(23, "SMPID", SMP_ID, D.SELF_MATCH_PREVENTION_ID),
    ])


def _amend_order():
    """Amend Order (12), section 7.6.3."""
    return Layout(12, "AmendOrder", C.ORDER_CANCEL_REPLACE_REQUEST, [
        Field(0, "ClientOrderID", IDENTIFIER, D.CL_ORD_ID),
        _party(1, "SubmittingBrokerID", D.PartyRole.EXECUTING_FIRM),
    ] + _instrument(2, 3, 4) + [
        _party(5, "BrokerLocationID", D.PartyRole.LOCATION_ID, LOCATION_ID),
        Field(6, "TransactionTime", TRANSACT_TIME, D.TRANSACT_TIME),
        Field(7, "Side", U8, D.SIDE, V.NUMBER),
        Field(8, "OriginalClientOrderID", IDENTIFIER, D.ORIG_CL_ORD_ID),
        Field(9, "OrderID", IDENTIFIER, D.ORDER_ID),
    ] + _order_terms(10, 11, 12, 13)
      + _handling(14, 15, 16, 17, 18) + [
        Field(19, "ExecutionInstructions", IDENTIFIER, D.EXEC_INST, EXEC_INST),
        _disclosure(20),
    ])


def _cancel_order():
    """Cancel Order (13), section 7.6.4.

    Carries no OrderQty, which the FIX encoding requires -- see
    :func:`exchangesim.venues.hkex.dictionary.build_binary`.
    """
    return Layout(13, "CancelOrder", C.ORDER_CANCEL_REQUEST, [
        Field(0, "ClientOrderID", IDENTIFIER, D.CL_ORD_ID),
        _party(1, "SubmittingBrokerID", D.PartyRole.EXECUTING_FIRM),
    ] + _instrument(2, 3, 4) + [
        _party(5, "BrokerLocationID", D.PartyRole.LOCATION_ID, LOCATION_ID),
        Field(6, "TransactionTime", TRANSACT_TIME, D.TRANSACT_TIME),
        Field(7, "Side", U8, D.SIDE, V.NUMBER),
        Field(8, "OriginalClientOrderID", IDENTIFIER, D.ORIG_CL_ORD_ID),
        Field(9, "OrderID", IDENTIFIER, D.ORDER_ID),
        Field(10, "Text", TEXT, C.TEXT),
    ])


def _mass_cancel():
    """Mass Cancel (14), section 7.6.5."""
    return Layout(14, "MassCancel", D.ORDER_MASS_CANCEL_REQUEST, [
        Field(0, "ClientOrderID", IDENTIFIER, D.CL_ORD_ID),
        _party(1, "SubmittingBrokerID", D.PartyRole.EXECUTING_FIRM),
    ] + _instrument(2, 3, 4) + [
        _party(5, "BrokerLocationID", D.PartyRole.LOCATION_ID, LOCATION_ID),
        Field(6, "TransactionTime", TRANSACT_TIME, D.TRANSACT_TIME),
        Field(7, "Side", U8, D.SIDE, V.NUMBER),
        Field(8, "MassCancelRequestType", U8, D.MASS_CANCEL_REQUEST_TYPE,
              V.NUMBER),
        Field(9, "MarketSegmentID", SEGMENT, D.MARKET_SEGMENT_ID),
    ])


def _cancel_reject_code(response_to, tag=D.CXL_REJ_REASON):
    """``CxlRejReason`` under whichever of the two bits this reject is.

    Binary splits the FIX field in two -- bit 29 for a cancel, bit 36 for an
    amend -- and the Execution Report says which it is in ``ExecType``. Both
    read the same tag; each declines to write itself when the other one is the
    right bit.
    """
    def get(message):
        if message.get(D.EXEC_TYPE) != response_to:
            return None
        value = message.get(tag)
        return None if value is None else int(value)

    def put(message, value):
        message.set(tag, str(value))

    return get, put


def _execution_report():
    """Execution Report (10), section 7.6.7 -- every variant, one bit table."""
    cancel_get, cancel_put = _cancel_reject_code(D.ExecType.CANCEL_REJECT)
    amend_get, amend_put = _cancel_reject_code(D.ExecType.AMEND_REJECT)

    return Layout(10, "ExecutionReport", C.EXECUTION_REPORT, [
        Field(0, "ClientOrderID", IDENTIFIER, D.CL_ORD_ID),
        _party(1, "SubmittingBrokerID", D.PartyRole.EXECUTING_FIRM),
    ] + _instrument(2, 3, 4) + [
        _party(5, "BrokerLocationID", D.PartyRole.LOCATION_ID, LOCATION_ID),
        Field(6, "TransactionTime", TRANSACT_TIME, D.TRANSACT_TIME),
        Field(7, "Side", U8, D.SIDE, V.NUMBER),
        Field(8, "OriginalClientOrderID", IDENTIFIER, D.ORIG_CL_ORD_ID),
        Field(9, "OrderID", IDENTIFIER, D.ORDER_ID),
        _party(10, "OwningBrokerID", D.PartyRole.ENTERING_TRADER),
    ] + _order_terms(11, 12, 13, 14)
      + _handling(15, 16, 17, 18, 19) + [
        Field(20, "Reason", REASON, D.REJECT_TEXT),
        Field(21, "ExecutionID", IDENTIFIER, D.EXEC_ID),
        Field(22, "OrderStatus", U8, D.ORD_STATUS, ORD_STATUS),
        Field(23, "ExecType", BYTE, D.EXEC_TYPE, V.CHARACTER),
        Field(24, "CumulativeQuantity", DECIMAL, D.CUM_QTY, V.QUANTITY),
        Field(25, "LeavesQuantity", DECIMAL, D.LEAVES_QTY, V.QUANTITY),
        Field(26, "OrderRejectCode", U16, D.ORD_REJ_REASON, V.NUMBER),
        Field(27, "LotType", U8, D.LOT_TYPE, V.NUMBER),
        Field(28, "ExecRestatementReason", U16, D.EXEC_RESTATEMENT_REASON,
              V.NUMBER),
        Field(29, "CancelRejectCode", U16, get=cancel_get, put=cancel_put),
        Field(30, "MatchType", U8, D.MATCH_TYPE, V.NUMBER),
        _party(31, "CounterpartyBrokerID", D.PartyRole.CONTRA_FIRM),
        Field(32, "ExecutionQuantity", DECIMAL, D.LAST_QTY, V.QUANTITY),
        Field(33, "ExecutionPrice", DECIMAL, D.LAST_PX, V.QUANTITY),
        Field(35, "OrderCategory", U8, D.ORDER_CATEGORY, ORDER_CATEGORY),
        Field(36, "AmendRejectCode", U16, get=amend_get, put=amend_put),
        Field(38, "TradeMatchID", TRADE_ID, D.TRD_MATCH_ID),
        Field(42, "AggressorIndicator", U8, D.AGGRESSOR_INDICATOR, V.FLAG),
        Field(43, "SMPID", SMP_ID, D.SELF_MATCH_PREVENTION_ID),
    ])


def _mass_cancel_report():
    """Order Mass Cancel Report (15), section 7.6.8."""
    return Layout(15, "MassCancelReport", D.ORDER_MASS_CANCEL_REPORT, [
        Field(0, "ClientOrderID", IDENTIFIER, D.CL_ORD_ID),
        _party(1, "SubmittingBrokerID", D.PartyRole.EXECUTING_FIRM),
    ] + _instrument(2, 3, 4) + [
        _party(5, "BrokerLocationID", D.PartyRole.LOCATION_ID, LOCATION_ID),
        Field(6, "TransactionTime", TRANSACT_TIME, D.TRANSACT_TIME),
        Field(7, "MassCancelRequestType", U8, D.MASS_CANCEL_REQUEST_TYPE,
              V.NUMBER),
        _party(8, "OwningBrokerID", D.PartyRole.ENTERING_TRADER),
        Field(9, "MassActionReportID", IDENTIFIER, D.MASS_ACTION_REPORT_ID),
        Field(10, "MassCancelResponse", U8, D.MASS_CANCEL_RESPONSE, V.NUMBER),
        Field(11, "MassCancelRejectCode", U16, D.MASS_CANCEL_REJECT_REASON,
              V.NUMBER),
        Field(12, "Reason", REASON, C.TEXT),
    ])


# -- party entitlements, section 7.10 ----------------------------------------

def _entitlement_request():
    """Party Entitlement Request (27): one field, and a whole handshake."""
    return Layout(27, "PartyEntitlementRequest", D.PARTY_ENTITLEMENT_REQUEST, [
        Field(0, "EntitlementRequestID", IDENTIFIER, D.ENTITLEMENTS_REQUEST_ID),
    ])


def _entitlement_report():
    """Party Entitlement Report (28), section 7.10.2.

    The one message here with repeating blocks, and the reason the codec has
    them. Note where the two encodings part company: FIX wraps the broker in
    ``<PartyEntitlementGrp><PartyDetailGrp>``, while binary carries a flat
    ``Broker ID`` at bit 5 and hangs the entitlements straight off the message.
    The counts those FIX wrappers need have no bit here, so they simply do not
    travel -- which is exactly what a per-encoding layout is for.
    """
    attributes = Block(2, "NoEntitlementAttributes", D.NO_ENTITLEMENT_ATTRIB, [
        Field(0, "EntitlementAttributeType", U16, D.ENTITLEMENT_ATTRIB_TYPE,
              V.NUMBER),
        Field(1, "EntitlementAttributeDataType", U8,
              D.ENTITLEMENT_ATTRIB_DATA_TYPE, V.NUMBER),
        Field(2, "EntitlementAttributeValue", ENTITLEMENT_ID,
              D.ENTITLEMENT_ATTRIB_VALUE),
    ])

    scopes = Block(4, "NoInstrumentScopes", D.NO_INSTRUMENT_SCOPES, [
        Field(0, "InstrumentScopeOperator", U8, D.INSTRUMENT_SCOPE_OPERATOR,
              V.NUMBER),
        Field(1, "SecurityID", IDENTIFIER, D.INSTRUMENT_SCOPE_SECURITY_ID),
        Field(2, "SecurityIDSource", U8,
              D.INSTRUMENT_SCOPE_SECURITY_ID_SOURCE, V.NUMBER),
        Field(3, "SecurityExchange", EXCHANGE,
              D.INSTRUMENT_SCOPE_SECURITY_EXCHANGE),
    ])

    entitlements = Block(6, "NoEntitlements", D.NO_ENTITLEMENTS, [
        Field(0, "EntitlementType", U8, D.ENTITLEMENT_TYPE, V.NUMBER),
        Field(1, "EntitlementIndicator", U8, D.ENTITLEMENT_INDICATOR, V.FLAG),
        attributes,
        Field(3, "EntitlementID", ENTITLEMENT_ID, D.ENTITLEMENT_ID),
        scopes,
    ])

    return Layout(28, "PartyEntitlementReport", D.PARTY_ENTITLEMENT_REPORT, [
        Field(0, "EntitlementReportID", IDENTIFIER, D.ENTITLEMENTS_REPORT_ID),
        Field(1, "EntitlementRequestID", IDENTIFIER, D.ENTITLEMENTS_REQUEST_ID),
        Field(2, "RequestResult", U16, D.REQUEST_RESULT, V.NUMBER),
        Field(3, "TotalNoPartyList", U16, D.TOT_NO_PARTY_LIST, V.NUMBER),
        Field(4, "LastFragment", U8, D.LAST_FRAGMENT, V.FLAG),
        Field(5, "BrokerID", BROKER_ID, D.PARTY_DETAIL_ID),
        entitlements,
    ])


def build(dictionary):
    """Every layout of the dialect, resolvable from either encoding's type.

    ``dictionary`` is the venue's own :class:`~exchangesim.fix.dictionary.
    Dictionary`, which the two reference fields need: binary names the message
    type it is rejecting by number and the field by *name*, where FIX gives the
    MsgType character and the tag number.
    """
    # The two reference converters need the finished dictionary, which needs
    # them first. One cell, filled the moment it exists, keeps that honest --
    # neither converter can run before a message reaches the codec.
    holder = []

    def ref_msg_type_get(message):
        value = message.get(C.REF_MSG_TYPE)
        return None if value is None else holder[0].binary_type(value)

    def ref_msg_type_put(message, value):
        message.set(C.REF_MSG_TYPE, holder[0].fix_msg_type(value))

    def ref_field_get(message):
        tag = message.get_int(C.REF_TAG_ID)
        if tag is None:
            return None
        field = dictionary.field(tag)
        return field.name if field is not None else str(tag)

    def ref_field_put(message, value):
        field = dictionary.fields_by_name.get(value)
        if field is not None:
            message.set(C.REF_TAG_ID, field.tag)

    reject = Layout(3, "Reject", C.REJECT, [
        Field(0, "MessageRejectCode", U16, C.SESSION_REJECT_REASON, V.NUMBER),
        Field(1, "Reason", REASON, C.TEXT),
        Field(2, "ReferenceMessageType", U8,
              get=ref_msg_type_get, put=ref_msg_type_put),
        Field(3, "ReferenceFieldName", FIELD_NAME,
              get=ref_field_get, put=ref_field_put),
        Field(4, "ReferenceSequenceNumber", U32, C.REF_SEQ_NUM, V.NUMBER),
        Field(5, "ClientOrderID", IDENTIFIER, D.CL_ORD_ID),
    ])

    business_reject = Layout(9, "BusinessMessageReject",
                             C.BUSINESS_MESSAGE_REJECT, [
        Field(0, "BusinessRejectCode", U16, C.BUSINESS_REJECT_REASON, V.NUMBER),
        Field(1, "Reason", REASON, C.TEXT),
        Field(2, "ReferenceMessageType", U8,
              get=ref_msg_type_get, put=ref_msg_type_put),
        Field(3, "ReferenceFieldName", FIELD_NAME,
              get=ref_field_get, put=ref_field_put),
        Field(4, "ReferenceSequenceNumber", U32, C.REF_SEQ_NUM, V.NUMBER),
        Field(5, "BusinessRejectReferenceID", IDENTIFIER,
              C.BUSINESS_REJECT_REF_ID),
    ])

    layouts = _session_layouts() + [
        reject,
        business_reject,
        _new_order(),
        _amend_order(),
        _cancel_order(),
        _mass_cancel(),
        _execution_report(),
        _mass_cancel_report(),
        _entitlement_request(),
        _entitlement_report(),
    ]

    binary_dictionary = BinaryDictionary(layouts)
    holder.append(binary_dictionary)
    return binary_dictionary
