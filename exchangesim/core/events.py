"""Events the core emits, and commands it accepts.

This is the seam between the venue-agnostic engine and a protocol gateway.
Gateways translate wire messages down into commands and these events back up
into wire messages; the core never constructs a FIX field, and the gateway
never touches the book.

Keeping the seam explicit is what makes the later phases cheap: injected
liquidity and scripted fills become another source of the same commands, and a
second protocol becomes another translator of the same events.
"""


class Event(object):
    """Base class for everything the engine reports."""

    __slots__ = ()

    @property
    def name(self):
        return type(self).__name__

    def describe(self):
        """JSON-friendly form, for the control plane and the journal."""
        return {"event": self.name}


class OrderAccepted(Event):
    """An order passed validation and entered the market.

    The running totals are snapshotted for the same reason
    :class:`OrderFilled` snapshots its own: a venue that acknowledges an order
    *before* matching it produces this event and its fills in one batch, and by
    render time the order has traded. The acceptance must still report the
    order as the market received it -- ``CumQty`` 0 and ``LeavesQty`` equal to
    the order quantity, which is what both HKEX documents require of it.
    """

    __slots__ = ("order", "cum_qty", "leaves_qty", "order_qty")

    def __init__(self, order):
        self.order = order
        self.cum_qty = order.cum_qty
        self.leaves_qty = order.leaves_qty
        self.order_qty = order.quantity

    def describe(self):
        return {"event": self.name, "order_id": self.order.order_id,
                "cl_ord_id": self.order.cl_ord_id, "symbol": self.order.symbol}


class OrderRejected(Event):
    """An order was refused. ``reason`` is a :class:`~.enums.RejectReason`."""

    __slots__ = ("order", "reason", "text")

    def __init__(self, order, reason, text=""):
        self.order = order
        self.reason = reason
        self.text = text

    def describe(self):
        return {"event": self.name, "cl_ord_id": self.order.cl_ord_id,
                "reason": self.reason, "text": self.text}


class OrderFilled(Event):
    """One side of one execution.

    A trade produces two of these -- one per participant -- sharing a
    ``trade_id`` so both reports can be correlated, exactly as
    ``TrdMatchID(880)`` does on the wire.

    The order's running totals are **snapshotted here**, not read from the
    order when the event is rendered. Events are produced as a batch and
    rendered afterwards, by which time the order may have filled further or
    been cancelled -- an IOC's partial fill must report the quantity that was
    still open at that moment, and each fill of a multi-level sweep must carry
    its own running average, not the final one.
    """

    __slots__ = ("order", "quantity", "price", "trade_id", "liquidity",
                 "counterparty_order_id", "counterparty_mpid", "cum_qty",
                 "leaves_qty", "notional_units", "order_qty")

    def __init__(self, order, quantity, price, trade_id, liquidity,
                 counterparty_order_id=None, counterparty_mpid=None):
        self.order = order
        self.quantity = quantity
        self.price = price
        self.trade_id = trade_id
        self.liquidity = liquidity
        # Both are snapshots of the other side taken here, for the same reason
        # the running totals below are: the counterparty order may be filled
        # again, or gone from the book, before this event is rendered. A venue
        # whose protocol discloses who it traded with -- HKEX names the
        # counterparty's Broker ID on every execution -- needs the identity as
        # it stood at the moment of the match.
        self.counterparty_order_id = counterparty_order_id
        self.counterparty_mpid = counterparty_mpid
        self.cum_qty = order.cum_qty
        self.leaves_qty = order.leaves_qty
        self.notional_units = order.notional_units
        self.order_qty = order.quantity

    def describe(self):
        return {"event": self.name, "order_id": self.order.order_id,
                "quantity": self.quantity, "price": self.price,
                "trade_id": self.trade_id, "liquidity": self.liquidity,
                "cum_qty": self.cum_qty, "leaves_qty": self.leaves_qty}

    @property
    def is_complete(self):
        """True when this fill left nothing open."""
        return self.leaves_qty == 0


class OrderCancelled(Event):
    """An order left the book. ``reason`` is a :class:`~.enums.CancelReason`."""

    __slots__ = ("order", "reason", "text")

    def __init__(self, order, reason, text=""):
        self.order = order
        self.reason = reason
        self.text = text

    def describe(self):
        return {"event": self.name, "order_id": self.order.order_id,
                "cl_ord_id": self.order.cl_ord_id, "reason": self.reason}


class OrderDecremented(Event):
    """An order's quantity was reduced without it being cancelled.

    Produced only by self-trade prevention in Decrement mode, where the larger
    of two same-MPID orders survives at a reduced size.
    """

    __slots__ = ("order", "reduced_by", "reason")

    def __init__(self, order, reduced_by, reason):
        self.order = order
        self.reduced_by = reduced_by
        self.reason = reason

    def describe(self):
        return {"event": self.name, "order_id": self.order.order_id,
                "reduced_by": self.reduced_by, "reason": self.reason}


class OrderReplaced(Event):
    """An amend was accepted."""

    __slots__ = ("order", "previous_cl_ord_id", "kept_priority")

    def __init__(self, order, previous_cl_ord_id, kept_priority=True):
        self.order = order
        self.previous_cl_ord_id = previous_cl_ord_id
        #: False when the amend cost the order its place in the queue.
        self.kept_priority = kept_priority

    def describe(self):
        return {"event": self.name, "order_id": self.order.order_id,
                "cl_ord_id": self.order.cl_ord_id,
                "orig_cl_ord_id": self.previous_cl_ord_id,
                "kept_priority": self.kept_priority}


class CancelRejected(Event):
    """A cancel or replace request was refused.

    ``order`` may be None when the request named an unknown order, which is why
    the identifiers are carried separately.
    """

    __slots__ = ("cl_ord_id", "orig_cl_ord_id", "reason", "text", "order",
                 "is_replace")

    def __init__(self, cl_ord_id, orig_cl_ord_id, reason, text="", order=None,
                 is_replace=False):
        self.cl_ord_id = cl_ord_id
        self.orig_cl_ord_id = orig_cl_ord_id
        self.reason = reason
        self.text = text
        self.order = order
        self.is_replace = is_replace

    def describe(self):
        return {"event": self.name, "cl_ord_id": self.cl_ord_id,
                "orig_cl_ord_id": self.orig_cl_ord_id, "reason": self.reason,
                "text": self.text}


class TradeExecuted(Event):
    """The market-data view of an execution: one per trade, not per side."""

    __slots__ = ("symbol", "market", "price", "quantity", "trade_id",
                 "buy_order_id", "sell_order_id", "aggressor_side", "timestamp")

    def __init__(self, symbol, market, price, quantity, trade_id,
                 buy_order_id, sell_order_id, aggressor_side, timestamp=None):
        self.symbol = symbol
        self.market = market
        self.price = price
        self.quantity = quantity
        self.trade_id = trade_id
        self.buy_order_id = buy_order_id
        self.sell_order_id = sell_order_id
        self.aggressor_side = aggressor_side
        self.timestamp = timestamp

    def describe(self):
        return {"event": self.name, "symbol": self.symbol, "market": self.market,
                "price": self.price, "quantity": self.quantity,
                "trade_id": self.trade_id, "aggressor_side": self.aggressor_side}


class TradingStateChanged(Event):
    """A market or instrument moved between trading phases."""

    __slots__ = ("market", "symbol", "previous", "current")

    def __init__(self, market, symbol, previous, current):
        self.market = market
        #: None when the change applies to the whole market.
        self.symbol = symbol
        self.previous = previous
        self.current = current

    @property
    def scope(self):
        return "instrument" if self.symbol else "market"

    def describe(self):
        return {"event": self.name, "market": self.market, "symbol": self.symbol,
                "previous": self.previous, "current": self.current,
                "scope": self.scope}
