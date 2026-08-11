"""The FIX 4.2 session layer as dictionary entries.

Every venue's dialect shares these: the standard header and trailer, and the
seven administrative messages. A venue dictionary composes them with its own
application messages via :func:`build_session_dictionary`, so session mechanics
are described once.
"""

from . import constants as C
from .dictionary import Dictionary, FieldDef, MessageDef, enum_labels

# The header tags Japannext lists for incoming and outgoing messages. Kept as
# one set because the session layer treats them uniformly.
HEADER_TAGS = (
    C.MSG_TYPE, C.MSG_SEQ_NUM, C.SENDER_COMP_ID, C.SENDER_SUB_ID,
    C.SENDING_TIME, C.TARGET_COMP_ID, C.TARGET_SUB_ID,
    C.POSS_DUP_FLAG, C.POSS_RESEND, C.ORIG_SENDING_TIME,
)

TRAILER_TAGS = (C.CHECKSUM,)


def session_fields(sub_id_in_length=30, sub_id_out_length=4, sub_id_labels=None):
    """Field definitions for the standard header, trailer and admin bodies.

    ``SenderSubID``/``TargetSubID`` lengths differ by direction at Japannext
    (4 characters outbound, 30 inbound), so the inbound limit is the one that
    constrains validation of what clients send. What the SubIDs *mean* is the
    venue's business -- at Japannext they name markets -- so a dialect that uses
    them passes ``sub_id_labels``; the length stays here.
    """
    T = C.FieldType
    return [
        # -- structural --
        # Validation skips these three: the codec derives them and a client
        # cannot get them wrong without the message failing to frame at all.
        # They are defined so that anything reading a message -- the audit
        # view -- can name them rather than showing a bare tag number.
        FieldDef(C.BEGIN_STRING, "BeginString", T.STRING, max_length=8),
        FieldDef(C.BODY_LENGTH, "BodyLength", T.INT),

        # -- header --
        FieldDef(C.MSG_TYPE, "MsgType", T.STRING, max_length=2),
        FieldDef(C.MSG_SEQ_NUM, "MsgSeqNum", T.INT),
        FieldDef(C.SENDER_COMP_ID, "SenderCompID", T.STRING, max_length=32),
        FieldDef(C.SENDER_SUB_ID, "SenderSubID", T.STRING,
                 max_length=max(sub_id_in_length, sub_id_out_length),
                 labels=sub_id_labels),
        FieldDef(C.SENDING_TIME, "SendingTime", T.UTC_TIMESTAMP),
        FieldDef(C.TARGET_COMP_ID, "TargetCompID", T.STRING, max_length=32),
        FieldDef(C.TARGET_SUB_ID, "TargetSubID", T.STRING,
                 max_length=max(sub_id_in_length, sub_id_out_length),
                 labels=sub_id_labels),
        FieldDef(C.POSS_DUP_FLAG, "PossDupFlag", T.BOOLEAN),
        FieldDef(C.POSS_RESEND, "PossResend", T.BOOLEAN),
        FieldDef(C.ORIG_SENDING_TIME, "OrigSendingTime", T.UTC_TIMESTAMP),
        FieldDef(C.CHECKSUM, "CheckSum", T.STRING, max_length=3),

        # -- admin bodies --
        FieldDef(C.BEGIN_SEQ_NO, "BeginSeqNo", T.INT),
        FieldDef(C.END_SEQ_NO, "EndSeqNo", T.INT),
        FieldDef(C.NEW_SEQ_NO, "NewSeqNo", T.INT),
        FieldDef(C.REF_SEQ_NUM, "RefSeqNum", T.INT),
        FieldDef(C.TEXT, "Text", T.STRING, max_length=255),
        FieldDef(C.ENCRYPT_METHOD, "EncryptMethod", T.INT, values=("0",),
                 labels=enum_labels(C.EncryptMethod)),
        FieldDef(C.HEART_BT_INT, "HeartBtInt", T.INT),
        FieldDef(C.TEST_REQ_ID, "TestReqID", T.STRING, max_length=64),
        FieldDef(C.GAP_FILL_FLAG, "GapFillFlag", T.BOOLEAN),
        FieldDef(C.RESET_SEQ_NUM_FLAG, "ResetSeqNumFlag", T.BOOLEAN),
        FieldDef(C.REF_TAG_ID, "RefTagID", T.INT),
        FieldDef(C.REF_MSG_TYPE, "RefMsgType", T.STRING, max_length=2),
        FieldDef(C.SESSION_REJECT_REASON, "SessionRejectReason", T.INT,
                 labels=enum_labels(C.SessionRejectReason)),
    ]


def session_messages():
    """Definitions for the seven administrative messages.

    Requirements follow the Japannext specification tables: ``Logon`` requires
    EncryptMethod and HeartBtInt, ``ResendRequest`` requires both sequence
    bounds, ``SequenceReset`` requires NewSeqNo, and ``TestRequest`` requires
    TestReqID.
    """
    return [
        MessageDef(C.LOGON, "Logon",
                   required=(C.ENCRYPT_METHOD, C.HEART_BT_INT),
                   optional=(C.RESET_SEQ_NUM_FLAG, C.TEXT)),
        MessageDef(C.HEARTBEAT, "Heartbeat",
                   optional=(C.TEST_REQ_ID,)),
        MessageDef(C.TEST_REQUEST, "TestRequest",
                   required=(C.TEST_REQ_ID,)),
        MessageDef(C.RESEND_REQUEST, "ResendRequest",
                   required=(C.BEGIN_SEQ_NO, C.END_SEQ_NO)),
        MessageDef(C.REJECT, "Reject",
                   required=(C.REF_SEQ_NUM,),
                   optional=(C.TEXT, C.REF_TAG_ID, C.REF_MSG_TYPE,
                             C.SESSION_REJECT_REASON)),
        MessageDef(C.SEQUENCE_RESET, "SequenceReset",
                   required=(C.NEW_SEQ_NO,),
                   optional=(C.GAP_FILL_FLAG,)),
        MessageDef(C.LOGOUT, "Logout",
                   optional=(C.TEXT,)),
    ]


def build_session_dictionary(begin_string="FIX.4.2", fields=(), messages=(),
                             header=None, sub_id_labels=None):
    """Compose the session layer with a venue's application definitions.

    Venue fields and messages are applied last and overwrite by tag and by
    MsgType, so a dialect that changes the shape of an administrative message --
    FIXT.1.1 adds ``NextExpectedMsgSeqNum(789)`` and ``DefaultApplVerID(1137)``
    to Logon -- supplies its own definition rather than needing a hook here.
    ``header`` names the tags legal in any message, which differ by dialect:
    Japannext routes markets by SubID, HKEX has no SubID at all.
    """
    return Dictionary(
        begin_string=begin_string,
        fields=list(session_fields(sub_id_labels=sub_id_labels)) + list(fields),
        messages=session_messages() + list(messages),
        header=HEADER_TAGS if header is None else header,
        trailer=TRAILER_TAGS,
    )
