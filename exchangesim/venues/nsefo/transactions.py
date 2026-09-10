"""Transaction codes and error codes, transcribed from the F&O appendix.

Scoped exactly as ``docs/specs/NSE_FO_TRANSCRIPTION.md`` scopes Phase 1: the
Regular Lot book of the Normal market, continuous trading, plus what a client
needs to log on, receive a system-information snapshot and receive trade
reports. Spread/2L/3L orders, Stop Loss/MIT, Negotiated Trade, give-up and the
broadcast feed are out of scope, and ``rules.NOT_IMPLEMENTED`` records the
refusal code for each.

The **"Trimmed" (``_TR``) order flow is served**, and it is not an optional
extra: a real gateway sends orders in it and nothing else. Chapter 11 says as
much -- "Only Trim-NNF protocol is supported by Direct Interface" -- so the
plain 316-byte structures and the compact ones are both accepted, and a client
is answered in whichever it asked in. What is *not* built is Chapter 15's
immediate-acknowledgement family (``TRIMMED_*_ACK_IN``), which is a separate
opt-in on a separate Gateway Router port.

**The single most dangerous property of this venue: ten transaction codes are
numerically identical to Capital Market's, and decode to a different, larger
structure.** ``BOARD_LOT_IN`` is ``2000`` at both venues, but Capital Market's
is a 290-byte ``ORDER_ENTRY_REQUEST`` and this venue's is a 316-byte
``MS_OE_REQUEST`` -- 26 bytes longer, throughout the whole order family:
``2000`` (``BOARD_LOT_IN``), ``2012`` (``PRICE_CONFIRMATION``), ``2040``
(``ORDER_MOD_IN``), ``2042`` (``ORDER_MOD_REJECT``), ``2070``
(``ORDER_CANCEL_IN``), ``2072`` (``ORDER_CANCEL_REJECT``), ``2073``
(``ORDER_CONFIRMATION``), ``2074`` (``ORDER_MOD_CONFIRMATION``), ``2075``
(``ORDER_CANCEL_CONFIRMATION``) and ``2231`` (``ORDER_ERROR``). A session that
somehow decoded one of these against the wrong venue's dictionary would frame
without error and mean something else entirely -- which is exactly why
``tests/test_nsefo_dictionary.py`` asserts the two dictionaries disagree on
every one of these ten by ``body_size``.

Several other codes collide the same way outside the order family: ``1600``/
``1601`` (system information, 44/106 bytes here against 40/94 at Capital
Market), ``2222`` (trade confirmation, 296 bytes against 228), ``2300``/
``2301`` (sign-on, 278 bytes against 276 -- the most consequential collision,
since logon is the very first exchange on the wire), ``2302`` (error
response, keyed by a bare contract token here rather than Capital Market's
Symbol+Series) and ``2321`` (sign-off response, carrying a payload here
against Capital Market's bare header). ``9002`` (``BATCH_ORDER_CANCEL``) is
*not* a collision -- Capital Market's own ``transactions.py`` does not define
it at all.

``PRICE_MOD_IN`` (``2013``) is new functionality with no Capital Market
analogue: an optimised structure that changes only an order's price without
resending the whole order. ``BOX_SIGN_OFF`` (``20322``) is likewise new --
plumbing for how a box learns why the exchange terminated its connection --
and is not present in Capital Market's ``transactions.py`` either, whether or
not it exists in that venue's own specification.

Only the **interactive** codes are here, for the same reason Capital Market's
module gives: this venue's market data is a separate UDP multicast feed,
broadcast codes are marked ``B`` in the appendix, and none of it is built.

``ERROR_CODES`` is the whole published table transcribed from the
transcription's §3 (the appendix "List of Error Codes", PDF p.256-269) rather
than the subset this venue can currently raise, for the same reason Capital
Market's module gives: it costs nothing, and a code the simulator never sends
is exactly the one somebody will be looking up. Symbolic IDs are preserved
**exactly as the appendix prints them** -- a mix of upper-case ``ERR_``/``OE_``
style and lower-case ``e$``/``E$``/``M$`` conventions used directly in the
source system -- rather than normalised to one style, because normalising
would invent a name the venue never used.

**Numeric value alone is not safe to key an error label from across venues.**
Several values agree in meaning between the two tables (``16000`` is "market
not open" at both, under different symbolic names), but at least one --
``16521`` -- means something different at each: Capital Market's is
``ERR_PRICE_OUTSIDE_REVISED_PRICE``; this venue's is ``e$not_clg_mem``, "not a
clearing member". Keep the two ``ERROR_CODES`` tables separate; never merge
them into one dict keyed only by the numeric code.
"""

# -- session and connection --------------------------------------------------

GR_REQUEST = 2400
GR_RESPONSE = 2401
SECURE_BOX_REGISTRATION_REQUEST_IN = 23008
SECURE_BOX_REGISTRATION_REQUEST_OUT = 23009
BOX_SIGN_ON_REQUEST_IN = 23000
BOX_SIGN_ON_REQUEST_OUT = 23001
#: Not in Capital Market's transactions.py at all. Sent by the exchange when
#: it terminates a box connection, carrying an error code explaining why.
BOX_SIGN_OFF = 20322
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
# Ten of these share one 316-byte structure, MS_OE_REQUEST -- the same ten
# transaction codes Capital Market shares across its own 290-byte
# ORDER_ENTRY_REQUEST, which is what makes them the safety list above.

BOARD_LOT_IN = 2000
PRICE_CONFIRMATION = 2012
ORDER_MOD_IN = 2040
ORDER_MOD_REJECT = 2042
ORDER_CANCEL_IN = 2070
ORDER_CANCEL_REJECT = 2072
ORDER_CONFIRMATION = 2073
ORDER_MOD_CONFIRMATION = 2074
ORDER_CANCEL_CONFIRMATION = 2075
ORDER_ERROR = 2231

#: New in this venue: an optimised price-only modification, PRICE_MOD
#: (106 bytes). "Volume will not be modified through this transcode."
PRICE_MOD_IN = 2013

#: Defined so a log line can name them; neither has a structure built in
#: Phase 1, matching Capital Market's own treatment of the same two codes.
FREEZE_TO_CONTROL = 2170
BATCH_ORDER_CANCEL = 9002

# -- trades ------------------------------------------------------------------

TRADE_CONFIRMATION = 2222

# -- the "trimmed" order flow ------------------------------------------------
#
# A second, more compact encoding of order entry, modification, cancellation
# and their answers -- appendix p.298-310. These do **not** use the forty-byte
# MESSAGE_HEADER at all: each opens with a compact prefix of its own. The
# naming pattern is the plain code with a `0` inserted after its first digit
# (2000 -> 20000, 2073 -> 20073), which is descriptive of the table rather
# than a rule the document states; every value here was read from it.
#
# This is what a real gateway sends. The plain 316-byte structures are served
# too, and both answer in kind.

BOARD_LOT_IN_TR = 20000
ORDER_MOD_IN_TR = 20040
ORDER_CANCEL_IN_TR = 20070
ORDER_CONFIRMATION_TR = 20073
ORDER_MOD_CONFIRMATION_TR = 20074
ORDER_CXL_CONFIRMATION_TR = 20075
TRADE_CONFIRMATION_TR = 20222

#: The quick cancel, which shares MS_OM_REQUEST_TR. No plain equivalent is
#: built here, so it is named to be refused rather than served.
ORDER_QUICK_CANCEL_IN_TR = 20060

#: Chapter 15's immediate-acknowledgement family: a member opts in by sending
#: these *instead of* the codes above, and gets a 22-byte MS_ACK_RESPONSE the
#: moment the order is received, ahead of the ordinary confirmation. It is a
#: separate feature on a separate Gateway Router port ("This new request must
#: be transmitted to the Exchange via a separate communication channel"), and
#: is not built -- see rules.NOT_IMPLEMENTED.
TRIMMED_BOARD_LOT_ACK_IN = 20400
TRIMMED_ORDER_MOD_ACK_IN = 20402
TRIMMED_ORDER_CANCEL_ACK_IN = 20404
PRICE_MOD_ACK_IN = 20406


# -- order and trade download, the recovery this protocol has instead of -----
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


#: The published error table (transcription §3, appendix p.256-269).
#: ``ErrorCode`` in the message header carries one of these, and is zero on a
#: message that reports no error.
ERROR_CODES = {
    293: 'INVALID_INSTRUMENT_TYPE',
    509: 'ORDER_NUMBER_INVALID',
    8049: 'ORD_CXL_INITIATOR_AUC_NOT_ALLOWED',
    8485: 'AUCTION_NUMBER_INVALID',
    16000: 'MARKET_CLOSED',
    16001: 'e$invalid_user',
    16003: 'ERROR_BAD_TRANS_CODE',
    16004: 'E$user_already_signed_on',
    16005: 'E$invalid_signoff',
    16006: 'E$invalid_signon',
    16007: 'e$signon_not_possible',
    16012: 'ERR_INVALID_SYMBOL',
    16013: 'ERR_INVALID_ORDER_NUMBER',
    16014: 'e$not_your_order',
    16015: 'E$not_your_fill',
    16016: 'E$invalid_fill_number',
    16019: 'E$stock_not_found',
    16020: 'e$order_price_out_of_revised_price_range',
    16035: 'SECURITY_NOT_AVAILABLE',
    16041: 'BROKER_NOT_FOUND',
    16042: 'USER_NOT_FOUND',
    16043: 'DUPLICATE_RECORD',
    16044: 'e$order_modified',
    16049: 'STOCK_SUSPENDED',
    16052: 'ERR_FUNCTION_NOT_AVAILABLE',
    16053: 'e$change_password',
    16054: 'ERR_INVALID_BRANCH',
    16055: 'PREOPEN_TRADE_CANCELLATION_NOT_ALLOWED',
    16056: 'OE_PROGRAM_ERROR',
    16063: 'ERR_INVALID_STATUS',
    16070: 'ERR_DATA_NOT_CHANGED',
    16086: 'e$dup_trd_cxl_request',
    16098: 'ERR_INVALID_BUYER_USER_ID',
    16099: 'ERR_INVALID_SELLER_USER_ID',
    16100: 'e$invalid_version',
    16104: 'OE_SYSTEM_ERROR',
    16134: 'ERR_USER_DISABLED',
    16145: 'OE_INVALID_STOCK_STATUS',
    16148: 'ERR_INVALID_USER_ID',
    16154: 'ERR_INVALID_TRADER_ID',
    16169: 'OE_ATO_IN_OPEN',
    16198: 'e$dup_request',
    16227: 'e$only_cp_allowed',
    16228: 'e$sl_mit_nt_not_allowed_pclose',
    16229: 'e$gtc_gtd_ord_not_allowed_pclose',
    16230: 'OE_CONT_MOD_NOT_ALLOWED',
    16231: 'TRD_CONT_MOD_NOT_ALLOWED',
    16233: 'STR_PRO_PARTIVIPANT_INVALID',
    16247: 'ERROR_INVALID_PRICE',
    16251: 'OE_DIFF_TRD_MOD_VOL',
    16260: 'ERROR_USER_NOT_EXISTS_IN_SYSTEM',
    16264: 'ERR_ALREADY_DELETED',
    16273: 'RECORD_NOT_FOUND',
    16278: 'OE_MARKETS_CLOSED',
    16279: 'OE_SECURITY_NOT_ADMITTED',
    16280: 'OE_SECURITY_MATURED',
    16281: 'OE_SECURITY_EXPELLED',
    16282: 'OE_ISSUED_CAP_EXCEEDS',
    16283: 'OE_PRICE_NOT_MULT',
    16284: 'OE_PRICE_EXCEEDS_DAY_MIN_MAX',
    16285: 'OE_IS_NOT_ACTIVE',
    16300: 'e$system_wrong_state',
    16303: 'OE_AUCTION_PENDING',
    16307: 'OE_QTY_FREEZE_CAN',
    16308: 'OE_PRICE_FREEZE_CAN',
    16311: 'OE_SOL_PERIOD_OVER',
    16312: 'OE_COMP_PERIOD_OVER',
    16313: 'OE_AUC_PERIOD_GREATER',
    16315: 'OE_LIMIT_TRIGGER',
    16316: 'OE_TRIGGER_PRICE_NOT_MULT',
    16317: 'OE_NO_AON_ATTRIB',
    16318: 'OE_NO_MF_ATTRIB',
    16319: 'OE_NO_AON_IN_ATTRIB1',
    16320: 'OE_NO_MF_ATTRIB1',
    16321: 'OE_MF_GREATER_DISC',
    16322: 'OE_MF_NOT_MULT',
    16323: 'OE_MF_GREATER_ORIGINAL',
    16324: 'OE_DISC_GREATER_ORIGINAL',
    16325: 'OE_DISC_NOT_MULT',
    16326: 'OE_GTD_GREATER',
    16327: 'OE_QUANTITY_GERATER_RL',
    16328: 'OE_QUANTITY_NOT_MULT_RL',
    16329: 'OE_BROKER_NOT_PERMITTED',
    16330: 'OE_IS_SUSPENDED',
    16333: 'OE_BRANCH_LIMIT_EXCEEDED',
    16343: 'OE_ORD_CAN_CHANGED',
    16344: 'OE_ORD_CANNOT_CANCEL',
    16345: 'OE_INIT_ORD_CANCEL',
    16346: 'OE_ORD_CANNOT_MODIFY',
    16348: 'ERR_TRADING_NOT_ALLOWED',
    16357: 'OE_NT_REJECTED',
    16363: 'CHG_ST_EXISTS',
    16369: 'OE_SECURITY_IN_PREOPEN',
    16372: 'OE_INQ_NOT_ALLOWED',
    16387: 'OE_SECURITY_INELIGIBLE',
    16388: 'e$fok_order_cancelled',
    16392: 'TURNOVER_LIMIT_NOT_PROVIDED',
    16397: 'ERR_CANNOT_MOD_AUC_ORDER',
    16400: 'OE_MAX_DQ_ALLOWED',
    16403: 'e$invalid_box_ip_combination',
    16404: 'OE_ADMIN_SUSP_CAN',
    16405: 'e$invalid_buy_sell_type',
    16406: 'e$invalid_book_type',
    16408: 'e$invalid_trigger_price',
    16414: 'e$invalid_pro_client',
    16415: 'e$invalid_instructions',
    16416: 'e$invalid_order_parameters',
    16418: 'e$nnf_req_exceeded',
    16419: 'INVALID_ORDER',
    16420: 'ERR_BOX_RATE_EXCEEDED_AT_MILLISECOND_LEVEL',
    16440: 'e$gtd_gt_maturity',
    16441: 'DQ_NOT_ALLOWED_IN_PREOPEN',
    16442: 'ST_ORD_NOT_ALLOWED_POPEN',
    16443: 'e$ord_lim_exceeds_ord_val_lim',
    16444: 'ERR_USR_ORD_VALUE_LIMIT_EXCEEDED',
    16445: 'SL_NOT_ALLOWED',
    16446: 'MIT_NOT_ALLOWED',
    16447: 'E$ord_not_allowed_in_preopen',
    16448: 'ERROR_SL_LMT_RSNBLTY_CHECK',
    16514: 'e$not_modifiable',
    16518: 'e$tm_cm_does_not_exist',
    16521: 'e$not_clg_mem',
    16523: 'e$user_not_corp_mgr',
    16532: 'e$pm_cm_invalid',
    16533: 'e$corp_mgr_vu_mod',
    16541: 'e$invalid_participant',
    16550: 'e$trade_approved_by_cm',
    16552: 'e$cm_stock_suspended',
    16554: 'e$broker_not_permitted_in_fut',
    16555: 'e$broker_not_permitted_in_opt',
    16556: 'e$qty_less_than_min_lot',
    16557: 'e$disc_qty_less_than_min_lot',
    16558: 'e$mf_qty_less_than_min_lot',
    16560: 'e$already_rejected',
    16561: 'e$nt_orders_not_allowed',
    16562: 'e$nt_trade_not_allowed',
    16566: 'e$inconsistent_broker_branch',
    16570: 'M$post_close_start',
    16571: 'M$post_close_ended',
    16572: 'M$post_close_trades',
    16573: 'e$invalid_msg_length',
    16574: 'e$invalid_open_close_type',
    16576: 'e$nnf_inq_req_exceeded',
    16577: 'e$participant_and_volume_changed',
    16578: 'e$invalid_cover_uncover_type',
    16580: 'e$illegal_participant',
    16581: 'e$invalid_fill_price',
    16583: 'e$pro_no_participant',
    16585: 'e$invalid_account_no',
    16586: 'e$allow_no_participant_order',
    16589: 'M$delete_all_orders',
    16597: 'e$cum_ur_ord_val_limit_exceeded',
    16598: 'e$branch_ord_val_limit_exceeded',
    16600: 'ERR_ORD_VAL_EXCEEDED',
    16601: 'ERR_PREOPEN_ORDER_REJECT',
    16602: 'e$dealer_value_limit_exceeds',
    16604: 'e$participant_not_found',
    16605: 'e$either_leg_failed',
    16606: 'e$qty_greater_than_freeze_qty',
    16607: 'e$spread_not_allowed',
    16609: 'e$spread_allowed_if_stock_open',
    16610: 'e$qty_should_be_same',
    16611: 'e$ord_mod_qty_frz_not_allowed',
    16612: 'e$trade_rec_modified',
    16615: 'e$tm_order_cant_be_modified',
    16616: 'e$tm_order_cant_be_cancelled',
    16617: 'e$tm_trade_cant_be_manipulated',
    16625: 'e$cm_of_tm_suspended',
    16626: 'e$expdate_not_in_ascending_ord',
    16627: 'e$invalid_contract_comb',
    16628: 'e$bm_cannot_cancel_cm_orders',
    16629: 'e$bm_cannot_cancel_bm_orders',
    16630: 'e$cm_cannot_cancel_cm_orders',
    16631: 'e$spread_in_different_underlying',
    16632: 'e$invalid_cli_ac',
    16636: 'e$br_ord_limit_fut_buy_exceeded',
    16637: 'e$br_ord_limit_fut_sell_exceeded',
    16638: 'e$br_ord_limit_opt_buy_exceeded',
    16639: 'e$br_ord_limit_opt_sell_exceeded',
    16640: 'e$ur_ord_limit_fut_buy_exceeded',
    16641: 'e$ur_ord_limit_fut_sell_exceeded',
    16642: 'e$ur_ord_limit_opt_buy_exceeded',
    16643: 'e$ur_ord_limit_opt_sell_exceeded',
    16645: 'e$cant_appr_bhav_copy_generated',
    16646: 'e$Collateral_Lmt_Chk',
    16656: 'e$address_not_found',
    16662: 'e$stk_in_popen',
    16666: 'e$invalid_nnf_field',
    16667: 'e$gtcgtd_not_allowed',
    16683: 'ERR_USER_ALREADY_SIGNED_OFF',
    16684: 'ERR_NO_PRIVILEGE',
    16686: 'CLOSEOUT_ORDER_REJECT',
    16687: 'CLOSEOUT_FRZ_REJECT',
    16688: 'CLOSEOUT_NOT_ALLOWED',
    16690: 'CLOSEOUT_TRDMOD_REJECT',
    16706: 'PARTIAL_ORDER_REJECT',
    16708: 'PARTIAL_QUICK_ORDER_CXL_REJ',
    16711: 'ERROR_INVALID_SPRD_COMBINATION',
    16713: 'e$price_diff_out_of_range',
    16725: 'RMS_REJECTED_IN_PREOPEN',
    16730: 'ERROR_ALGOID_NNFID_MISMATCH_1',
    16731: 'ERROR_ALGOID_NNFID_MISMATCH_2',
    16732: 'ERROR_ALGO_MKT_NOT_ALLOWED',
    16733: 'ERROR_INVALID_NNF_ID',
    16749: 'ERROR_PREOPN_ATO_MOD_CAN_REJ',
    16752: 'ERROR_PREOPN_ATO_NOT_ALLOWED',
    16778: 'ERR_USR_NOT_FOUND_IN_NNF_FILE',
    16793: 'e$vc_order_rejected',
    16794: 'e$ssd_order_rejected',
    16795: 'e$order_cancelled_for_vc',
    16796: 'e$order_cancelled_for_ssd',
    16797: 'MSG_CODE_VOLUNTARY_CLOSE_OUT_STATUS',
    16798: 'MSG_CODE_SUSPENDED_STATUS',
    16803: 'e$bo_price_out_of_range',
    16804: 'e$bo_excess_quantity',
    16805: 'e$user_ineligible_for_bulk_orders',
    16806: 'e$user_not_allowed_for_regular',
    16807: 'e$account_debarred',
    16810: 'ERR_USR_ALREADY_UNLCKED',
    16811: 'ERR_DUPLICATE_UNLCK_ALRT',
    16816: 'e$account_debarred_by_pit',
    17022: 'ERR_ACTV_NUM_OF_USRS_IN_BRNCH_EXCEEDED',
    17039: 'EC_TRD_MOD_REJ_CLI_CP_MOD_NOT_ALLOWED',
    17045: 'ERROR_QUANTITY_LIM_EXCEEDS_QTY_VAL_LIM',
    17046: 'USER_TRD_MOD_DISABLED',
    17063: 'ERR_DEPNDENT_SESSN_NOT_ACTIVE',
    # Two symbolic IDs are printed for one value in the appendix
    # (TPP/LPP execution-price bounds); the first is kept as the label.
    17070: 'e$trd_price_out_of_stock_tpp',
    17071: 'e$order_cancelled_for_self_trade',
    17101: 'e$invalid_packet',
    17102: 'e$hearbeat_not_received',
    17104: 'e$Invalid_box_id',
    17105: 'e$seq_no_mismatch',
    17106: 'e$box_rate_exceeded',
    17107: 'ERROR_HB_RATE_EXCEEDED',
    17142: 'e$max_user_count_exceeded',
    17177: 'ERR_INVALID_PAN_ID',
    17179: 'ERR_INVALID_ALGO_ID',
    17180: 'ERR_INVALID_VALUE_IN_RESERVED',
    17181: 'ERR_MKT_ORDER_NOT_ALLOWED',
    17182: 'ERR_TRADE_BEYOND_MARKUP_PRICE',
    17184: 'ERR_USER_HAVING_NULL_RIGHTS',
    17185: 'ERR_ALGO_ID_DISABLED',
    17186: 'ERR_ORDER_CANCELLED_ALGOID_DISABLED',
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
