"""Continuous matching with self-trade prevention.

Order-driven, price/time priority, and -- as the Japannext rules specify --
executions occur **at the resting order's price**, not the incoming order's.
An aggressive buy at 100.5 hitting a resting offer at 100.1 trades at 100.1.

Time-in-force handling:

``FOK``
    All-or-nothing, decided by a pre-check before anything executes.
``IOC``
    Fills what it can and cancels the remainder. With ``MinQty``, the whole
    order is cancelled untouched unless at least that much is executable --
    again decided before any execution, so a client cannot end up with a
    partial fill smaller than the minimum it asked for.
``DAY``
    Rests whatever is left.

The pre-checks deliberately run before any self-trade prevention side effects,
so a FOK that was never going to fill does not cancel resting orders on its way
past. The specification does not describe this interaction; the conservative
reading is the one that leaves the book untouched.
"""

import logging

from .enums import (
    CancelReason,
    ExecInst,
    Liquidity,
    OrderStatus,
    OrderType,
    RejectReason,
    Side,
    StpMode,
    TimeInForce,
)
from .events import (
    OrderAccepted,
    OrderCancelled,
    OrderDecremented,
    OrderFilled,
    OrderRejected,
    TradeExecuted,
)

log = logging.getLogger(__name__)


class MatchingEngine(object):
    """Matches orders against a book, emitting core events.

    Holds no per-symbol state: the book is passed in, so one engine serves
    every instrument in a market.
    """

    def __init__(self, codec, trade_ids, clock=None, stp_mode=StpMode.NONE):
        self.codec = codec
        self.trade_ids = trade_ids
        self.clock = clock
        self.stp_mode = stp_mode

    # -- entry point -------------------------------------------------------

    def submit(self, book, order, allow_matching=True):
        """Enter ``order`` into ``book``.

        With ``allow_matching`` False the order is only accumulated -- the
        behaviour an auction call phase needs.
        """
        events = []

        if ExecInst.POST_ONLY in order.exec_inst and self._would_cross(book, order):
            order.status = OrderStatus.REJECTED
            return [OrderRejected(
                order, RejectReason.POST_ONLY_WOULD_CROSS,
                "post-only order would remove liquidity")]

        if not allow_matching:
            return self._rest(book, order, events)

        blocked = self._precheck(order, book, events)
        if blocked:
            return events

        events.extend(self._match(book, order))

        if order.leaves_qty > 0:
            if order.order_type == OrderType.MARKET:
                # A market order carries no price, so there is no level to rest
                # at: whatever the book could not fill expires. HKEX states it
                # outright -- "Any remainder will be expired" -- and resting one
                # would in any case put a priceless order into a sorted ladder.
                order.cancel(self._now())
                events.append(OrderCancelled(
                    order, CancelReason.MARKET_REMAINDER,
                    "market order remainder expired"))
            elif order.is_immediate:
                order.cancel(self._now())
                events.append(OrderCancelled(
                    order, CancelReason.IOC_REMAINDER,
                    "unfilled remainder cancelled"))
            else:
                self._rest(book, order, events)
        elif order.status not in OrderStatus.TERMINAL:
            order.status = OrderStatus.FILLED

        return events

    # -- pre-checks --------------------------------------------------------

    def _precheck(self, order, book, events):
        """Apply FOK and MinQty all-or-nothing rules. True means "stop"."""
        if order.time_in_force == TimeInForce.FOK:
            if self._executable(book, order) < order.quantity:
                order.cancel(self._now())
                events.append(OrderCancelled(
                    order, CancelReason.FOK_UNFILLED,
                    "fill-or-kill could not be filled in full"))
                return True
            return False

        if order.time_in_force == TimeInForce.IOC and order.min_qty:
            if self._executable(book, order) < order.min_qty:
                order.cancel(self._now())
                events.append(OrderCancelled(
                    order, CancelReason.MIN_QTY_UNMET,
                    "less than MinQty was executable"))
                return True
        return False

    def _executable(self, book, order):
        """Quantity that would trade, ignoring orders self-trade prevention blocks."""
        available = 0
        for resting in book.opposite(order.side).orders_in_priority():
            if not self._prices_cross(order, resting.price):
                break
            if self._is_self_trade(order, resting):
                # In every mode a same-MPID pairing yields no execution.
                continue
            available += resting.leaves_qty
            if available >= order.quantity:
                break
        return available

    def _would_cross(self, book, order):
        if order.price is None:
            # A market order crosses whatever is there, and `crosses` cannot
            # compare a price against None.
            return bool(book.opposite(order.side))
        return book.opposite(order.side).crosses(order.price)

    def _prices_cross(self, order, resting_price):
        if order.price is None:
            return True  # a market order crosses anything
        return (resting_price <= order.price if order.is_buy
                else resting_price >= order.price)

    # -- the matching loop -------------------------------------------------

    def _match(self, book, order):
        events = []

        while order.leaves_qty > 0:
            resting = self._best_match(book, order)
            if resting is None:
                break

            if self._is_self_trade(order, resting):
                stop = self._apply_stp(book, order, resting, events)
                if stop:
                    break
                continue

            events.extend(self._execute(book, order, resting))

        return events

    def _best_match(self, book, order):
        """The highest-priority resting order that ``order`` can trade with."""
        opposite = book.opposite(order.side)
        best_price = opposite.best_price
        if best_price is None or not self._prices_cross(order, best_price):
            return None
        level = opposite.best_level()
        if not level:
            return None
        for resting in level.values():
            if resting.leaves_qty > 0:
                return resting
        return None

    def _execute(self, book, order, resting):
        quantity = min(order.leaves_qty, resting.leaves_qty)
        # The resting order sets the price -- price improvement accrues to the
        # aggressor, per the Japannext matching rules.
        price = resting.price
        trade_id = self.trade_ids.next()
        when = self._now()

        order.apply_fill(quantity, price, when)
        resting.apply_fill(quantity, price, when)

        if resting.leaves_qty == 0:
            book.remove(resting)

        buy, sell = (order, resting) if order.is_buy else (resting, order)

        return [
            OrderFilled(resting, quantity, price, trade_id, Liquidity.ADDED,
                        order.order_id),
            OrderFilled(order, quantity, price, trade_id, Liquidity.REMOVED,
                        resting.order_id),
            TradeExecuted(order.symbol, order.market, price, quantity, trade_id,
                          buy.order_id, sell.order_id,
                          Side.BUY if order.is_buy else Side.SELL, when),
        ]

    def _rest(self, book, order, events):
        """Put the remainder on the book.

        An acceptance is reported only for an order that has not traded. The
        specification defines the Order Accepted report as carrying CumQty 0
        and LeavesQty equal to OrderQty, so an order that partially filled on
        entry must not also produce one -- its partial-fill report already
        carries the resting quantity in LeavesQty.
        """
        already_traded = order.cum_qty > 0
        order.status = (OrderStatus.PARTIALLY_FILLED if already_traded
                        else OrderStatus.NEW)
        book.add(order)
        if not already_traded:
            events.append(OrderAccepted(order))
        return events

    # -- self-trade prevention ---------------------------------------------

    def _is_self_trade(self, order, resting):
        """A pairing the venue must not let execute.

        Two venues key this differently, so both are supported. Japannext keys
        on the firm's MPID and applies one mode across the market. HKEX lets the
        client put a ``SelfMatchPreventionID`` on the order, which matches even
        across Exchange Participants and carries its own instruction -- so an
        order-borne key takes precedence and is checked first.
        """
        if order.stp_id and resting.stp_id:
            return order.stp_id == resting.stp_id

        if self.stp_mode == StpMode.NONE:
            return False
        if not order.mpid or not resting.mpid:
            return False
        return order.mpid == resting.mpid

    def _stp_mode_for(self, incoming):
        """The incoming order's own instruction, or the market's default."""
        if incoming.stp_id and incoming.stp_instruction:
            return incoming.stp_instruction
        return self.stp_mode

    def _apply_stp(self, book, incoming, resting, events):
        """Resolve a self-match. Returns True when matching must stop."""
        mode = self._stp_mode_for(incoming)
        when = self._now()

        if mode == StpMode.CANCEL_NEWEST:
            # The incoming order's whole remaining balance goes back to its
            # owner; the resting order is untouched.
            incoming.cancel(when)
            events.append(OrderCancelled(
                incoming, CancelReason.SELF_TRADE_PREVENTION,
                "cancelled to prevent a self-trade"))
            return True

        if mode == StpMode.CANCEL_OLDEST:
            book.remove(resting)
            resting.cancel(when)
            events.append(OrderCancelled(
                resting, CancelReason.SELF_TRADE_PREVENTION,
                "cancelled to prevent a self-trade"))
            return False

        if mode == StpMode.DECREMENT:
            return self._apply_decrement(book, incoming, resting, events, when)

        log.error("unknown self-trade prevention mode %r; treating as CANCEL_NEWEST",
                  mode)
        incoming.cancel(when)
        events.append(OrderCancelled(
            incoming, CancelReason.SELF_TRADE_PREVENTION,
            "cancelled to prevent a self-trade"))
        return True

    def _apply_decrement(self, book, incoming, resting, events, when):
        incoming_qty = incoming.leaves_qty
        resting_qty = resting.leaves_qty

        if incoming_qty == resting_qty:
            # Equal sizes: both are cancelled back to their owners.
            book.remove(resting)
            resting.cancel(when)
            incoming.cancel(when)
            events.append(OrderCancelled(
                resting, CancelReason.SELF_TRADE_PREVENTION,
                "cancelled to prevent a self-trade"))
            events.append(OrderCancelled(
                incoming, CancelReason.SELF_TRADE_PREVENTION,
                "cancelled to prevent a self-trade"))
            return True

        if resting_qty < incoming_qty:
            # The smaller resting order is cancelled, the incoming one shrinks,
            # and entry resumes against the rest of the book.
            book.remove(resting)
            resting.cancel(when)
            events.append(OrderCancelled(
                resting, CancelReason.SELF_TRADE_PREVENTION,
                "cancelled to prevent a self-trade"))
            incoming.reduce_quantity(resting_qty, when)
            events.append(OrderDecremented(
                incoming, resting_qty, CancelReason.SELF_TRADE_PREVENTION))
            return incoming.leaves_qty == 0

        # The incoming order is the smaller one: it is cancelled entirely and
        # the resting order is decremented by that amount.
        resting.reduce_quantity(incoming_qty, when)
        events.append(OrderDecremented(
            resting, incoming_qty, CancelReason.SELF_TRADE_PREVENTION))
        if resting.leaves_qty == 0:
            book.remove(resting)
        incoming.cancel(when)
        events.append(OrderCancelled(
            incoming, CancelReason.SELF_TRADE_PREVENTION,
            "cancelled to prevent a self-trade"))
        return True

    # -- cancel and amend --------------------------------------------------

    def cancel(self, book, order, reason=CancelReason.USER_REQUEST, text=""):
        """Remove a resting order and report it."""
        book.remove(order)
        order.cancel(self._now())
        return [OrderCancelled(order, reason, text)]

    def amend(self, book, order, new_quantity=None, new_price=None):
        """Apply an amendment, returning whether time priority was kept.

        Standard priority rules: raising the quantity or changing the price
        sends the order to the back of its new price level; lowering the
        quantity alone keeps its place.
        """
        price = order.price if new_price is None else new_price
        quantity = order.quantity if new_quantity is None else new_quantity

        loses_priority = (price != order.price) or (quantity > order.quantity)

        was_resting = book.remove(order)
        order.price = price
        order.quantity = quantity
        order.updated_at = self._now()

        if order.leaves_qty > 0:
            if was_resting and not loses_priority:
                # Re-adding to the same level appends, so priority is restored
                # by rebuilding the level in its original order instead.
                self._reinsert_keeping_priority(book, order)
            elif was_resting or order.status in OrderStatus.LIVE:
                book.add(order)

        return not loses_priority

    def _reinsert_keeping_priority(self, book, order):
        """Put an order back at its original place in the level's queue."""
        side = book.side(order.side)
        level_orders = [existing for existing in side.orders()
                        if existing.price == order.price]
        for existing in level_orders:
            side.remove(existing)

        restored = sorted(level_orders + [order], key=lambda o: o.sequence)
        for existing in restored:
            side.add(existing)

    # -- helpers -----------------------------------------------------------

    def _now(self):
        return self.clock.now() if self.clock else None
