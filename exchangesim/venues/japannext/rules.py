"""Translation between core vocabulary and Japannext FIX values.

Every mapping here is a statement about the venue, so each one is traceable to
a line in the specification. Where the specification is silent, the choice is
marked ``ASSUMPTION`` -- those are the behaviours to confirm against the real
test environment, and they are collected in :data:`ASSUMPTIONS` so the venue can
report them over the control plane rather than leaving them buried in code.
"""

from ...core.enums import (
    Capacity,
    CancelRejectReason,
    CancelReason,
    ExecInst,
    Liquidity,
    OrderStatus,
    RejectReason,
    Side,
    TimeInForce,
    TradingState,
)
from ...core.validation import ValidationLimits
from . import dictionary as D

# -- side --------------------------------------------------------------------

SIDE_TO_CORE = {
    D.SideValue.BUY: Side.BUY,
    D.SideValue.SELL: Side.SELL,
    D.SideValue.SELL_SHORT: Side.SELL_SHORT,
    D.SideValue.SELL_SHORT_EXEMPT: Side.SELL_SHORT_EXEMPT,
}
SIDE_TO_FIX = dict((core, fix) for fix, core in SIDE_TO_CORE.items())

# -- time in force -----------------------------------------------------------

TIF_TO_CORE = {
    D.TimeInForce.DAY: TimeInForce.DAY,
    D.TimeInForce.IOC: TimeInForce.IOC,
    D.TimeInForce.FOK: TimeInForce.FOK,
}
TIF_TO_FIX = dict((core, fix) for fix, core in TIF_TO_CORE.items())

# -- capacity ----------------------------------------------------------------

CAPACITY_TO_CORE = {
    D.Rule80A.AGENCY: Capacity.AGENCY,
    D.Rule80A.PRINCIPAL: Capacity.PRINCIPAL,
}
CAPACITY_TO_FIX = dict((core, fix) for fix, core in CAPACITY_TO_CORE.items())

# -- execution instructions --------------------------------------------------

EXEC_INST_TO_CORE = {
    D.ExecInstValue.POST_ONLY: ExecInst.POST_ONLY,
    D.ExecInstValue.IGNORE_NOTIONAL: ExecInst.IGNORE_NOTIONAL_CHECK,
}

# -- order status ------------------------------------------------------------

STATUS_TO_FIX = {
    OrderStatus.PENDING_NEW: D.OrdStatus.PENDING_NEW,
    OrderStatus.NEW: D.OrdStatus.NEW,
    OrderStatus.PARTIALLY_FILLED: D.OrdStatus.PARTIALLY_FILLED,
    OrderStatus.FILLED: D.OrdStatus.FILLED,
    OrderStatus.CANCELLED: D.OrdStatus.CANCELED,
    OrderStatus.REPLACED: D.OrdStatus.REPLACED,
    OrderStatus.PENDING_CANCEL: D.OrdStatus.PENDING_CANCEL,
    OrderStatus.PENDING_REPLACE: D.OrdStatus.PENDING_REPLACE,
    OrderStatus.REJECTED: D.OrdStatus.REJECTED,
    # The dialect has no Expired value; an expired order reads as cancelled.
    OrderStatus.EXPIRED: D.OrdStatus.CANCELED,
}

# -- liquidity ---------------------------------------------------------------

LIQUIDITY_TO_FIX = {
    Liquidity.ADDED: D.LastLiquidityInd.ADDED,
    Liquidity.REMOVED: D.LastLiquidityInd.REMOVED,
}

# -- trading state -----------------------------------------------------------
#
# Japannext publishes only Halted, Open and Closed. The core's auction and
# lunch-break phases exist for other venues, so they are folded onto the
# nearest value a Japannext client can understand.

STATE_TO_TRAD_SES_STATUS = {
    TradingState.OPEN: D.TradSesStatus.OPEN,
    TradingState.CLOSED: D.TradSesStatus.CLOSED,
    TradingState.HALTED: D.TradSesStatus.HALTED,
    TradingState.PRE_OPEN: D.TradSesStatus.HALTED,
    TradingState.OPENING_AUCTION: D.TradSesStatus.HALTED,
    TradingState.CLOSING_AUCTION: D.TradSesStatus.HALTED,
    TradingState.LUNCH_BREAK: D.TradSesStatus.HALTED,
}

# -- order rejection ---------------------------------------------------------

REJECT_TO_ORD_REJ_REASON = {
    RejectReason.UNKNOWN_SYMBOL: D.OrdRejReason.UNKNOWN_SYMBOL,          # 1
    RejectReason.MARKET_CLOSED: D.OrdRejReason.VENUE_CLOSED,             # 2
    RejectReason.EXCEEDS_QUANTITY_LIMIT: D.OrdRejReason.EXCEEDS_LIMIT,   # 3
    RejectReason.EXCEEDS_VALUE_LIMIT: D.OrdRejReason.EXCEEDS_LIMIT,      # 3
    RejectReason.DUPLICATE_ORDER: D.OrdRejReason.DUPLICATE_ORDER,        # 6
    RejectReason.UNSUPPORTED_CHARACTERISTIC:
        D.OrdRejReason.UNSUPPORTED_CHARACTERISTIC,                       # 11
    RejectReason.INVALID_QUANTITY: D.OrdRejReason.INCORRECT_QUANTITY,    # 13
    RejectReason.PRICE_OUTSIDE_BAND: D.OrdRejReason.PRICE_EXCEEDS_BAND,  # 16

    # ASSUMPTION: the specification lists no reject reason for a tick-size
    # violation. 11 (unsupported order characteristic) is the closest fit.
    RejectReason.TICK_SIZE: D.OrdRejReason.UNSUPPORTED_CHARACTERISTIC,

    # ASSUMPTION: the specification does not say whether a post-only order that
    # would cross is rejected or silently cancelled. Rejecting with 11 tells the
    # client the most.
    RejectReason.POST_ONLY_WOULD_CROSS:
        D.OrdRejReason.UNSUPPORTED_CHARACTERISTIC,

    # Confirmed: JNX_Self-Trade_Prevention_2.00 states that an MPID not
    # permitted on the port is rejected with OrdRejReason 99 (other).
    RejectReason.UNKNOWN_MPID: D.OrdRejReason.OTHER,

    RejectReason.INVALID_PRICE: D.OrdRejReason.OTHER,
    RejectReason.OTHER: D.OrdRejReason.OTHER,
}

# -- cancel rejection --------------------------------------------------------

CANCEL_REJECT_TO_REASON = {
    CancelRejectReason.TOO_LATE_TO_CANCEL: D.CxlRejReason.TOO_LATE_TO_CANCEL,
    CancelRejectReason.UNKNOWN_ORDER: D.CxlRejReason.UNKNOWN_ORDER,
    CancelRejectReason.ALREADY_PENDING: D.CxlRejReason.ALREADY_PENDING,
    CancelRejectReason.DUPLICATE_CLORDID: D.CxlRejReason.DUPLICATE_CL_ORD_ID,
    CancelRejectReason.PRICE_OUTSIDE_BAND: D.CxlRejReason.PRICE_EXCEEDS_BAND,
    CancelRejectReason.MISMATCHED_FIELD: D.CxlRejReason.OTHER,
    CancelRejectReason.OTHER: D.CxlRejReason.OTHER,
}

# -- unsolicited cancel restatement -----------------------------------------

CANCEL_REASON_TO_RESTATEMENT = {
    CancelReason.SELF_TRADE_PREVENTION:
        D.ExecRestatementReason.TRADE_PREVENTION,                    # 100
    CancelReason.CANCEL_ON_DISCONNECT:
        D.ExecRestatementReason.CANCEL_ON_CONNECTION_LOSS,           # 12
    CancelReason.MARKET_CLOSED: D.ExecRestatementReason.OTHER,       # 99
    CancelReason.ADMINISTRATIVE: D.ExecRestatementReason.OTHER,      # 99
}


#: Behaviours the specification does not define, surfaced for confirmation
#: against the real test environment. Reported by the ``venue.assumptions``
#: control command.
ASSUMPTIONS = [
    {"topic": "post-only crossing",
     "behaviour": "rejected with OrdRejReason(103)=11",
     "alternative": "silently cancelled",
     "source": "not specified in FIX Trading Specification Equities 3.00"},
    {"topic": "tick-size violation",
     "behaviour": "rejected with OrdRejReason(103)=11",
     "alternative": "OrdRejReason 99 (other)",
     "source": "no reject reason listed for tick violations"},
    {"topic": "amend priority",
     "behaviour": "price change or quantity increase loses time priority; "
                  "a quantity decrease keeps it",
     "alternative": "every amend loses priority",
     "source": "standard practice; not stated in the specification"},
    {"topic": "throttle breach",
     "behaviour": "not enforced by default",
     "alternative": "queue or reject above the configured rate",
     "source": "specification says sessions are throttled but not how"},
    {"topic": "tick-size tables",
     "behaviour": "the shipped reference/tick_sizes_*.csv ladders are "
                  "monotonic and use only values from the appendices, but the "
                  "exact bracket boundaries are unverified",
     "alternative": "the published Appendix 3/4/5 values",
     "source": "Trading Rules appendices are merged-cell tables that text "
               "extraction cannot reconstruct unambiguously; correcting the "
               "CSV is the entire fix"},
    {"topic": "FOK and MinQty interaction with self-trade prevention",
     "behaviour": "the all-or-nothing pre-check runs before any prevention "
                  "side effect, so a failing order leaves the book untouched",
     "alternative": "prevention applies during the attempt",
     "source": "interaction not described"},
]


# -- validation limits -------------------------------------------------------

def default_limits(max_order_value=100000000):
    """Japannext's order restrictions.

    From JNX_Trading_Rules_Equities_2.02 section 5: order quantity is capped at
    5% of shares outstanding (applied per instrument), the standard FIX order
    value limit is 100 million JPY, and limit prices must sit within the daily
    price range. Only limit orders exist, and MinQty is valid only with IOC.
    """
    from ...core.enums import OrderType

    return ValidationLimits(
        max_order_value=max_order_value,
        require_round_lot=True,
        require_tick=True,
        enforce_price_band=True,
        allowed_order_types=(OrderType.LIMIT,),
        allowed_time_in_force=(TimeInForce.DAY, TimeInForce.IOC,
                               TimeInForce.FOK),
        min_qty_requires_ioc=True,
    )
