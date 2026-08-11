"""Order validation.

The checks themselves are common to equity venues -- symbol known, market open,
round lot, on tick, inside the price band, under the size and value caps -- but
their parameters and their reject codes are not. So the rules live here and the
venue supplies the limits and does the mapping onto its own codes.

Check order matters: it decides which reason a client sees when an order breaks
more than one rule. The sequence below follows the order of the Japannext
``OrdRejReason(103)`` table, from identity through state to the numeric limits.
"""

from .enums import ExecInst, OrderType, RejectReason, TimeInForce


class Rejection(object):
    """A validation failure: a core reason plus human-readable detail."""

    __slots__ = ("reason", "text")

    def __init__(self, reason, text=""):
        self.reason = reason
        self.text = text

    def __repr__(self):
        return "Rejection(%s, %r)" % (self.reason, self.text)


class ValidationLimits(object):
    """Venue-wide order restrictions."""

    __slots__ = ("max_order_value", "max_quantity", "require_round_lot",
                 "require_tick", "enforce_price_band", "allowed_order_types",
                 "allowed_time_in_force", "min_qty_requires_ioc",
                 "max_quotation_spreads", "max_aggression_spreads")

    def __init__(self, max_order_value=None, max_quantity=None,
                 require_round_lot=True, require_tick=True,
                 enforce_price_band=True, allowed_order_types=None,
                 allowed_time_in_force=None, min_qty_requires_ioc=True,
                 max_quotation_spreads=None, max_aggression_spreads=None):
        #: Cap on order value in whole currency units (Japannext: 100,000,000 JPY).
        self.max_order_value = max_order_value
        #: Venue-wide quantity cap, on top of the per-instrument one.
        self.max_quantity = max_quantity
        self.require_round_lot = require_round_lot
        self.require_tick = require_tick
        self.enforce_price_band = enforce_price_band
        self.allowed_order_types = frozenset(
            allowed_order_types or (OrderType.LIMIT,))
        self.allowed_time_in_force = frozenset(
            allowed_time_in_force or (TimeInForce.DAY, TimeInForce.IOC,
                                      TimeInForce.FOK))
        #: Japannext allows MinQty only with IOC.
        self.min_qty_requires_ioc = min_qty_requires_ioc

        # -- the quotation rule, for venues that have one ---------------------
        #
        # Distinct from the price band, which is anchored on a *static* nominal
        # price. This one is anchored on the *live* best bid and offer, so it
        # constrains how far a limit price may sit behind its own side's best
        # (``max_quotation_spreads``) and how far it may reach through the other
        # side's best (``max_aggression_spreads``), both counted in ticks.
        # SEHK is 24 and 9. None disables each independently.
        self.max_quotation_spreads = max_quotation_spreads
        self.max_aggression_spreads = max_aggression_spreads


class StandardValidator(object):
    """Applies :class:`ValidationLimits` against an instrument and market state."""

    def __init__(self, codec, limits=None):
        self.codec = codec
        self.limits = limits or ValidationLimits()

    # -- new orders --------------------------------------------------------

    def validate_new(self, order, instrument, state_machine, book=None):
        """Return a :class:`Rejection`, or None when the order is acceptable.

        ``book`` is optional and only needed by a venue with a quotation rule,
        which is anchored on the live BBO rather than on reference data.
        """
        if instrument is None or not instrument.tradable:
            return Rejection(RejectReason.UNKNOWN_SYMBOL,
                             "unknown or untradable symbol '%s'" % order.symbol)

        if state_machine is not None and not state_machine.allows_entry(order.symbol):
            return Rejection(
                RejectReason.MARKET_CLOSED,
                "market is %s" % state_machine.state_for(order.symbol))

        failure = self._check_characteristics(order)
        if failure is not None:
            return failure

        failure = self._check_size_and_price(order, instrument,
                                             order.quantity, order.price)
        if failure is not None:
            return failure

        return self._check_quotation(order, instrument, order.price, book,
                                     state_machine)

    # -- amendments --------------------------------------------------------

    def validate_amend(self, order, instrument, state_machine,
                       new_quantity, new_price):
        """Validate the post-amend shape of an existing order."""
        if instrument is None or not instrument.tradable:
            return Rejection(RejectReason.UNKNOWN_SYMBOL,
                             "unknown or untradable symbol '%s'" % order.symbol)

        if state_machine is not None and not state_machine.allows_entry(order.symbol):
            return Rejection(
                RejectReason.MARKET_CLOSED,
                "market is %s" % state_machine.state_for(order.symbol))

        if new_quantity is not None and new_quantity <= order.cum_qty:
            # Reducing to at or below the executed amount is a cancel, not an
            # amend, and the venue should say so rather than silently comply.
            return Rejection(
                RejectReason.INVALID_QUANTITY,
                "new quantity %d is not above the executed quantity %d"
                % (new_quantity, order.cum_qty))

        return self._check_size_and_price(order, instrument, new_quantity,
                                          new_price)

    # -- shared checks -----------------------------------------------------

    def _check_characteristics(self, order):
        if order.order_type not in self.limits.allowed_order_types:
            return Rejection(
                RejectReason.UNSUPPORTED_CHARACTERISTIC,
                "order type %s is not supported" % order.order_type)

        if order.time_in_force not in self.limits.allowed_time_in_force:
            return Rejection(
                RejectReason.UNSUPPORTED_CHARACTERISTIC,
                "time in force %s is not supported" % order.time_in_force)

        if order.min_qty:
            if (self.limits.min_qty_requires_ioc
                    and order.time_in_force != TimeInForce.IOC):
                return Rejection(
                    RejectReason.UNSUPPORTED_CHARACTERISTIC,
                    "MinQty is only valid with an IOC order")
            if order.min_qty > order.quantity:
                return Rejection(
                    RejectReason.INVALID_QUANTITY,
                    "MinQty %d exceeds the order quantity %d"
                    % (order.min_qty, order.quantity))

        if (ExecInst.POST_ONLY in order.exec_inst
                and order.time_in_force in TimeInForce.IMMEDIATE):
            return Rejection(
                RejectReason.UNSUPPORTED_CHARACTERISTIC,
                "a post-only order cannot be %s" % order.time_in_force)
        return None

    def _check_size_and_price(self, order, instrument, quantity, price):
        quantity = order.quantity if quantity is None else quantity
        price = order.price if price is None else price

        if quantity is not None:
            failure = self._check_quantity(instrument, quantity)
            if failure is not None:
                return failure

        if order.order_type == OrderType.LIMIT and price is None:
            return Rejection(RejectReason.INVALID_PRICE,
                             "a limit order requires a price")

        if price is not None:
            failure = self._check_price(order, instrument, price)
            if failure is not None:
                return failure

        if (price is not None and quantity is not None
                and self.limits.max_order_value
                and ExecInst.IGNORE_NOTIONAL_CHECK not in order.exec_inst):
            value = self.codec.notional(price, quantity)
            if value > self.limits.max_order_value:
                return Rejection(
                    RejectReason.EXCEEDS_VALUE_LIMIT,
                    "order value %d exceeds the limit of %d"
                    % (value, self.limits.max_order_value))
        return None

    def _check_quantity(self, instrument, quantity):
        if quantity <= 0:
            return Rejection(RejectReason.INVALID_QUANTITY,
                             "quantity must be positive")

        if self.limits.require_round_lot and not instrument.is_round_lot(quantity):
            return Rejection(
                RejectReason.INVALID_QUANTITY,
                "quantity %d is not a multiple of the lot size %d"
                % (quantity, instrument.lot_size))

        instrument_max = instrument.max_quantity
        if instrument_max and quantity > instrument_max:
            return Rejection(
                RejectReason.EXCEEDS_QUANTITY_LIMIT,
                "quantity %d exceeds %d, being 5%% of shares outstanding"
                % (quantity, instrument_max))

        if self.limits.max_quantity and quantity > self.limits.max_quantity:
            return Rejection(
                RejectReason.EXCEEDS_QUANTITY_LIMIT,
                "quantity %d exceeds the venue limit of %d"
                % (quantity, self.limits.max_quantity))
        return None

    def _check_quotation(self, order, instrument, price, book, state_machine):
        """The quotation rule: how far a limit price may sit from the BBO.

        Only meaningful while the book is continuously matching. During an
        auction the book is legitimately crossed and the venue's own auction
        price limits apply instead, so this stands down.

        Each bound needs its anchor to exist. SEHK states the rule for the case
        "where existing buy and sell orders exist on the primary queues" and
        says nothing about an empty side, so an absent anchor is treated as no
        constraint -- the alternative would make the first order of the day
        unplaceable.
        """
        limits = self.limits
        if limits.max_quotation_spreads is None and \
                limits.max_aggression_spreads is None:
            return None
        if price is None or book is None:
            return None
        if state_machine is not None and \
                not state_machine.allows_matching(order.symbol):
            return None

        tick = instrument.tick_size(price)
        if not tick:
            return None

        same = book.side(order.side).best_price
        opposite = book.opposite(order.side).best_price

        if limits.max_quotation_spreads is not None and same is not None:
            behind = (same - price) if order.is_buy else (price - same)
            if behind > limits.max_quotation_spreads * tick:
                return Rejection(
                    RejectReason.PRICE_OUTSIDE_BAND,
                    "price %s is more than %d spreads away from the best %s "
                    "of %s" % (self.codec.format(price),
                               limits.max_quotation_spreads,
                               "bid" if order.is_buy else "ask",
                               self.codec.format(same)))

        if limits.max_aggression_spreads is not None and opposite is not None:
            through = (price - opposite) if order.is_buy else (opposite - price)
            if through > limits.max_aggression_spreads * tick:
                return Rejection(
                    RejectReason.PRICE_OUTSIDE_BAND,
                    "price %s is more than %d spreads through the best %s "
                    "of %s" % (self.codec.format(price),
                               limits.max_aggression_spreads,
                               "ask" if order.is_buy else "bid",
                               self.codec.format(opposite)))
        return None

    def _check_price(self, order, instrument, price):
        if price <= 0:
            return Rejection(RejectReason.INVALID_PRICE, "price must be positive")

        if self.limits.require_tick and not instrument.is_on_tick(price):
            tick = instrument.tick_size(price)
            return Rejection(
                RejectReason.TICK_SIZE,
                "price %s is not a multiple of the tick size %s"
                % (self.codec.format(price), self.codec.format(tick)))

        if (self.limits.enforce_price_band
                and ExecInst.IGNORE_PRICE_CHECK not in order.exec_inst
                and not instrument.is_within_band(price)):
            limits = instrument.price_limits
            return Rejection(
                RejectReason.PRICE_OUTSIDE_BAND,
                "price %s is outside the permitted range %s to %s"
                % (self.codec.format(price), self.codec.format(limits[0]),
                   self.codec.format(limits[1])))
        return None
