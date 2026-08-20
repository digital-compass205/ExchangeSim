"""Translation between core vocabulary and HKEX OCG-C FIX values.

Every mapping is a statement about the venue and is traceable to a line in
HKEX_OCGC_FIX_Trading_Protocol_3.12. Where the specification is silent the
choice is marked ``ASSUMPTION`` and collected in :data:`ASSUMPTIONS`, which the
``venue.assumptions`` control command reports.
"""

from ...core.enums import (
    Capacity,
    CancelRejectReason,
    CancelReason,
    ExecInst,
    OrderStatus,
    OrderType,
    RejectReason,
    Side,
    StpMode,
    TimeInForce,
)
from ...core.validation import Rejection, StandardValidator, ValidationLimits
from . import dictionary as D

# -- side --------------------------------------------------------------------
#
# HKEX has no "sell short exempt"; the core value simply never appears.

SIDE_TO_CORE = {
    D.SideValue.BUY: Side.BUY,
    D.SideValue.SELL: Side.SELL,
    D.SideValue.SELL_SHORT: Side.SELL_SHORT,
}
SIDE_TO_FIX = dict((core, fix) for fix, core in SIDE_TO_CORE.items())

# -- order type --------------------------------------------------------------

ORD_TYPE_TO_CORE = {
    D.OrdType.MARKET: OrderType.MARKET,
    D.OrdType.LIMIT: OrderType.LIMIT,
}
ORD_TYPE_TO_FIX = dict((core, fix) for fix, core in ORD_TYPE_TO_CORE.items())

# -- time in force -----------------------------------------------------------
#
# TimeInForce 9 (At Crossing) is accepted on the wire because the dialect
# defines it, but it belongs to the auction sessions this venue does not yet
# run, so it is refused by validation rather than silently treated as Day.

TIF_TO_CORE = {
    D.TimeInForce.DAY: TimeInForce.DAY,
    D.TimeInForce.IOC: TimeInForce.IOC,
    D.TimeInForce.FOK: TimeInForce.FOK,
}
TIF_TO_FIX = dict((core, fix) for fix, core in TIF_TO_CORE.items())

# -- capacity ----------------------------------------------------------------

CAPACITY_TO_CORE = {
    D.OrderCapacity.AGENCY: Capacity.AGENCY,
    D.OrderCapacity.PRINCIPAL: Capacity.PRINCIPAL,
}
CAPACITY_TO_FIX = dict((core, fix) for fix, core in CAPACITY_TO_CORE.items())

# -- execution instructions --------------------------------------------------

EXEC_INST_TO_CORE = {
    D.ExecInstValue.IGNORE_NOTIONAL: ExecInst.IGNORE_NOTIONAL_CHECK,
    # 'c' suppresses the price band and reference-price checks. The core has no
    # separate flag for that, so it is handled in the handler by clearing the
    # instrument's band for the order -- see handlers._exec_inst.
}

# -- self-match prevention ---------------------------------------------------
#
# Section 6.13: the SMP *instruction* is registered with HKEX against the SMP
# ID, not carried on the order. The gateway therefore resolves ID -> instruction
# from the venue's registry and puts the result on the order, which is the seam
# core/matching.py already exposes.

SMP_INSTRUCTION_TO_CORE = {
    "CANCEL_AGGRESSIVE": StpMode.CANCEL_NEWEST,
    "CANCEL_PASSIVE": StpMode.CANCEL_OLDEST,
}
SMP_INSTRUCTION_TO_NAME = dict(
    (core, name) for name, core in SMP_INSTRUCTION_TO_CORE.items())

#: Used when an SMP ID appears on an order but has not been registered.
DEFAULT_SMP_INSTRUCTION = StpMode.CANCEL_NEWEST

# -- order status ------------------------------------------------------------

STATUS_TO_FIX = {
    OrderStatus.PENDING_NEW: D.OrdStatus.PENDING_NEW,
    OrderStatus.NEW: D.OrdStatus.NEW,
    OrderStatus.PARTIALLY_FILLED: D.OrdStatus.PARTIALLY_FILLED,
    OrderStatus.FILLED: D.OrdStatus.FILLED,
    OrderStatus.CANCELLED: D.OrdStatus.CANCELED,
    OrderStatus.PENDING_CANCEL: D.OrdStatus.PENDING_CANCEL,
    OrderStatus.PENDING_REPLACE: D.OrdStatus.PENDING_REPLACE,
    OrderStatus.REJECTED: D.OrdStatus.REJECTED,
    OrderStatus.EXPIRED: D.OrdStatus.EXPIRED,
    # FIX 5.0 has no distinct "replaced" status; the order carries on as New or
    # Partially Filled and ExecType(150)=5 conveys that it was the amend.
    OrderStatus.REPLACED: D.OrdStatus.NEW,
}

# -- cancels that report as expiry -------------------------------------------
#
# ASSUMPTION: the specification defines both "Order Cancelled Unsolicited"
# (ExecType 4) and "Order Expired" (ExecType C) but does not say which applies
# to an unfilled IOC/FOK balance or a market order's remainder. FIX 5.0
# semantics reserve Expired for an order that ran out of time or of book, which
# is exactly these, so they report as C and everything else as 4.

EXPIRING_CANCEL_REASONS = frozenset((
    CancelReason.IOC_REMAINDER,
    CancelReason.MARKET_REMAINDER,
    CancelReason.FOK_UNFILLED,
    CancelReason.MIN_QTY_UNMET,
))

# -- order rejection ---------------------------------------------------------

REJECT_TO_ORD_REJ_REASON = {
    RejectReason.EXCEEDS_QUANTITY_LIMIT: D.OrdRejReason.EXCEEDS_LIMIT,      # 3
    RejectReason.DUPLICATE_ORDER: D.OrdRejReason.DUPLICATE_ORDER,           # 6
    RejectReason.INVALID_QUANTITY: D.OrdRejReason.INCORRECT_QUANTITY,       # 13
    RejectReason.PRICE_OUTSIDE_BAND: D.OrdRejReason.PRICE_EXCEEDS_BAND,     # 16
    RejectReason.EXCEEDS_VALUE_LIMIT:
        D.OrdRejReason.NOTIONAL_EXCEEDS_THRESHOLD,                          # 20

    # Confirmed: 19 is "reference price is not available", which is precisely
    # the state of an instrument admitted without a nominal price.
    RejectReason.INVALID_PRICE: D.OrdRejReason.OTHER,

    # ASSUMPTION: HKEX publishes no reject reason for an unknown security, an
    # unsupported order characteristic or a closed market. 99 (other) is the
    # only honest choice; RejectText(1328) carries the detail.
    RejectReason.UNKNOWN_SYMBOL: D.OrdRejReason.OTHER,
    RejectReason.MARKET_CLOSED: D.OrdRejReason.OTHER,
    RejectReason.UNSUPPORTED_CHARACTERISTIC: D.OrdRejReason.OTHER,
    RejectReason.TICK_SIZE: D.OrdRejReason.OTHER,
    RejectReason.POST_ONLY_WOULD_CROSS: D.OrdRejReason.OTHER,
    RejectReason.UNKNOWN_MPID: D.OrdRejReason.OTHER,
    RejectReason.OTHER: D.OrdRejReason.OTHER,
}

#: The reason above that means "no nominal price to band against".
NO_REFERENCE_PRICE = D.OrdRejReason.REFERENCE_PRICE_UNAVAILABLE

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
#
# Self-trade prevention is the one place HKEX is more expressive than Japannext:
# the aggressive and the passive side of a prevented match carry different
# reasons, so the renderer picks by which order the event concerns rather than
# by the reason alone. See handlers._restatement_for.

CANCEL_REASON_TO_RESTATEMENT = {
    CancelReason.MASS_CANCEL:
        D.ExecRestatementReason.MASS_CANCELLED_BY_BROKER,                # 103
    CancelReason.CANCEL_ON_DISCONNECT:
        D.ExecRestatementReason.CANCEL_ON_DISCONNECT,                    # 104
    CancelReason.MARKET_CLOSED:
        D.ExecRestatementReason.CANCEL_ON_TRADING_HALT,                  # 6
    CancelReason.ADMINISTRATIVE: D.ExecRestatementReason.MARKET_OPERATION,  # 8
}


#: Behaviours the specification does not define, surfaced for confirmation
#: against the real test environment. Reported by ``venue.assumptions``.
ASSUMPTIONS = [
    {"topic": "scope",
     "behaviour": "board-lot continuous trading, plus the POS and CAS auctions",
     "alternative": "the full OCG-C surface",
     "source": "deliberate: the odd/special lot book and its trade request "
               "flow, quotes (35=S/Z/AI), trade capture (35=AE/AR) and drop "
               "copy are not implemented. An odd-lot order -- LotType(1093)=1 "
               "-- is rejected rather than silently treated as a board lot"},
    {"topic": "auction periods",
     "behaviour": "driven by control commands -- 'state.set' opens the session, "
                  "'auction.lock' ends the input period, 'state.set' ends it "
                  "and uncrosses -- with no wall-clock timings at all",
     "alternative": "the published schedule (POS 09:00-09:30, CAS 16:00-16:10) "
                    "on a timer, with a random close",
     "source": "deliberate and general to this simulator: market phases are "
               "command-driven so a CI job never waits, and the random closing "
               "period would make a test's outcome depend on the clock"},
    {"topic": "CAS reference price",
     "behaviour": "the nominal price when the session opens -- the last traded "
                  "price, falling back to the previous close",
     "alternative": "the median of five nominal prices sampled at 15-second "
                    "intervals from 15:59:00",
     "source": "the median needs a minute of wall clock the simulator does not "
               "spend; 'auction.reference' sets it explicitly where a test "
               "needs a particular value"},
    {"topic": "POS reference price",
     "behaviour": "the previous closing price, which is the instrument's "
                  "nominal price as loaded from reference data",
     "alternative": "a separately fed previous-close field",
     "source": "confirmed as the right anchor; only its provenance is assumed"},
    {"topic": "auction stage 2 with a one-sided book",
     "behaviour": "a security quoted on only one side when the input period "
                  "closes keeps its stage 1 band",
     "alternative": "pin one edge to the single quote and leave the other at "
                    "stage 1",
     "source": "the rule reads 'within the highest bid and the lowest ask', "
               "which is an interval and needs both; the specification does "
               "not say what happens when there is only one"},
    {"topic": "carried-forward orders outside the auction band",
     "behaviour": "an aggressive order is cancelled with "
                  "ExecRestatementReason(378)=8 (Market Operation); a passive "
                  "one is carried in and simply never matches",
     "alternative": "a restatement reason specific to the transition",
     "source": "the cancellation itself is specified; the specification lists "
               "no reason code for it, and 8 is the closest published fit"},
    {"topic": "self-match prevention during an auction",
     "behaviour": "not applied, so two orders sharing an SMP ID may execute "
                  "against each other in POS or CAS",
     "alternative": "apply it, as continuous trading does",
     "source": "confirmed: section 6.13 states that a trade matched during POS "
               "or CAS does not trigger SMP"},
    {"topic": "password authentication",
     "behaviour": "EncryptedPassword(1402) is required on Logon but not "
                  "verified; any non-empty value is accepted",
     "alternative": "reject on mismatch",
     "source": "the password is RSA-encrypted under the venue's public key "
               "(EncryptedPasswordMethod 101) and a simulator holds no private "
               "key, so it cannot be decrypted let alone checked"},
    {"topic": "quotation rule aggression allowance",
     "behaviour": "a limit price may reach 9 spreads through the opposite "
                  "side's best, which is the *enhanced* limit order allowance; "
                  "a plain limit order may only reach the opposite best",
     "alternative": "read MaxPriceLevels(1090)=1 as marking a plain limit "
                    "order and cap it at the opposite best",
     "source": "OCG-C maps both onto OrdType(40)=2 and distinguishes them by "
               "MaxPriceLevels(1090), which this venue accepts and ignores; "
               "the enhanced allowance is the more permissive of the two, so "
               "it never rejects an order the real venue would take"},
    {"topic": "quotation rule with an empty side",
     "behaviour": "each bound applies only when its anchor exists, so an empty "
                  "book accepts any on-tick price inside the 9-times limit",
     "alternative": "refuse to quote at all until both sides are populated",
     "source": "the Rules state the rule for the case 'where existing buy and "
               "sell orders exist on the primary queues' and are silent on an "
               "empty side; the alternative would make the first order of the "
               "day unplaceable"},
    {"topic": "VCM",
     "behaviour": "the Volatility Control Mechanism is not implemented, so no "
                  "cooling-off period is ever triggered",
     "alternative": "a 5-minute cooling-off with its own price band after a "
                    "±10% move against the last trade five minutes prior",
     "source": "deliberate: VCM needs a wall-clock timer, and market phases "
               "here are command-driven so that CI never waits"},
    {"topic": "expiry versus cancellation",
     "behaviour": "an unfilled IOC, FOK or market-order balance reports "
                  "ExecType(150)=C (Expired); every other unsolicited removal "
                  "reports 4 (Cancelled)",
     "alternative": "ExecType 4 throughout",
     "source": "the specification defines both reports but does not say which "
               "covers an unfilled balance"},
    {"topic": "self-match prevention instruction",
     "behaviour": "the instruction is held per SMP ID by the venue -- set it "
                  "with the 'smp.register' control command -- and defaults to "
                  "cancel aggressive for an unregistered ID",
     "alternative": "reject an order whose SMP ID was never registered",
     "source": "section 6.13 says the instruction is registered with HKEX "
               "against the ID out of band, so there is nothing on the wire "
               "for the simulator to read"},
    {"topic": "cancel passive scope",
     "behaviour": "each resting order with the matching SMP ID is cancelled as "
                  "the incoming order reaches it, so it walks the book",
     "alternative": "cancel every order sharing the ID up front, including "
                    "those the incoming order would never have reached",
     "source": "section 6.13 says 'all tradable resting order(s) with the same "
               "SMP ID will be cancelled'; tradable is read as 'would have "
               "traded with this order'"},
    {"topic": "TimeInForce 9 (At Crossing)",
     "behaviour": "rejected, being meaningful only in an auction session",
     "alternative": "accepted and treated as Day",
     "source": "auctions are out of scope; see the 'scope' entry"},
    {"topic": "market segment routing",
     "behaviour": "a security's segment comes from the 'segment' column of "
                  "reference/securities.csv and decides which book the order "
                  "reaches",
     "alternative": "a single book for the whole venue",
     "source": "MarketSegmentID(1300) appears only on mass cancel, so an order "
               "message never names its segment; it is a property of the "
               "security"},
    {"topic": "MaxPriceLevels(1090)",
     "behaviour": "accepted and ignored; the order sweeps as deep as it must",
     "alternative": "stop after the first price level, as the field asks",
     "source": "the specification says only that the value must be 1 if "
               "present, without describing the resulting behaviour"},

    # -- the binary encoding, where it and the FIX one do not simply agree --

    {"topic": "binary Gap Fill value",
     "behaviour": "Y and N, as FIX spells GapFillFlag(123)",
     "alternative": "1 and 0",
     "source": "the value list for this field is a graphic the binary "
               "specification's text layer does not carry. It is typed Byte, "
               "an ASCII character, where every genuinely numeric flag in that "
               "dictionary is a UInt8 -- so a letter is the reading the type "
               "supports"},
    {"topic": "binary Disclosure Instructions bit order",
     "behaviour": "bit 0 (None) is the least significant bit of the 16-bit "
                  "word, so a disclosure of None travels as the value 1",
     "alternative": "bit 0 as the most significant bit, as the field presence "
                    "map numbers its own bits",
     "source": "the specification calls the field 'the Integer value of the 16 "
               "bit representation', which is the ordinary reading of an "
               "integer's bits; only the presence map states the other one"},
    {"topic": "binary cancel reject for an unknown order",
     "behaviour": "the Execution Report that refuses it carries no instrument "
                  "and no side, and zero for both quantities",
     "alternative": "echo the instrument and side out of the client's own "
                    "request",
     "source": "this encoding reports a refused cancel as an Execution Report, "
               "whose required fields describe an order -- and an unknown "
               "OrigClOrdID is precisely the case where there is no order to "
               "describe. Echoing the request back would state as fact "
               "something the venue has not confirmed"},
]


# -- price limits ------------------------------------------------------------

class NineTimesRule(object):
    """SEHK's nominal-price limit, in the shape ``Instrument`` expects.

    The Rules of the Exchange state it as a deviation, not a band: an order
    price that is *nine times or more* the nominal price, or *one ninth of it
    or less*, is refused. That is a multiplicative rule, so it cannot be a row
    in a threshold table the way Japannext's bands are -- hence a small object
    with the same ``limits_for`` interface rather than a
    :class:`~exchangesim.core.instrument.BandTable`.

    Both ends are exclusive in the rule and inclusive here, so the returned
    bounds are the outermost prices that are still acceptable.
    """

    __slots__ = ("multiple", "name")

    def __init__(self, multiple=9, name="9-times"):
        self.multiple = multiple
        self.name = name

    def limits_for(self, base_price):
        base = int(base_price)
        return base // self.multiple + 1, base * self.multiple - 1

    def band_for(self, base_price):
        low, high = self.limits_for(base_price)
        return max(int(base_price) - low, high - int(base_price))

    def __len__(self):
        return 1


#: How far a limit price may sit behind its own side's best, in spreads.
QUOTATION_SPREADS = 24

#: How far a limit price may reach through the opposite side's best.
#: This is the enhanced-limit-order allowance; see ASSUMPTIONS.
AGGRESSION_SPREADS = 9


# -- validation limits -------------------------------------------------------

def default_limits(max_order_value=None,
                   quotation_spreads=QUOTATION_SPREADS,
                   aggression_spreads=AGGRESSION_SPREADS):
    """HKEX's order restrictions.

    Board lots vary per security -- SEHK lot sizes range from 100 to 10,000 --
    so the round-lot check matters more here than at a venue with one lot size.
    Market and limit orders are both accepted, which Japannext does not do.

    Two *separate* price rules apply, which is the thing most easily got wrong:
    the 9-times rule against the static nominal price (carried by the
    instrument's band table, see :class:`NineTimesRule`), and the quotation
    rule against the live BBO (below).
    """
    return ValidationLimits(
        max_order_value=max_order_value,
        require_round_lot=True,
        require_tick=True,
        enforce_price_band=True,
        allowed_order_types=(OrderType.LIMIT, OrderType.MARKET),
        allowed_time_in_force=(TimeInForce.DAY, TimeInForce.IOC,
                               TimeInForce.FOK),
        # HKEX has no MinQty field at all, so the restriction cannot arise.
        min_qty_requires_ioc=False,
        max_quotation_spreads=quotation_spreads,
        max_aggression_spreads=aggression_spreads,
    )


class HkexValidator(StandardValidator):
    """The standard checks, plus the ones that only apply during an auction.

    Auction price limits are anchored on a reference price the venue fixes when
    the session opens, and they change halfway through it, so they cannot be
    expressed as :class:`ValidationLimits`. They live here, asking the venue
    which session -- if any -- the order's market is running.
    """

    def __init__(self, codec, limits, venue):
        StandardValidator.__init__(self, codec, limits)
        self.venue = venue

    def validate_new(self, order, instrument, state_machine, book=None):
        rejection = StandardValidator.validate_new(
            self, order, instrument, state_machine, book)
        if rejection is not None:
            return rejection
        return self._check_auction(order)

    def validate_amend(self, order, instrument, state_machine,
                       new_quantity, new_price):
        rejection = StandardValidator.validate_amend(
            self, order, instrument, state_machine, new_quantity, new_price)
        if rejection is not None:
            return rejection
        return self._check_auction(order, new_price)

    def _check_auction(self, order, price=None):
        session = self.venue.auctions.get(order.market)
        if session is None:
            # Outside an auction an order must carry a price unless it is a
            # market order, which the standard checks have already allowed.
            return None

        if order.time_in_force in TimeInForce.IMMEDIATE:
            return Rejection(
                RejectReason.UNSUPPORTED_CHARACTERISTIC,
                "%s orders cannot be entered during the %s"
                % (order.time_in_force, session.kind))

        wanted = order.price if price is None else price
        if not session.permits(order.symbol, wanted):
            low, high = session.limits_for(order.symbol)
            return Rejection(
                RejectReason.PRICE_OUTSIDE_BAND,
                "price %s is outside the %s %s limit of %s to %s"
                % (self.codec.format(wanted), session.kind,
                   session.stage_name,
                   self.codec.format(low) if low else "-",
                   self.codec.format(high) if high else "-"))
        return None
