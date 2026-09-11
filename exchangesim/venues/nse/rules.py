"""Mapping between NSE's vocabulary and the core's, and what NSE refuses.

Three kinds of thing live here. The value tables, which are the whole of the
translation between a wire value and a core enum. The refusals -- which books,
which order attributes and which flows this venue will not trade, each with the
published error code it answers with. And ``ASSUMPTIONS``, everything the
specification does not settle, reported by ``exsim venue.assumptions``.

**Rejections are numeric here, and that is a mapping rather than a core change.**
``core/enums.py:RejectReason`` is deliberately the simulator's own vocabulary,
"which a venue module maps onto its own codes" -- Japannext onto
``OrdRejReason(103)``, HKEX onto its own, NSE onto ``ErrorCode``. Where NSE
draws a distinction the core has no word for -- an odd lot, a minimum fill, a
stop-loss trigger -- the gateway refuses before the engine ever sees it and
names the code itself.
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

#: Market status, as ``SYSTEM_INFORMATION_OUT`` reports it. NSE has one more
#: state than the core's PRE_OPEN: "Preopen ended" is the locked window between
#: the close of order entry and the uncrossing, which is exactly what the core
#: calls OPENING_AUCTION.
STATE_TO_MARKET_STATUS = {
    TradingState.PRE_OPEN: D.MarketStatus.PRE_OPEN,
    TradingState.OPENING_AUCTION: D.MarketStatus.PRE_OPEN_ENDED,
    TradingState.OPEN: D.MarketStatus.OPEN,
    TradingState.CLOSED: D.MarketStatus.CLOSED,
    TradingState.HALTED: D.MarketStatus.CLOSED,
    TradingState.LUNCH_BREAK: D.MarketStatus.CLOSED,
    TradingState.CLOSING_AUCTION: D.MarketStatus.CLOSED,
}

#: ``ActivityType`` on a trade confirmation. The document lists several; only
#: the ordinary one is produced, since trade modification and cancellation are
#: not implemented.
ACTIVITY_TRADE = "NT"

#: HKEX taught the core that whether an acknowledgement precedes the executions
#: is a venue answer. NSE answers yes: ORDER_CONFIRMATION carries the
#: OrderNumber, and TRADE_CONFIRMATION references it, so a client that never
#: saw the confirmation would be told about a fill on an order it cannot name.
ACK_BEFORE_EXECUTION = True


# -- what this venue refuses -------------------------------------------------

#: The only book this venue trades. Everything else is refused outright rather
#: than promoted to a Regular Lot order, on the reasoning CLAUDE.md gives for
#: HKEX's odd lots: half-simulating a flow is worse than not having it.
SUPPORTED_BOOK = D.BookType.REGULAR_LOT

#: Why each other book is refused, and with which published error code.
UNSUPPORTED_BOOKS = {
    D.BookType.SPECIAL_TERMS: (
        16422, "the Special Terms book is not implemented"),
    D.BookType.STOP_LOSS: (
        16422, "the Stop Loss book is not implemented"),
    D.BookType.ODD_LOT: (
        16422, "the Odd Lot book is not implemented"),
    D.BookType.SPOT: (
        16422, "the Spot book is not implemented"),
    D.BookType.AUCTION: (
        16422, "the Auction book is not implemented"),
    D.BookType.CALL_AUCTION2: (
        16422, "Call Auction 2 is not implemented"),
}

#: Order attributes this venue refuses, each as
#: ``tag -> (error code, why)``. Every one is a flow the core could not honour
#: without pretending, so it is refused at the gateway before the engine sees
#: it -- exactly where HKEX refuses an odd lot.
UNSUPPORTED_ATTRIBUTES = (
    (D.FLAG_AON, 16319, "All Or None orders are not implemented"),
    (D.FLAG_MF, 16320, "Minimum Fill orders are not implemented"),
    (D.FLAG_ON_STOP, 16422,
     "stop-loss orders need the Stop Loss book, which is not implemented"),
    (D.FLAG_GTC, 16326,
     "Good Till Cancelled and Good Till Date are not implemented"),
)

#: ``ERR_INVALID_ORDER_PARAM``, the catch-all for an order this venue will not
#: take for a reason with no more specific code.
INVALID_ORDER_PARAM = 16415

#: What a core rejection becomes on the wire.
REJECT_TO_ERROR_CODE = {
    RejectReason.UNKNOWN_SYMBOL: 16012,             # ERR_INVALID_SYMBOL
    RejectReason.MARKET_CLOSED: 16000,              # ERR_MARKET_NOT_OPEN
    RejectReason.EXCEEDS_QUANTITY_LIMIT: 16282,     # QUANTITY_EXCEEDS_ISSUED_CA
    RejectReason.EXCEEDS_VALUE_LIMIT: 16600,        # ERR_ORD_VAL_EXCEEDED
    RejectReason.DUPLICATE_ORDER: 16418,            # ERR_INVALID_ORDER
    RejectReason.UNSUPPORTED_CHARACTERISTIC: 16415,
    RejectReason.INVALID_QUANTITY: 16328,           # NOT_MULT_BOARD_LOT
    RejectReason.INVALID_PRICE: 16415,
    RejectReason.PRICE_OUTSIDE_BAND: 16284,         # PRICE_EXCEEDS_DAY_MIN_MAX
    RejectReason.TICK_SIZE: 16283,                  # NOT_MULT_TICK_SIZE
    RejectReason.POST_ONLY_WOULD_CROSS: 16415,
    RejectReason.UNKNOWN_MPID: 16606,               # ERR_INVALID_CLIENT
    RejectReason.OTHER: 16415,
}

#: What a refused cancel or amend becomes.
CANCEL_REJECT_TO_ERROR_CODE = {
    CancelRejectReason.TOO_LATE_TO_CANCEL: 16013,   # ERR_INVALID_ORDER_NUMBER
    CancelRejectReason.UNKNOWN_ORDER: 16013,
    CancelRejectReason.ALREADY_PENDING: 16115,      # ERR_MOD_CAN_REJECT
    CancelRejectReason.DUPLICATE_CLORDID: 16418,
    CancelRejectReason.PRICE_OUTSIDE_BAND: 16284,
    CancelRejectReason.MISMATCHED_FIELD: 16115,
    CancelRejectReason.OTHER: 16115,
}

#: A dialect failure has no session-level Reject to travel in -- NNF has none --
#: so it becomes an ORDER_ERROR or a sign-on refusal carrying one of these.
SESSION_FAILURE_TO_ERROR_CODE = {
    0: 16003,       # invalid tag number      -> ERR_BAD_TRANSACTION_CODE
    2: 16003,       # undefined message type  -> ERR_BAD_TRANSACTION_CODE
    5: 16415,       # value is incorrect      -> ERR_INVALID_ORDER_PARAM
    6: 16415,       # incorrect data format
    11: 16003,      # invalid MsgType
}

#: The order status a report carries. NNF has no OrdStatus field -- what an
#: order's state *is* comes from the transaction code and the running totals --
#: so this exists only to decide which transaction code to send.
TERMINAL_STATUSES = frozenset((OrderStatus.FILLED, OrderStatus.CANCELLED,
                               OrderStatus.REJECTED, OrderStatus.EXPIRED))


def error_for(reason, table, default=INVALID_ORDER_PARAM):
    """The published error code for a core rejection reason."""
    return table.get(reason, default)


# -- the trimmed order flow --------------------------------------------------

#: A trimmed request against its plain twin. Both encodings are served, and a
#: client is answered in the one it asked in -- so this is read in both
#: directions rather than being a rewrite on the way in.
TRIMMED_REQUESTS = {
    X.BOARD_LOT_IN_TR: X.BOARD_LOT_IN,
    X.ORDER_MOD_IN_TR: X.ORDER_MOD_IN,
    X.ORDER_CANCEL_IN_TR: X.ORDER_CANCEL_IN,
}

#: What each plain response becomes for an order entered over the trimmed
#: structures. Unlike Futures & Options, this venue's own appendix publishes
#: a trimmed twin of every plain code, ORDER_ERROR and the two rejects
#: included, so the mapping is one code to one code and needs no ErrorCode
#: branching -- see F&O's rules.py for the venue where it does.
TRIMMED_RESPONSES = {
    X.ORDER_CONFIRMATION: X.ORDER_CONFIRMATION_TR,
    X.ORDER_MOD_CONFIRMATION: X.ORDER_MOD_CONFIRMATION_TR,
    X.ORDER_CANCEL_CONFIRMATION: X.ORDER_CXL_CONFIRMATION_TR,
    X.ORDER_ERROR: X.ORDER_ERROR_TR,
    X.ORDER_MOD_REJECT: X.ORDER_MOD_REJECT_TR,
    X.ORDER_CANCEL_REJECT: X.ORDER_CANCEL_REJECT_TR,
    X.PRICE_CONFIRMATION: X.PRICE_CONFIRMATION_TR,
    X.TRADE_CONFIRMATION: X.TRADE_CONFIRMATION_TR,
}

#: The reverse, for the message download: "a downloaded message is always a
#: non-trimmed message" -- a trimmed structure has no forty-byte header, so
#: it could not be wrapped in a MESSAGE_RECORD even in principle.
UNTRIMMED_RESPONSES = dict((tr, plain)
                           for plain, tr in TRIMMED_RESPONSES.items())


def plain_code(msg_type):
    """The plain transaction code a trimmed request carries, or None."""
    try:
        return TRIMMED_REQUESTS.get(int(msg_type))
    except (TypeError, ValueError):
        return None


def trimmed_code(code, trimmed):
    """Which of a response's two codes to answer with."""
    return TRIMMED_RESPONSES.get(code, code) if trimmed else code


def untrimmed(message):
    """The form of a message a message download replays.

    A trimmed response becomes its plain twin, by a direct lookup -- unlike
    F&O, this venue's trimmed responses need no ErrorCode branching, since a
    trimmed refusal already carries its own code. Anything else is already
    the right shape and is returned untouched -- and *not* copied, because
    the store keeps what it is given and nothing mutates a sent message.
    """
    try:
        plain = UNTRIMMED_RESPONSES.get(int(message.msg_type))
    except (TypeError, ValueError):
        return message
    if plain is None:
        return message
    recovered = message.copy()
    recovered.set(D.TRANSACTION_CODE, str(plain))
    return recovered


# -- order attributes from the flag bits -------------------------------------

def time_in_force(message):
    """The core time-in-force a message's flag bits ask for.

    NSE has no ``TimeInForce`` field: Day, IOC, GTC and the rest are bits of
    ``ST_ORDER_FLAGS``, and an order that sets none of them is a Day order,
    which is the default the document describes.
    """
    if message.get(D.FLAG_IOC) == "Y":
        return TimeInForce.IOC
    return TimeInForce.DAY


def order_type(message):
    """Limit unless the Market or ATO bit says otherwise.

    ``Mkt`` is a market order in continuous trading and ``ATO`` is its pre-open
    equivalent -- an order with no price, to be executed at whatever the
    uncrossing decides. The core has one concept for both, because to a book
    they are the same thing: an order with no price.
    """
    if message.get(D.FLAG_MARKET) == "Y" or message.get(D.FLAG_ATO) == "Y":
        return OrderType.MARKET
    return OrderType.LIMIT


def set_flags(message, order, preopen=False):
    """Write an order's characteristics back onto a report.

    Every bit is written, set or not, because the field travels either way and a
    client reads all sixteen of them.
    """
    market = order.order_type == OrderType.MARKET
    flags = {
        D.FLAG_ATO: market and preopen,
        D.FLAG_MARKET: market and not preopen,
        D.FLAG_ON_STOP: False,
        D.FLAG_DAY: order.time_in_force == TimeInForce.DAY,
        D.FLAG_GTC: False,
        D.FLAG_IOC: order.time_in_force in TimeInForce.IMMEDIATE,
        D.FLAG_AON: False,
        D.FLAG_MF: False,
        D.FLAG_MATCHED_IND: False,
        D.FLAG_TRADED: order.cum_qty > 0,
        D.FLAG_MODIFIED: order.orig_cl_ord_id is not None,
        D.FLAG_FROZEN: False,
        D.FLAG_PREOPEN: preopen,
        D.FLAG_STPC: False,
    }
    for tag, value in flags.items():
        message.set(tag, "Y" if value else "N")


# -- the daily price range ---------------------------------------------------

class CircuitFilter(object):
    """NSE's operating range, in the shape ``Instrument`` expects.

    The band is a **percentage of the base price**, and it is set per security
    rather than by a published price ladder: a scrip carries a 2, 5, 10 or 20
    per cent filter, and two securities at the same price can carry different
    ones. So it cannot be a row in a threshold table the way Japannext's bands
    are, and it is a small object with the same ``limits_for`` interface
    instead -- exactly what HKEX's multiplicative nine-times rule needed.

    Arithmetic is on integer price units throughout, so a twenty per cent band
    on 1543.25 is exact rather than 1234.5999999999999.
    """

    __slots__ = ("percent", "name")

    def __init__(self, percent, name=None):
        self.percent = int(percent)
        self.name = name or "%d%% circuit filter" % self.percent

    def band_for(self, base_price):
        return int(base_price) * self.percent // 100

    def limits_for(self, base_price):
        """The (low, high) limits around a base price.

        The lower limit is floored at one price unit, as every band table here
        is: a limit of zero or below is not a tradeable price.
        """
        band = self.band_for(base_price)
        return max(1, int(base_price) - band), int(base_price) + band

    def __len__(self):
        return 1


#: The bands NSE actually assigns. A security naming anything else is a
#: reference-data error rather than something to be quietly accepted.
PERMITTED_BANDS = (2, 5, 10, 20)


# -- limits ------------------------------------------------------------------

def default_limits(max_order_value=None, max_quantity=None):
    """What the standard validator enforces at this venue.

    ``require_round_lot`` is on, but the equity segment's board lot is one
    share, so it only ever bites on a security whose lot is larger.
    ``min_qty_requires_ioc`` is irrelevant here -- minimum fill is refused
    outright -- and is left at its default rather than turned off, so that
    turning MF on later does not silently inherit a permissive setting.
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


#: What a download answers when the stream it names is not one this venue
#: serves. Not an error on the wire: the member is told the stream is empty
#: and moves on to the next, which is what its loop is for.
DOWNLOAD_UNKNOWN_STREAM = 0

#: What a download must not replay. The download's own three answers,
#: because storing them would make a second download return the first one
#: wrapped in a third, and a third return that -- growing without bound and
#: telling the client nothing. And SYSTEM_INFORMATION_OUT, because it is the
#: answer to a system information *request* that a client makes once, at
#: logon, and sets up its streams from: it is not in Chapter 5's list, and a
#: real Futures & Options client replayed a second one out of the download
#: asserted and crashed. The two venues share the client code path.
NOT_RECOVERABLE = frozenset(str(code) for code in
                            (X.HEADER_RECORD, X.MESSAGE_RECORD,
                             X.TRAILER_RECORD, X.SYSTEM_INFORMATION_OUT))


def is_recoverable(msg_type):
    """Whether a message sent to a user can come back in a download.

    ASSUMPTION: everything but ``NOT_RECOVERABLE`` can. Chapter 5 lists what
    a download returns -- logon and logoff responses, interactive messages
    from NSE-Control, order and trade responses, trade confirmations, and a
    set of broadcasts -- but it reads as illustrative rather than closed.
    Excluding by that list instead would mean a message type added later is
    silently unrecoverable; excluding by name means the reverse, which is
    visible -- and it was, for system information, which is how it came to
    be named.
    """
    return str(msg_type) not in NOT_RECOVERABLE


ASSUMPTIONS = [
    "The trimmed (_TR) order flow is served alongside the plain 290-byte "
    "structures, and a client is answered in the encoding it asked in -- "
    "read off the *order* rather than off the request, so that a trade "
    "confirmation and a cancel on disconnect, which answer no request, "
    "still go back in the right one. Unlike Futures & Options, this venue's "
    "own appendix (Tables 57-60) publishes a trimmed ORDER_ERROR (20231), "
    "ORDER_MOD_REJECT (20042) and ORDER_CANCEL_REJECT (20072), so a refused "
    "trimmed order is answered with its own trimmed refusal code rather "
    "than a confirmation carrying a non-zero ErrorCode.",

    "The message download replays every message this venue sent a user "
    "except SYSTEM_INFORMATION_OUT, which a client sets its streams up from "
    "once at logon and asserts on a second time; bounded by 'nnf.recovery_capacity' (500) rather than by the trading "
    "day, and keyed on the header's TimeStamp1 in jiffies from 1980 -- the "
    "document gives that field's unit and never its origin. A client echoes "
    "the value back rather than reading it, so the choice is invisible to "
    "it; it matches the sibling nanosecond Timestamp field so one capture "
    "does not hold two epochs. Two further points came from a real client "
    "rather than the document: a record's inner header is the ordinary "
    "MESSAGE_HEADER, not the INNER_MESSAGE_HEADER Chapter 2 prescribes for "
    "download data, and a recovered message is always the non-trimmed "
    "form.",

    "SYSTEM_INFORMATION_OUT (1601) is sent only in answer to "
    "SYSTEM_INFORMATION_IN -- never on sign-on and never when the market "
    "changes state. A real Futures & Options client sets its streams up "
    "from it once and asserts on a second. The market-status broadcasts the "
    "document publishes for a state change -- BC_OPEN_MESSAGE (6511), "
    "BC_CLOSE_MESSAGE (6521), BC_PREOPEN_SHUTDOWN_MSG (6531), "
    "BC_NORMAL_MKT_PREOPEN_ENDED (6571) -- are unbuilt, so a client learns "
    "of a state change, the pre-open's lock included, only by asking.",

    "The Gateway Router's TLS version is configurable, and defaults to '1.3', "
    "which the specification requires and which the RHEL 8 target's Python "
    "supports. An interpreter linked against OpenSSL 1.0.2, such as this "
    "project's own Windows development box, cannot honour '1.3' at all, and "
    "asking for it there is refused at start-up rather than quietly served "
    "as something weaker. '1.2' is a minimum, not a pin: under OpenSSL 3.x a "
    "'1.2' context still negotiates 1.3 when both ends support it, so it is "
    "a floor for a box that cannot reach 1.3 rather than a ceiling anyone "
    "would choose in production.",

    "The Gateway Router's certificate is generated by the simulator and "
    "self-signed, matching the published methodology under which the "
    "exchange issues its own CA and distributes it as 'gr_ca_cert1.pem'. "
    "What the simulator cannot reproduce is the exchange's actual CA, so a "
    "member's client must be pointed at the generated file instead of a "
    "real one.",

    "The dynamic half of the cryptographic IV is laid out little-endian. It "
    "is the one value in this protocol that does not follow the wire's "
    "big-endian convention, because it never reaches the wire: the "
    "pseudocode (p.256) hands the cipher the address of a C struct -- 'char "
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

    "The heartbeat drop-counter threshold is 10. The document says a member "
    "connection is dropped when the counter 'reaches the threshold value set "
    "by the exchange' without naming it.",

    "SYSTEM_INFORMATION_DATA is 94 bytes. The appendix's summary table says "
    "90; Chapter 3's own table for the structure lists fields reaching 94. The "
    "detailed table is taken as authoritative.",

    "The password is not verified. A simulator holds no credential store, and "
    "SIGN_ON_REQUEST_IN is accepted on a configured User ID whatever password "
    "it carries.",

    "Order numbers are assigned sequentially from 1 rather than in NSE's own "
    "encoding, which the specification does not publish. They are unique, "
    "monotonic and fit the DOUBLE the wire carries, which is everything a "
    "client can rely on.",

    "The daily price range comes from the 'band' column of securities.csv, "
    "since NSE sets the operating range per scrip rather than by a published "
    "price ladder. price_bands.csv holds a single permissive fallback row.",

    "The tick is five paise at every price. NSE has revised the tick before, "
    "so it stays a table (reference/tick_sizes.csv) rather than a constant.",
]

#: Flows that are deliberately unbuilt, and refused rather than half-simulated.
#: Kept beside ASSUMPTIONS because a reader looking for one wants the other.
NOT_IMPLEMENTED = [
    "Every book but Regular Lot: Special Terms, Stop Loss, Odd Lot, Spot, "
    "Auction and Call Auction 2 are rejected with ERR_INVALID_BOOK_TYPE.",
    "All Or None and Minimum Fill orders, rejected with the security-level "
    "codes 16319 and 16320.",
    "Disclosed quantity: the core has no replenishment concept, and accepting "
    "the field while ignoring it would be the worst of both.",
    "Good Till Cancelled and Good Till Date.",
    "Trade modification and cancellation (5440/5445), and the freeze and "
    "approval flow (2170).",
    "The UDP multicast broadcast feed. It is LZO-compressed, and LZO cannot be "
    "written under this project's standard-library-only constraint. Market "
    "data is on the control plane, the CLI and the board instead.",
    "Market-wide index circuit breakers.",
    "The immediate-acknowledgement alternates the trimmed tables list beside "
    "the ordinary codes (TRIMMED_BOARD_LOT_ACK_IN 20400, "
    "TRIMMED_ORDER_MOD_ACK_IN 20402, TRIMMED_ORDER_CANCEL_ACK_IN 20404). "
    "This document gives them nothing beyond the number -- no response "
    "structure, no chapter -- so they are refused as unrecognised codes "
    "rather than answered in a guessed shape.",
]


def describe_transaction(code):
    """The published name of a transaction code, for a log line."""
    return X.NAMES.get(code, str(code))
