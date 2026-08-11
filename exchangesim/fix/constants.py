"""Standard FIX 4.2 constants used by the session layer.

Only protocol-generic definitions belong here. Business enumerations that a
venue narrows or extends -- OrdRejReason values, custom tags, supported order
types -- live in that venue's module.
"""

# -- administrative message types -------------------------------------------

HEARTBEAT = "0"
TEST_REQUEST = "1"
RESEND_REQUEST = "2"
REJECT = "3"
SEQUENCE_RESET = "4"
LOGOUT = "5"
LOGON = "A"

ADMIN_MSG_TYPES = frozenset((HEARTBEAT, TEST_REQUEST, RESEND_REQUEST, REJECT,
                             SEQUENCE_RESET, LOGOUT, LOGON))

# -- application message types (those Japannext uses) -----------------------

EXECUTION_REPORT = "8"
ORDER_CANCEL_REJECT = "9"
NEW_ORDER_SINGLE = "D"
ORDER_CANCEL_REQUEST = "F"
ORDER_CANCEL_REPLACE_REQUEST = "G"
BUSINESS_MESSAGE_REJECT = "j"
TRADING_SESSION_STATUS = "h"

# -- header / trailer tags ---------------------------------------------------

BEGIN_STRING = 8
BODY_LENGTH = 9
MSG_TYPE = 35
MSG_SEQ_NUM = 34
SENDER_COMP_ID = 49
SENDER_SUB_ID = 50
SENDING_TIME = 52
TARGET_COMP_ID = 56
TARGET_SUB_ID = 57
POSS_DUP_FLAG = 43
POSS_RESEND = 97
ORIG_SENDING_TIME = 122
CHECKSUM = 10

# -- session-layer body tags -------------------------------------------------

BEGIN_SEQ_NO = 7
END_SEQ_NO = 16
NEW_SEQ_NO = 36
REF_SEQ_NUM = 45
TEXT = 58
ENCRYPT_METHOD = 98
HEART_BT_INT = 108
TEST_REQ_ID = 112
GAP_FILL_FLAG = 123
RESET_SEQ_NUM_FLAG = 141
REF_TAG_ID = 371
REF_MSG_TYPE = 372
SESSION_REJECT_REASON = 373

# -- FIXT.1.1 session tags ---------------------------------------------------
#
# FIX 5.0 split the session layer (FIXT.1.1) from the application layer, which
# is then named per session and per message. A FIX 4.2 venue never sets any of
# these; they exist for dialects such as HKEX OCG-C that run FIX 5.0 SP2
# semantics over a FIXT.1.1 session.

NEXT_EXPECTED_MSG_SEQ_NUM = 789
TEST_MESSAGE_INDICATOR = 464
APPL_VER_ID = 1128
DEFAULT_APPL_VER_ID = 1137
ENCRYPTED_PASSWORD_METHOD = 1400
ENCRYPTED_PASSWORD_LEN = 1401
ENCRYPTED_PASSWORD = 1402
ENCRYPTED_NEW_PASSWORD_LEN = 1403
ENCRYPTED_NEW_PASSWORD = 1404
SESSION_STATUS = 1409


class ApplVerID(object):
    """``ApplVerID(1128)`` / ``DefaultApplVerID(1137)`` values."""

    FIX50SP2 = "9"


class SessionStatus(object):
    """``SessionStatus(1409)`` values used on Logon and Logout."""

    ACTIVE = "0"
    PASSWORD_CHANGED = "1"
    PASSWORD_EXPIRED = "2"
    LOGOUT_COMPLETE = "4"
    INVALID_USERNAME_OR_PASSWORD = "5"
    ACCOUNT_LOCKED = "6"
    LOGONS_NOT_ALLOWED = "7"
    PASSWORD_EXPIRY_WARNING = "8"
    OTHER = "100"

# -- business-reject tags ----------------------------------------------------

BUSINESS_REJECT_REF_ID = 379
BUSINESS_REJECT_REASON = 380


class SessionRejectReason(object):
    """``SessionRejectReason(373)`` values.

    The first block is what Japannext lists; the second is the remainder HKEX
    OCG-C adds, which only a dialect with repeating groups can provoke.
    """

    INVALID_TAG_NUMBER = 0
    REQUIRED_TAG_MISSING = 1
    TAG_NOT_DEFINED_FOR_MESSAGE = 2
    UNDEFINED_TAG = 3
    TAG_WITHOUT_VALUE = 4
    VALUE_INCORRECT = 5
    INCORRECT_DATA_FORMAT = 6
    COMPID_PROBLEM = 9
    SENDING_TIME_ACCURACY = 10
    INVALID_MSGTYPE = 11

    TAG_APPEARS_MORE_THAN_ONCE = 13
    REPEATING_GROUP_OUT_OF_ORDER = 15
    INCORRECT_NUM_IN_GROUP = 16
    UNSUPPORTED_APPL_VERSION = 18
    OTHER = 99


class BusinessRejectReason(object):
    """``BusinessRejectReason(380)`` values supported by Japannext."""

    OTHER = 0
    UNSUPPORTED_MESSAGE_TYPE = 3
    CONDITIONALLY_REQUIRED_FIELD_MISSING = 5


class EncryptMethod(object):
    NONE = "0"


YES = "Y"
NO = "N"


# -- field data types --------------------------------------------------------

class FieldType(object):
    STRING = "STRING"
    CHAR = "CHAR"
    INT = "INT"
    QTY = "QTY"
    PRICE = "PRICE"
    AMT = "AMT"
    BOOLEAN = "BOOLEAN"
    UTC_TIMESTAMP = "UTCTIMESTAMP"
    LOCAL_DATE = "LOCALMKTDATE"
    MULTI_VALUE_STRING = "MULTIPLEVALUESTRING"
