"""Domain enumerations for the venue-agnostic core.

These are the simulator's own vocabulary, deliberately not FIX values: a venue
module maps between them and its wire representation. That is what lets one
matching engine serve exchanges whose protocols disagree about spelling.

Values are plain strings so that control-plane JSON and log lines are readable
without a lookup table.
"""


class Side(object):
    BUY = "BUY"
    SELL = "SELL"
    SELL_SHORT = "SELL_SHORT"
    SELL_SHORT_EXEMPT = "SELL_SHORT_EXEMPT"

    #: Every side that removes from the bid and adds to the offer.
    SELLING = frozenset((SELL, SELL_SHORT, SELL_SHORT_EXEMPT))
    ALL = frozenset((BUY, SELL, SELL_SHORT, SELL_SHORT_EXEMPT))

    @staticmethod
    def is_buy(side):
        return side == Side.BUY

    @staticmethod
    def opposite(side):
        return Side.SELL if side == Side.BUY else Side.BUY


class OrderType(object):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class TimeInForce(object):
    DAY = "DAY"
    IOC = "IOC"
    FOK = "FOK"

    #: Time-in-force values that never rest on the book.
    IMMEDIATE = frozenset((IOC, FOK))


class OrderStatus(object):
    """Order lifecycle states, aligned with FIX OrdStatus(39) semantics."""

    PENDING_NEW = "PENDING_NEW"
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REPLACED = "REPLACED"
    PENDING_CANCEL = "PENDING_CANCEL"
    PENDING_REPLACE = "PENDING_REPLACE"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"

    #: States in which an order may still trade.
    LIVE = frozenset((NEW, PARTIALLY_FILLED, PENDING_CANCEL, PENDING_REPLACE))
    #: States from which no further transition is possible.
    TERMINAL = frozenset((FILLED, CANCELLED, REPLACED, REJECTED, EXPIRED))


class ExecInst(object):
    """Order-handling instructions the core understands."""

    POST_ONLY = "POST_ONLY"
    IGNORE_NOTIONAL_CHECK = "IGNORE_NOTIONAL_CHECK"
    #: Suppress the price-band check, where the venue lets a client override it
    #: (HKEX ``ExecInst(18)=c``, "ignore price validity checks").
    IGNORE_PRICE_CHECK = "IGNORE_PRICE_CHECK"


class Capacity(object):
    AGENCY = "AGENCY"
    PRINCIPAL = "PRINCIPAL"


class Liquidity(object):
    """Whether a fill added liquidity (rested) or removed it (aggressed)."""

    ADDED = "ADDED"
    REMOVED = "REMOVED"


class StpMode(object):
    """Self-trade prevention modes, per JNX_Self-Trade_Prevention_2.00.

    Orders are grouped by MPID. On a would-be self-match:

    ``CANCEL_NEWEST``
        The incoming order's whole remaining balance is cancelled back to its
        owner and matching stops. The resting order is untouched.
    ``CANCEL_OLDEST``
        The resting order's remaining balance is cancelled and the incoming
        order resumes matching against the rest of the book.
    ``DECREMENT``
        Equal sizes cancel both. Otherwise the smaller is cancelled and the
        larger is reduced by the smaller's size, then matching resumes.
    """

    NONE = "NONE"
    CANCEL_NEWEST = "CANCEL_NEWEST"
    CANCEL_OLDEST = "CANCEL_OLDEST"
    DECREMENT = "DECREMENT"

    ALL = frozenset((NONE, CANCEL_NEWEST, CANCEL_OLDEST, DECREMENT))


class TradingState(object):
    """Market phases.

    The core supports the full set so that venues with call auctions and lunch
    breaks are expressible. Japannext uses only HALTED, OPEN and CLOSED --
    its rules state plainly that it runs no opening or closing auctions.
    """

    PRE_OPEN = "PRE_OPEN"
    OPENING_AUCTION = "OPENING_AUCTION"
    OPEN = "OPEN"
    LUNCH_BREAK = "LUNCH_BREAK"
    CLOSING_AUCTION = "CLOSING_AUCTION"
    CLOSED = "CLOSED"
    HALTED = "HALTED"

    ALL = frozenset((PRE_OPEN, OPENING_AUCTION, OPEN, LUNCH_BREAK,
                     CLOSING_AUCTION, CLOSED, HALTED))

    #: States in which continuous matching occurs.
    CONTINUOUS = frozenset((OPEN,))
    #: States that accept orders onto the book without matching them.
    ACCUMULATING = frozenset((PRE_OPEN, OPENING_AUCTION, CLOSING_AUCTION))
    #: States that accept no new orders at all.
    NO_ENTRY = frozenset((CLOSED, HALTED, LUNCH_BREAK))


class RejectReason(object):
    """Why an order was refused, in core terms.

    A venue module maps these onto its own codes -- for Japannext, onto
    ``OrdRejReason(103)``.
    """

    UNKNOWN_SYMBOL = "UNKNOWN_SYMBOL"
    MARKET_CLOSED = "MARKET_CLOSED"
    EXCEEDS_QUANTITY_LIMIT = "EXCEEDS_QUANTITY_LIMIT"
    EXCEEDS_VALUE_LIMIT = "EXCEEDS_VALUE_LIMIT"
    DUPLICATE_ORDER = "DUPLICATE_ORDER"
    UNSUPPORTED_CHARACTERISTIC = "UNSUPPORTED_CHARACTERISTIC"
    INVALID_QUANTITY = "INVALID_QUANTITY"
    INVALID_PRICE = "INVALID_PRICE"
    PRICE_OUTSIDE_BAND = "PRICE_OUTSIDE_BAND"
    TICK_SIZE = "TICK_SIZE"
    POST_ONLY_WOULD_CROSS = "POST_ONLY_WOULD_CROSS"
    UNKNOWN_MPID = "UNKNOWN_MPID"
    OTHER = "OTHER"


class CancelRejectReason(object):
    """Why a cancel or replace request was refused."""

    TOO_LATE_TO_CANCEL = "TOO_LATE_TO_CANCEL"
    UNKNOWN_ORDER = "UNKNOWN_ORDER"
    ALREADY_PENDING = "ALREADY_PENDING"
    DUPLICATE_CLORDID = "DUPLICATE_CLORDID"
    PRICE_OUTSIDE_BAND = "PRICE_OUTSIDE_BAND"
    MISMATCHED_FIELD = "MISMATCHED_FIELD"
    OTHER = "OTHER"


class CancelReason(object):
    """Why an order left the book.

    Distinguishes solicited cancels from the unsolicited ones a venue must
    report with a restatement reason.
    """

    USER_REQUEST = "USER_REQUEST"
    IOC_REMAINDER = "IOC_REMAINDER"
    #: A market order's unfilled balance: it has no price, so it cannot rest.
    MARKET_REMAINDER = "MARKET_REMAINDER"
    FOK_UNFILLED = "FOK_UNFILLED"
    MIN_QTY_UNMET = "MIN_QTY_UNMET"
    SELF_TRADE_PREVENTION = "SELF_TRADE_PREVENTION"
    MARKET_CLOSED = "MARKET_CLOSED"
    CANCEL_ON_DISCONNECT = "CANCEL_ON_DISCONNECT"
    ADMINISTRATIVE = "ADMINISTRATIVE"
    #: A single request that removed many orders at once, so each individual
    #: order's owner learns of it without having asked for that order by name.
    MASS_CANCEL = "MASS_CANCEL"
    REPLACED = "REPLACED"

    #: Cancels the client did not ask for, which venues report unsolicited.
    UNSOLICITED = frozenset((SELF_TRADE_PREVENTION, MARKET_CLOSED,
                             CANCEL_ON_DISCONNECT, ADMINISTRATIVE,
                             MASS_CANCEL))
