"""Mapping between F&O's vocabulary and the core's, and what F&O refuses.

The counterpart of :mod:`exchangesim.venues.nse.rules`, and built the same way:
value tables translating a wire value onto a core enum, the refusals -- which
books, which order attributes and which flows this venue will not trade, each
with the published error code it answers with -- and ``ASSUMPTIONS``, reported
by ``exsim venue.assumptions``. Read the docstring there first; this module
repeats its shape and departs from its numbers only where the two published
tables actually disagree, which ``docs/specs/NSE_FO_TRANSCRIPTION.md`` records
happens at least once (``16521`` means two different things at the two
venues) -- so every code below comes from **this venue's own** ``ERROR_CODES``
table (``transactions.ERROR_CODES``), never copied across from Capital
Market's numbering by assumption.
"""

from ...core.enums import (
    CancelRejectReason,
    Capacity,
    OrderStatus,
    OrderType,
    RejectReason,
    Side,
    TimeInForce,
    TradingState,
)
from ...core.validation import ValidationLimits
from . import dictionary as D
from . import transactions as X

# -- values ------------------------------------------------------------------

SIDE_TO_CORE = {
    D.BuySell.BUY: Side.BUY,
    D.BuySell.SELL: Side.SELL,
}
SIDE_TO_WIRE = dict((core, wire) for wire, core in SIDE_TO_CORE.items())

CAPACITY_TO_CORE = {
    D.ProClient.CLIENT: Capacity.AGENCY,
    D.ProClient.PRO: Capacity.PRINCIPAL,
}
CAPACITY_TO_WIRE = dict((core, wire) for wire, core in CAPACITY_TO_CORE.items())

#: Market status, as ``MS_SYSTEM_INFO_DATA`` reports it. F&O publishes a fifth
#: status CM does not have -- ``POST_CLOSE``, a genuine additional phase in
#: which only market orders are accepted -- and this slice does not run it or
#: the pre-open either (see ``NsefoVenue.trading_states``), so the map still
#: names both wire values (the maps stay total -- see ``Venue.trading_states``)
#: but no command can ever select the core phases that would produce them.
STATE_TO_MARKET_STATUS = {
    TradingState.PRE_OPEN: D.MarketStatus.PRE_OPEN,
    TradingState.OPENING_AUCTION: D.MarketStatus.PRE_OPEN_ENDED,
    TradingState.OPEN: D.MarketStatus.OPEN,
    TradingState.CLOSED: D.MarketStatus.CLOSED,
    TradingState.HALTED: D.MarketStatus.CLOSED,
    TradingState.LUNCH_BREAK: D.MarketStatus.CLOSED,
    TradingState.CLOSING_AUCTION: D.MarketStatus.CLOSED,
}

#: ``ActivityType`` on a trade confirmation. F&O's own full table
#: (transcription §2.5) has many more values than this simulator ever
#: produces; only the ordinary trade is rendered, exactly as Capital Market's
#: own ``ACTIVITY_TRADE`` is the one value its own renderer ever writes.
ACTIVITY_TRADE = D.ActivityType.ACTIVITY_TRADE

#: The acknowledgement precedes the executions here for the same reason as
#: Capital Market: ORDER_CONFIRMATION carries the OrderNumber, which is the
#: client's only handle on the order, so it must exist before a fill can be
#: reported against it.
ACK_BEFORE_EXECUTION = True


# -- what this venue refuses -------------------------------------------------

#: The only book this venue trades (transcription §2.3, Book ID 1).
SUPPORTED_BOOK = D.BookType.REGULAR_LOT

#: Why each other book is refused, and with which published error code
#: (transcription §7, "Out of scope, and its published refusal code").
#: Book 3 (Stop Loss / MIT) is *not* keyed here: it needs the more specific
#: ``SL_NOT_ALLOWED``/``MIT_NOT_ALLOWED`` codes, which depend on which of the
#: two ``ST_ORDER_FLAGS`` bits is set rather than on the book number alone --
#: see ``handlers._unsupported``. If a Book 3 order somehow carries neither
#: bit, the fallback entry below still refuses it.
UNSUPPORTED_BOOKS = {
    D.BookType.SPECIAL_TERMS: (
        16406, "the Special Terms book is not implemented"),         # e$invalid_book_type
    D.BookType.STOP_LOSS_OR_MIT: (
        16406, "book 3 combines Stop Loss and MIT; see FLAG_SL/FLAG_MIT "
               "for the specific refusal codes"),
    D.BookType.NEGOTIATED: (
        16561, "Negotiated Trade orders are not implemented"),       # e$nt_orders_not_allowed
    D.BookType.ODD_LOT: (
        16406, "the Odd Lot book is not implemented (and is itself "
               "unused by F&O, per the exchange's own appendix)"),
    D.BookType.SPOT: (
        16406, "the Spot book is not implemented (and is itself unused "
               "by F&O)"),
    D.BookType.AUCTION: (
        16406, "the Auction book is not implemented (and is itself "
               "unused as a market type by F&O)"),
}

#: The two order *types* Book 3 conflates, refused by their own bit rather
#: than by book number (transcription §7).
SL_NOT_ALLOWED = 16445
MIT_NOT_ALLOWED = 16446

#: Order attributes this venue refuses outright, each as
#: ``tag -> (error code, why)`` -- exactly where Capital Market refuses an odd
#: lot, before the engine ever sees the order.
UNSUPPORTED_ATTRIBUTES = (
    (D.FLAG_AON, 16319, "All Or None orders are not implemented"),       # OE_NO_AON_IN_ATTRIB1
    (D.FLAG_MF, 16320, "Minimum Fill orders are not implemented"),       # OE_NO_MF_ATTRIB1
    (D.FLAG_GTC, 16667, "Good Till Cancelled and Good Till Date are "
                        "not implemented"),                              # e$gtcgtd_not_allowed
)

#: A non-zero disclosed volume: the core has no replenishment concept, so this
#: is refused rather than silently ignored, exactly as Capital Market refuses it.
DISCLOSED_VOLUME_ERROR = 16400        # OE_MAX_DQ_ALLOWED

#: ``CounterPartyBrokerId`` is "valid only for Negotiated Trade Orders...for
#: other books, this field should be blank" (transcription §1.4) -- Negotiated
#: Trade is the give-up flow, and it is out of scope, so a populated field on
#: any other book is refused with the catch-all rather than silently carried.
GIVEUP_NOT_ALLOWED = 16415            # e$invalid_instructions

#: ``ADDITIONAL_ORDER_FLAGS.STPC`` (transcription §1.3) is F&O's self-trade
#: -prevention instruction, relocated from CM's ``ST_ORDER_FLAGS`` bit 15 to
#: its own structure here. This venue implements no self-trade prevention at
#: all -- see ``ASSUMPTIONS`` -- so an order asking for it is refused rather
#: than silently accepted and ignored. No error code specific to "self-trade
#: prevention is unavailable" was found in the published table, so this uses
#: the same catch-all as an invalid book/attribute combination; see
#: ASSUMPTIONS.
STPC_NOT_ALLOWED = 16415              # e$invalid_instructions

#: The Spread/2L/3L order family (``MS_SPD_OE_REQUEST``, transcription §1.4
#: intro and §4) has no layout in this package at all -- Phase 1 scoped the
#: whole 480-byte structure out, so a message using one of these codes decodes
#: header-only (``NnfCodec.decode`` falls back to the header when no layout is
#: registered) and is refused by transaction code alone. Two codes per §7:
#: the base spread family answers ``e$spread_not_allowed``, and the two
#: leg-count variants the transcription could find no distinct code for
#: answer the generic ``ERROR_BAD_TRANS_CODE`` instead of a guess.
SPREAD_NOT_ALLOWED = 16607            # e$spread_not_allowed
BAD_TRANSACTION_CODE = 16003          # ERROR_BAD_TRANS_CODE

SPREAD_CODE_ERRORS = {
    2100: SPREAD_NOT_ALLOWED,   # SP_BOARD_LOT_IN
    2106: SPREAD_NOT_ALLOWED,   # SP_ORDER_CANCEL_IN
    2118: SPREAD_NOT_ALLOWED,   # SP_ORDER_MOD_IN
    2102: BAD_TRANSACTION_CODE,  # TWOL_BOARD_LOT_IN -- no 2L-specific code found
    2104: BAD_TRANSACTION_CODE,  # THRL_BOARD_LOT_IN -- no 3L-specific code found
}

#: ``ERR_INVALID_ORDER_PARAM`` equivalent: the catch-all for an order this
#: venue will not take for a reason with no more specific code -- F&O's own
#: ``e$invalid_instructions``.
INVALID_ORDER_PARAM = 16415

#: What a core rejection becomes on the wire. Picked independently from this
#: venue's own ``ERROR_CODES`` table by matching each reason's published
#: English description -- not copied from Capital Market's numbering, which
#: the module docstring explains is unsafe (``16521`` is the proof).
REJECT_TO_ERROR_CODE = {
    RejectReason.UNKNOWN_SYMBOL: 16012,             # ERR_INVALID_SYMBOL
    RejectReason.MARKET_CLOSED: 16000,              # MARKET_CLOSED
    RejectReason.EXCEEDS_QUANTITY_LIMIT: 16282,     # OE_ISSUED_CAP_EXCEEDS
    RejectReason.EXCEEDS_VALUE_LIMIT: 16600,        # ERR_ORD_VAL_EXCEEDED
    RejectReason.DUPLICATE_ORDER: 16043,            # DUPLICATE_RECORD
    RejectReason.UNSUPPORTED_CHARACTERISTIC: 16415,  # e$invalid_instructions
    RejectReason.INVALID_QUANTITY: 16328,           # OE_QUANTITY_NOT_MULT_RL
    RejectReason.INVALID_PRICE: 16247,              # ERROR_INVALID_PRICE
    RejectReason.PRICE_OUTSIDE_BAND: 16284,         # OE_PRICE_EXCEEDS_DAY_MIN_MAX
    RejectReason.TICK_SIZE: 16283,                  # OE_PRICE_NOT_MULT
    RejectReason.POST_ONLY_WOULD_CROSS: 16415,
    # ASSUMPTION: F&O has no MPID-shaped field on order entry, only
    # AccountNumber/ProClient; e$invalid_pro_client is the nearest published
    # concept to "the venue does not recognise this client identity".
    RejectReason.UNKNOWN_MPID: 16414,               # e$invalid_pro_client
    RejectReason.OTHER: 16415,
}

#: What a refused cancel or amend becomes.
CANCEL_REJECT_TO_ERROR_CODE = {
    CancelRejectReason.TOO_LATE_TO_CANCEL: 16013,   # ERR_INVALID_ORDER_NUMBER
    CancelRejectReason.UNKNOWN_ORDER: 16013,
    CancelRejectReason.ALREADY_PENDING: 16346,      # OE_ORD_CANNOT_MODIFY
    CancelRejectReason.DUPLICATE_CLORDID: 16043,    # DUPLICATE_RECORD
    CancelRejectReason.PRICE_OUTSIDE_BAND: 16284,
    CancelRejectReason.MISMATCHED_FIELD: 16346,
    CancelRejectReason.OTHER: 16346,
}

#: A dialect failure has no session-level Reject to travel in -- NNF has none
#: -- so it becomes an ORDER_ERROR or a sign-on refusal carrying one of these.
SESSION_FAILURE_TO_ERROR_CODE = {
    0: 16003,       # invalid tag number      -> ERROR_BAD_TRANS_CODE
    2: 16003,       # undefined message type  -> ERROR_BAD_TRANS_CODE
    5: 16415,       # value is incorrect      -> e$invalid_instructions
    6: 16415,       # incorrect data format
    11: 16003,      # invalid MsgType
}

#: The order status a report carries. Exactly Capital Market's reasoning:
#: NNF has no OrdStatus field, so this exists only to decide which
#: transaction code to send next.
TERMINAL_STATUSES = frozenset((OrderStatus.FILLED, OrderStatus.CANCELLED,
                               OrderStatus.REJECTED, OrderStatus.EXPIRED))

#: The message download is refused outright (see ASSUMPTIONS/NOT_IMPLEMENTED),
#: same reasoning as Capital Market, with this venue's own generic
#: "function not available" code rather than CM's ``CANT_COMPLETE_YOUR_REQUEST``
#: (not present in F&O's own table).
DOWNLOAD_NOT_IMPLEMENTED = 16052      # ERR_FUNCTION_NOT_AVAILABLE


def error_for(reason, table, default=INVALID_ORDER_PARAM):
    """The published error code for a core rejection reason."""
    return table.get(reason, default)


# -- order attributes from the flag bits -------------------------------------

def time_in_force(message):
    """The core time-in-force a message's flag bits ask for.

    Exactly Capital Market's reasoning: Day, IOC and the rest are bits of
    ``ST_ORDER_FLAGS`` rather than a scalar field, and an order that sets none
    of them is a Day order (transcription §1.3, "Day=1 Day (default)").
    """
    if message.get(D.FLAG_IOC) == "Y":
        return TimeInForce.IOC
    return TimeInForce.DAY


def order_type(message):
    """Limit unless the Market or ATO bit says otherwise.

    ASSUMPTION: the transcription's own Order Terms/Attributes reading
    (§1.3, p.68-69) describes this venue's ``Market`` bit as inverted-sense --
    "``Market``\\=0 for a market order" -- against every other bit of the same
    structure, which is the ordinary "1 means set" convention. That literal
    reading is not adopted here: because a ``Flags`` field is zero until a
    sender explicitly sets it to ``Y`` (``layouts.py:_order_flags``, frozen),
    an inverted reading would make an *ordinary, unmarked limit order* decode
    as a market order by default on every single message this venue receives
    -- a far more dangerous misreading than assuming the bit follows the same
    "1 means set" convention as its fourteen neighbours in the same byte, none
    of which the transcription flags as inverted.

    The sibling document settles it rather than leaving this to judgement:
    Capital Market 6.6 p.57 states "For market order, Mkt bit will be set as 1
    in order response". Two published descriptions of the same bitfield, and
    only one of them can be read without making the default order type
    nonsense -- so the ``0`` is a slip in a table the extractor also scrambled
    (the neighbouring rows bleed into one another), not an inverted bit.
    """
    if message.get(D.FLAG_MARKET) == "Y" or message.get(D.FLAG_ATO) == "Y":
        return OrderType.MARKET
    return OrderType.LIMIT


def set_flags(message, order):
    """Write an order's characteristics back onto a report.

    Every bit is written, set or not, because the field travels either way.
    ``SL``/``MIT`` are always ``N``: this venue never accepts one (see
    ``UNSUPPORTED_BOOKS``/``SL_NOT_ALLOWED``/``MIT_NOT_ALLOWED``), so no order
    the engine holds can carry either. ``ADDITIONAL_ORDER_FLAGS`` are always
    ``N`` too: ``BOC`` has no confirmed meaning to reflect (transcription §6),
    ``COL`` (Cancel On Logoff) is not modelled as a per-order setting in this
    slice, and ``STPC`` is refused at entry (see ``STPC_NOT_ALLOWED``) so no
    resting order can carry it either.
    """
    market = order.order_type == OrderType.MARKET
    flags = {
        D.FLAG_ATO: False,             # no pre-open in this slice
        D.FLAG_MARKET: market,
        D.FLAG_SL: False,
        D.FLAG_MIT: False,
        D.FLAG_DAY: order.time_in_force == TimeInForce.DAY,
        D.FLAG_GTC: False,
        D.FLAG_IOC: order.time_in_force in TimeInForce.IMMEDIATE,
        D.FLAG_AON: False,
        D.FLAG_MF: False,
        D.FLAG_MATCHED_IND: False,
        D.FLAG_TRADED: order.cum_qty > 0,
        D.FLAG_MODIFIED: order.orig_cl_ord_id is not None,
        D.FLAG_FROZEN: False,
        D.FLAG_PREOPEN: False,
    }
    for tag, value in flags.items():
        message.set(tag, "Y" if value else "N")
    for tag in (D.FLAG_BOC, D.FLAG_COL, D.FLAG_STPC_ADDITIONAL):
        message.set(tag, "N")


# -- the daily price range ---------------------------------------------------

class CircuitFilter(object):
    """F&O's operating range, in the shape ``Instrument`` expects.

    F&O's own copy of the *approach* ``venues/nse/rules.py:CircuitFilter``
    takes -- a percentage of the base price, set per contract in
    ``reference/contracts.csv``'s ``band_pct`` column rather than read off a
    published ladder -- but independently defined, since F&O's contracts are a
    different reference-data universe from Capital Market's securities and the
    two must never share a Python object a reference-data edit at one venue
    could silently affect the other through.
    """

    __slots__ = ("percent", "name")

    def __init__(self, percent, name=None):
        self.percent = int(percent)
        if self.percent <= 0:
            raise ValueError("a circuit filter percentage must be positive")
        self.name = name or "%d%% circuit filter" % self.percent

    def band_for(self, base_price):
        return int(base_price) * self.percent // 100

    def limits_for(self, base_price):
        """The (low, high) limits around a base price.

        The lower limit is floored at one price unit, as every band table
        here is: a limit of zero or below is not a tradeable price.
        """
        band = self.band_for(base_price)
        return max(1, int(base_price) - band), int(base_price) + band

    def __len__(self):
        return 1


# -- limits ------------------------------------------------------------------

def default_limits(max_order_value=None, max_quantity=None):
    """What the standard validator enforces at this venue.

    Exactly Capital Market's shape: Day and IOC are the only supported
    time-in-force values (GTC is refused before the engine ever sees it), and
    every book but Regular Lot is refused for the same reason, before
    quantity or price are even looked at.
    """
    return ValidationLimits(
        max_order_value=max_order_value,
        max_quantity=max_quantity,
        require_round_lot=True,
        require_tick=True,
        enforce_price_band=True,
        allowed_order_types=(OrderType.LIMIT, OrderType.MARKET),
        allowed_time_in_force=(TimeInForce.DAY, TimeInForce.IOC),
    )


ASSUMPTIONS = [
    "The dynamic half of the cryptographic IV is laid out little-endian. It "
    "is the one value in this protocol that does not follow the wire's "
    "big-endian convention, because it never reaches the wire: the "
    "pseudocode (F&O 9.50 p.315) hands the cipher the address of a C struct -- 'char "
    "caStaticIv[8]; long long lDynamicIv;' -- so those eight bytes are laid "
    "out the member's host way. What is left open is whether a member "
    "byte-swaps the Gateway Router's LONG LONG out of the response before "
    "storing it, as every other numeric field in this protocol requires. So "
    "the exchange issues that field as zero, the one value at which both "
    "readings coincide, and 'gateway_router.dynamic_iv' defaults to 'auto': "
    "the layout is settled by whichever reading authenticates the first "
    "message a box sends and pinned for the rest of the connection. Set it "
    "to 'little' or 'big' to refuse the other outright.",

    "The counter in that IV rises for member-to-exchange traffic and falls "
    "for exchange-to-member traffic. The document gives the member two "
    "separate copies and states only the member's rule -- increment before "
    "encryption, decrement before decryption -- leaving the exchange to "
    "mirror it. Two sequences walking away from one origin is the only "
    "reading that both matches the text and keeps GCM's requirement that an "
    "IV never repeat under a key.",

    "The Gateway Router's certificate is generated by the simulator and "
    "self-signed, matching the published methodology under which the "
    "exchange issues its own CA and distributes it as 'gr_ca_cert1.pem'. "
    "What the simulator cannot reproduce is the exchange's actual CA, so a "
    "member's client must be pointed at the generated file (var/tls-fo/) "
    "instead of a real one.",

    "BuySell, ProClient, ModCxlBy and UserType value domains are carried over "
    "from Capital Market's own values (dictionary.py), on the reading that the "
    "two documents use the same role and buy/sell vocabulary -- every page "
    "describing these domains directly had its value list elided by the PDF "
    "extractor. Unverified; see docs/specs/NSE_FO_TRANSCRIPTION.md §6.",

    "The ST_ORDER_FLAGS 'Market' bit is treated as the ordinary '1 means set' "
    "convention (Y = market order). The F&O Order Terms table prints "
    "'Market | 0 | Market order' where all fourteen neighbouring bits print 1, "
    "and read literally that would make every unmarked limit order decode as a "
    "market order, since the field is zero until something sets it. The "
    "Capital Market document settles it in the other direction and in so many "
    "words -- 'For market order, Mkt bit will be set as 1 in order response' "
    "(CM 6.6 p.57) -- so the 0 is taken as a slip in a table the extractor also "
    "scrambled, not as an inverted bit.",

    "ADDITIONAL_ORDER_FLAGS.STPC (self-trade-prevention-cancel) is refused "
    "outright on order entry: this venue implements no self-trade prevention "
    "at all, for the same reason Capital Market has none -- the core's mode "
    "keys on 'mpid', which means something else here. No error code specific "
    "to 'self-trade prevention unavailable' was found in the published table; "
    "the generic e$invalid_instructions (16415) is used instead.",

    "CounterPartyBrokerId populated on a Regular Lot order is refused with "
    "the same generic catch-all (16415): the field is documented as valid "
    "only for Negotiated Trade (give-up) orders, which are out of scope, so a "
    "value on any other book indicates a flow this simulator does not carry.",

    "Order numbers are assigned sequentially from a configurable base, exactly "
    "as Capital Market's OrderNumberGenerator -- the specification does not "
    "publish how a real exchange composes one.",

    "The password is not verified at sign-on, for the same reason as Capital "
    "Market: a simulator holds no credential store.",

    "TokenNo is not cross-validated against CONTRACT_DESC: this simulator has "
    "no token-number reference data, so a contract is always resolved from "
    "CONTRACT_DESC alone, matching the natural reading "
    "docs/specs/NSE_FO_TRANSCRIPTION.md §1.4 describes for a zero/absent "
    "TokenNo.",

    "The daily price range and tick size are both per-contract "
    "(reference/contracts.csv's band_pct and tick columns), since F&O has no "
    "published price ladder the way Capital Market's tick_sizes.csv "
    "transcribes one -- every contract in this simulator's universe carries "
    "its own flat tick and its own circuit filter.",
]

#: Flows that are deliberately unbuilt, and refused rather than half-simulated.
NOT_IMPLEMENTED = [
    "The pre-open and Postclose phases (transcription §2.2): both are "
    "published market statuses this simulator does not enter. The pre-open "
    "needs its own uncrossing rule set the way Capital Market's does, and "
    "Postclose is a genuine fifth phase (market orders only, RL/ST books) "
    "with no analogue anywhere else in this project. Neither is mapped onto "
    "a phase this venue does run; both are simply not offered by "
    "'trading_states', and 'state.set' refuses them plainly.",

    "Every book but Regular Lot: Special Terms and the unused Odd Lot/Spot/"
    "Auction books are rejected with e$invalid_book_type (16406); Stop Loss "
    "and Market If Touched orders (Book 3) are rejected with their own codes, "
    "SL_NOT_ALLOWED (16445) and MIT_NOT_ALLOWED (16446); Negotiated Trade "
    "(Book 4, the give-up flow) is rejected with e$nt_orders_not_allowed "
    "(16561).",

    "The Spread/2L/3L order family (transaction codes 2100-2136, "
    "MS_SPD_OE_REQUEST): no layout exists for this 480-byte structure at all "
    "(Phase 1 scoped it out), so a message using one of these codes decodes "
    "header-only and is refused by transaction code, per "
    "rules.SPREAD_CODE_ERRORS.",

    "All Or None and Minimum Fill orders (16319, 16320), disclosed quantity "
    "(16400), and Good Till Cancelled/Good Till Date (16667, using this "
    "venue's own e$gtcgtd_not_allowed code) -- the core has no replenishment "
    "or resting-order-past-today concept, and half-simulating one would be "
    "worse than refusing it.",

    "PRICE_MOD (2013), the optimised price-only modification: not built in "
    "this phase. A client changes an order's price through the ordinary "
    "ORDER_MOD_IN (2040) instead; a message naming 2013 is refused as an "
    "unrecognised transaction code, same as any other code this simulator "
    "does not implement.",

    "Trade modification and cancellation, the freeze/approval workflow, and "
    "give-up/Interactive Give-Up: never generated, and never producible by a "
    "client this simulator serves -- see docs/specs/NSE_FO_TRANSCRIPTION.md "
    "§7 for why none of these has a client-facing refusal beyond the general "
    "catch-all.",

    "The order and trade download (7000/7011/7021/7031), refused with "
    "ERR_FUNCTION_NOT_AVAILABLE (16052) for the same reason as Capital "
    "Market: every structure here is fixed width, and a MESSAGE_RECORD is "
    "not.",

    "The UDP multicast broadcast feed: LZO-compressed, and LZO cannot be "
    "written under this project's standard-library-only constraint. Market "
    "data is on the control plane and the CLI instead.",
]


def describe_transaction(code):
    """The published name of a transaction code, for a log line."""
    return X.NAMES.get(code, str(code))
