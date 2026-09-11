"""Transaction codes and error codes, transcribed from the Appendix.

A transaction code is what NNF has instead of a MsgType, and it is a number:
``2000`` is a fresh order, ``2073`` its confirmation, ``2231`` its refusal. The
suffix says which way it travels -- "IN in the transaction codes implies that
the request is sent from the TWS to the host end whereas OUT implies that the
message is sent from the host end to TWS" -- and a few carry neither because
they travel both ways.

Only the **interactive** codes are here. The appendix also tables the broadcast
ones, marked ``B``, and none of them is implemented: NSE's market data is a
separate UDP multicast feed, LZO-compressed, and LZO cannot be written under
this project's standard-library-only constraint. Market data reaches a person
through the control plane, the CLI and the board instead, as it does at the
other two venues.

``ERROR_CODES`` is the whole published table rather than the subset this venue
can currently raise. It costs nothing, it makes the audit name an error a client
reports, and a code the simulator never sends is exactly the one somebody will
be looking up.
"""

# -- session and connection --------------------------------------------------

GR_REQUEST = 2400
GR_RESPONSE = 2401
SECURE_BOX_REGISTRATION_REQUEST_IN = 23008
SECURE_BOX_REGISTRATION_REQUEST_OUT = 23009
BOX_SIGN_ON_REQUEST_IN = 23000
BOX_SIGN_ON_REQUEST_OUT = 23001
SIGN_ON_REQUEST_IN = 2300
SIGN_ON_REQUEST_OUT = 2301
SIGN_OFF_REQUEST_IN = 2320
SIGN_OFF_REQUEST_OUT = 2321
ERROR_RESPONSE_OUT = 2302
HEARTBEAT = 23506

SYSTEM_INFORMATION_IN = 1600
SYSTEM_INFORMATION_OUT = 1601

# -- order entry -------------------------------------------------------------
#
# Fourteen of these share one 290-byte structure, ORDER_ENTRY_REQUEST. Two --
# BOARD_LOT_OUT and ORDER_CANCEL_OUT -- are a 214-byte truncation of it.

BOARD_LOT_IN = 2000
BOARD_LOT_OUT = 2001
PRICE_CONFIRMATION = 2012
ORDER_MOD_IN = 2040
ORDER_MOD_REJECT = 2042
ORDER_CANCEL_IN = 2070
ORDER_CANCEL_OUT = 2071
ORDER_CANCEL_REJECT = 2072
ORDER_CONFIRMATION = 2073
ORDER_MOD_CONFIRMATION = 2074
ORDER_CANCEL_CONFIRMATION = 2075
ORDER_ERROR = 2231
FREEZE_TO_CONTROL = 2170
BATCH_ORDER_CANCEL = 9002

# -- trades ------------------------------------------------------------------

TRADE_CONFIRMATION = 2222
ON_STOP_NOTIFICATION = 2212
TRADE_CANCEL_CONFIRM = 2282
TRADE_CANCEL_REJECT = 2286
TRADE_MODIFY_CONFIRM = 2287

# -- the "trimmed" order flow -------------------------------------------------
#
# Chapter 10's own appendix (Tables 57-60): a second, compact encoding of order
# entry, modification, cancellation and their answers, and of the trade
# confirmation. None of these carry the forty-byte MESSAGE_HEADER -- each
# opens with a prefix of its own. And this is not an optional extra: "the
# Request messages in transaction codes [BOARD_LOT_IN, ORDER_MOD_IN,
# ORDER_CANCEL_IN] must have BookType 1 or 11 or 12", and MS_OE_REQUEST "is
# not allowed" with those book types at all -- Regular Lot is 1, the only book
# this venue trades, so this is what a real Direct Interface gateway sends for
# every order this venue accepts. The plain 290-byte structures are served
# too, and a client is answered in whichever it asked in.

BOARD_LOT_IN_TR = 20000
ORDER_MOD_IN_TR = 20040
ORDER_MOD_REJECT_TR = 20042
ORDER_CANCEL_IN_TR = 20070
ORDER_CANCEL_REJECT_TR = 20072
ORDER_CONFIRMATION_TR = 20073
ORDER_MOD_CONFIRMATION_TR = 20074
ORDER_CXL_CONFIRMATION_TR = 20075
ORDER_ERROR_TR = 20231
PRICE_CONFIRMATION_TR = 20012
TRADE_CONFIRMATION_TR = 20222

#: The immediate-acknowledgement alternates the table headers list beside the
#: ordinary trimmed codes (for example "BOARD_LOT_IN_TR (20000) /
#: TRIMMED_BOARD_LOT_ACK_IN (20400)"). Unlike Futures & Options' Chapter 15,
#: this document gives them no separate structure or chapter of their own --
#: nothing beyond the alternate number -- so they are named here to be
#: refused rather than guessed at. See rules.NOT_IMPLEMENTED.
TRIMMED_BOARD_LOT_ACK_IN = 20400
TRIMMED_ORDER_MOD_ACK_IN = 20402
TRIMMED_ORDER_CANCEL_ACK_IN = 20404

# -- order and trade download, the recovery this protocol has instead of ------
# a resend request.

DOWNLOAD_REQUEST = 7000
HEADER_RECORD = 7011
MESSAGE_RECORD = 7021
TRAILER_RECORD = 7031

#: Every code above, by number, for naming one in a log or an audit entry.
NAMES = {}


def _name_the_codes():
    for name, value in sorted(globals().items()):
        if name.isupper() and isinstance(value, int) and name != "NAMES":
            NAMES.setdefault(value, name)


_name_the_codes()


#: The published error table. ``ErrorCode`` in the message header carries one of
#: these, and is zero on a message that reports no error.
ERROR_CODES = {
    16000: 'ERR_MARKET_NOT_OPEN',
    16001: 'ERR_INVALID_USER_TYPE',
    16003: 'ERR_BAD_TRANSACTION_CODE',
    16004: 'ERR_USER_ALREADY_SIGNED_ON',
    16006: 'ERR_INVALID_SIGNON',
    16007: 'ERR_SIGNON_NOT_POSSIBLE',
    16012: 'ERR_INVALID_SYMBOL',
    16013: 'ERR_INVALID_ORDER_NUMBER',
    16035: 'ERR_SECURITY_NOT_AVAILABLE',
    16041: 'ERR_INVALID_BROKER_OR_BRANCH',
    16042: 'ERR_USER_NOT_FOUND',
    16050: 'ERR_TRD_MOD_REJ_END_OF_DAY_PR',
    16053: 'ERR_PASSWORD_HAS_EXPIRED',
    16054: 'ERR_INVALID_BRANCH',
    16056: 'ERR_PROGRAM_ERROR',
    16098: 'ERR_INVALID_BUYER_USER_ID',
    16099: 'ERR_INVALID_SELLER_USER_ID',
    16100: 'ERR_INVALID_SYSTEM_VERSION',
    16104: 'ERR_SYSTEM_ERROR',
    16115: 'ERR_MOD_CAN_REJECT',
    16123: 'ERR_CANT_COMPLETE_YOUR_REQUES',
    16134: 'ERR_USER_IS_DISABLED',
    16148: 'ERR_INVALID_USER_ID',
    16154: 'ERR_INVALID_TRADER_ID',
    16169: 'ERR_ATO_IN_OPEN',
    16251: 'ERR_TRADE_MOD_DIFF_VOL',
    16273: 'ERR_NOT_FOUND',
    16278: 'ERR_MARKETS_CLOSED',
    16279: 'ERR_SECURITY_NOT_ADMITTED',
    16280: 'ERR_SECURITY_MATURED',
    16281: 'ERR_SECURITY_EXPELLED',
    16282: 'ERR_QUANTITY_EXCEEDS_ISSUED_CA',
    16283: 'ERR_PRICE_NOT_MULT_TICK_SIZE',
    16284: 'ERR_PRICE_EXCEEDS_DAY_MIN_MAX',
    16285: 'ERR_BROKER_NOT_ACTIVE',
    16307: 'ERR_QUANTITY_FREEZE_CANCELLED',
    16308: 'ERR_PRICE_FREEZE_CANCELLED',
    16311: 'ERR_SOLICITOR_PERIOD_OVER',
    16312: 'ERR_COMPETITIOR_PERIOD_OVER',
    16315: 'ERR_LIMIT_WORSE_TRIGGER',
    16316: 'ERR_TRG_PRICE_NOT_MULT_TICK_SI',
    16317: 'ERR_NO_AON_IN_LIMITS',
    16318: 'ERR_NO_MF_IN_LIMITS',
    16319: 'ERR_NO_AON_IN_SECURITY',
    16320: 'ERR_NO_MF_IN_SECURITY',
    16321: 'ERR_MF_EXCEEDS_DQ',
    16322: 'ERR_MF_NOT_MULT_BOARD_LOT',
    16323: 'ERR_MF_EXCEEDS_ORIGINAL_',
    16324: 'ERR_DQ_EXCEEDS_ORIGINAL_',
    16325: 'ERR_DQ_NOT_MULT_BOARD_LOT',
    16326: 'ERR_GTD_EXCEEDS_LIMIT',
    16328: 'ERR_QUANTITY_NOT_MULT_BOARD_L',
    16329: 'ERR_BROKER_NOT_PERMITTED_IN_M',
    16330: 'ERR_SECURITY_IS_SUSPENDED',
    16333: 'ERR_BRANCH_LIMIT_EXCEEDED',
    16348: 'ERR_TRADING_NOT_ALLOWED',
    16372: 'ERR_USER_TYPE_INQUIRY',
    16379: 'ERR_SOLICITION_NOT_ALLOWED',
    16383: 'ERR_AUCTION_FINISHED',
    16387: 'ERR_NO_TRADING_IN_SECURITY',
    16388: 'ERR_FOK_ORDER_CANCELLED',
    16392: 'ERR_TURNOVER_LIMIT_NOT_SET',
    16397: 'ERR_CANNOT_MOD_AUC_ORDER',
    16400: 'ERR_DQ_EXCEEDS_LIMIT',
    16403: 'ERR_WRONG_LOGIN_ADDRESS',
    16404: 'ERR_ADMIN_SUSP_CANCELLED',
    16411: 'ERR_INVALID_PRO_CLIENT',
    16412: 'ERR_INVALID_NEW_VOLUME',
    16413: 'ERR_INVALID_BUY_SELL',
    16414: 'ERR_INVALID_INST',
    16415: 'ERR_INVALID_ORDER_PARAM',
    16416: 'ERR_INVALID_CP_ID',
    16417: 'ERR_NNF_REQ_EXCEEDED',
    16418: 'ERR_INVALID_ORDER',
    16420: 'ERR_INVALID_ALPHA_CHAR',
    16421: 'ERR_TRADER_CANT_INIT_AUCTION',
    16422: 'ERR_INVALID_BOOK_TYPE',
    16423: 'ERR_INVALID_TRIGGER_PRICE',
    16424: 'ERR_INVALID_MSG_LENGTH',
    16425: 'ERR_INVALID_PARTICIPANT',
    16426: 'ERR_PARTICIPANT_AND_VOLUME_',
    16427: 'ERR_BROKER_SUSP_TRD_MOD_REJ',
    16493: 'ERR_FUNCTION_NOT_FOR_INQ_USER',
    16521: 'ERR_PRICE_OUTSIDE_REVISED_PRICE',
    16532: 'ERR_BR_BUY_ORD_VAL_LIMIT_EXCEE',
    16533: 'ERR_BR_SELL_ORD_VAL_LIMIT_EXCEE',
    16560: 'ERR_CANNOT_LOGOFF_SELF',
    16562: 'ERR_USER_ALREADY_SIGNED_OFF',
    16563: 'ERR_NO_PRIVILEGE_FOR_USER',
    16567: 'ERR_FRZ_REJECT_FOR_CLOSEOUT',
    16568: 'ERR_CLOSEOUT_NOT_ALLOWED',
    16569: 'ERR_CLOSEOUT_ORDER_REJECT',
    16571: 'ERR_CLOSEOUT_TRDMOD_REJECT',
    16576: 'ERR_MAX_UOVL_VALUE_EXCEEDED',
    16577: 'ERR_MAX_BOVL_VALUE_EXCEEDED',
    16588: 'ERR_USER_IP_REC_NOT_FOUND',
    16592: 'ERR_SYS_REJECT',
    16598: 'ERR_SEC_REJECT',
    16600: 'ERR_ORD_VAL_EXCEEDED',
    16601: 'ERR_PREOPEN_ORDER_REJECT',
    16606: 'ERR_INVALID_CLIENT',
    16700: 'ERR_',
    16750: 'ERR_ORD_LIM_EXCEEDS_SET_ORD_VA',
    16761: 'ERR_ACCNT_DISABLE_TRADING',
    16778: 'ERR_NEW_PWD_INVALID',
    16910: 'ERR_ACCNT_DISABLE_TRADING_FOR_',
    17015: 'ERR_STATUS_CHANGE_NOT_ALLOWED',
    17017: 'ERR_VOLUNTARY_CLOSEOUT_ORDR_R',
    17022: 'ERR_ACTV_NUM_OF_USRS_IN_BRNCH',
    17080: 'ERR_ORD_COULD_RESULT_IN_SELF_T',
    17102: 'ERR_HEARTBEAT_NOT_RECEIVED',
    17104: 'ERR_INVALID_BOX_ID',
    17105: 'ERR_SEQ_NUM_MISMATCH',
    17142: 'ERR_MAX_USR_LOGIN_EXCEEDED',
    17177: 'ERR_INVALID_PAN_ID',
    17179: 'ERR_INVALID_ALGO_ID',
    17180: 'ERR_INVALID_RESERVED_FILLER',
    17182: 'ERR_MKT_ORD_NOT_ALLOWED',
    17183: 'ERR_TRADE_BEYOND_MARKUP_PRICE',
    17184: 'ERR_USER_HAVING_NULL_RIGHTS',
    17702: 'ERR_CAS_',
    19028: 'ERR_CHECKSUM_FAILED_GR',
    19029: 'ERR_MULTIPLE_GR_QUERY_RCV',
    19030: 'ERR_ENCRYPTION_FLAG_MISMATCH',
    19031: 'ERR_MD5_CHECKSUM_FAILURE',
}

#: No error. Every outbound message that is not a refusal carries this.
NO_ERROR = 0


def error_name(code):
    """The published name of an error code, or its number as text."""
    return ERROR_CODES.get(code, str(code))
