"""Price-time priority limit order book.

Price levels are kept in a ``bisect``-maintained sorted list of integer price
units, and each level holds an :class:`~collections.OrderedDict` of orders in
arrival order. That gives exact best-price lookup and strict FIFO within a
level, which is what the Japannext rules require: "for limit orders at the same
price, the order accepted earlier has higher priority".

Both sides store prices ascending. The best bid is therefore the *last* entry
and the best offer the *first* -- one ordering, two accessors, no negated keys
to get wrong.

A side also holds a queue of **priceless** orders. During continuous trading a
market order never rests -- it takes what it can and its remainder expires --
but in an auction it is an *at-auction order*, which rests until the uncrossing
and then executes at the auction price ahead of every limit order. Those cannot
go in the sorted ladder, so they have their own FIFO queue and are deliberately
excluded from ``best_price`` and ``depth``: the BBO and the ita board are
statements about limit prices, and an at-auction order has none.
"""

import bisect
from collections import OrderedDict

from .enums import Side


class BookSide(object):
    """One side of the book: price levels, each a FIFO queue of orders."""

    __slots__ = ("is_buy", "_prices", "_levels", "_at_market")

    def __init__(self, is_buy):
        self.is_buy = is_buy
        self._prices = []                 # ascending integer price units
        self._levels = {}                 # price -> OrderedDict(order_id -> Order)
        self._at_market = OrderedDict()   # at-auction orders, in arrival order

    # -- mutation ----------------------------------------------------------

    def add(self, order):
        if order.price is None:
            self._at_market[order.order_id] = order
            return order
        level = self._levels.get(order.price)
        if level is None:
            level = OrderedDict()
            self._levels[order.price] = level
            bisect.insort(self._prices, order.price)
        level[order.order_id] = order
        return order

    def remove(self, order):
        if order.price is None:
            return self._at_market.pop(order.order_id, None) is not None
        level = self._levels.get(order.price)
        if level is None:
            return False
        if level.pop(order.order_id, None) is None:
            return False
        if not level:
            del self._levels[order.price]
            index = bisect.bisect_left(self._prices, order.price)
            if index < len(self._prices) and self._prices[index] == order.price:
                self._prices.pop(index)
        return True

    def clear(self):
        self._prices = []
        self._levels = {}
        self._at_market = OrderedDict()

    # -- at-auction orders -------------------------------------------------

    def at_market_orders(self):
        """Priceless orders, in arrival order."""
        return list(self._at_market.values())

    @property
    def at_market_quantity(self):
        return sum(order.leaves_qty for order in self._at_market.values())

    # -- inspection --------------------------------------------------------

    @property
    def best_price(self):
        if not self._prices:
            return None
        return self._prices[-1] if self.is_buy else self._prices[0]

    def best_level(self):
        price = self.best_price
        return self._levels.get(price) if price is not None else None

    def prices_in_priority(self):
        """Price levels from most to least aggressive."""
        return reversed(self._prices) if self.is_buy else iter(self._prices)

    def orders_in_priority(self):
        """Resting *limit* orders, best price first and FIFO within a price.

        At-auction orders are excluded on purpose. This iterator exists for
        continuous matching, which has no price at which to execute a priceless
        resting order; the auction asks for them explicitly instead.
        """
        for price in list(self.prices_in_priority()):
            level = self._levels.get(price)
            if not level:
                continue
            for order in list(level.values()):
                yield order

    def quantity_at(self, price):
        level = self._levels.get(price)
        return sum(order.leaves_qty for order in level.values()) if level else 0

    def orders_at(self, price):
        level = self._levels.get(price)
        return len(level) if level else 0

    def quantity_above(self, price):
        """Total resting quantity strictly above ``price``.

        Together with :meth:`quantity_below` this is what lets a fixed-height
        ladder account for the whole book: an ita board shows OVER and UNDER
        rather than silently dropping what falls outside the window.
        """
        index = bisect.bisect_right(self._prices, price)
        return sum(self.quantity_at(price) for price in self._prices[index:])

    def quantity_below(self, price):
        index = bisect.bisect_left(self._prices, price)
        return sum(self.quantity_at(price) for price in self._prices[:index])

    def depth(self, levels=5):
        """Aggregated ladder: ``[{price, quantity, orders}, ...]``, best first."""
        result = []
        for price in self.prices_in_priority():
            level = self._levels[price]
            quantity = sum(order.leaves_qty for order in level.values())
            if quantity <= 0:
                continue
            result.append({"price": price, "quantity": quantity,
                           "orders": len(level)})
            if len(result) >= levels:
                break
        return result

    def crosses(self, price):
        """True when ``price`` would trade against this side's best."""
        best = self.best_price
        if best is None:
            return False
        return best >= price if self.is_buy else best <= price

    @property
    def total_quantity(self):
        return sum(order.leaves_qty for order in self.orders())

    def orders(self):
        """Every resting order on this side, at-auction orders included."""
        for order in list(self._at_market.values()):
            yield order
        for level in self._levels.values():
            for order in level.values():
                yield order

    def __len__(self):
        return (len(self._at_market)
                + sum(len(level) for level in self._levels.values()))

    def __bool__(self):
        return bool(self._prices) or bool(self._at_market)

    __nonzero__ = __bool__  # Python 2 name, harmless and explicit


class OrderBook(object):
    """The two sides of one instrument's book, in one market."""

    def __init__(self, symbol, market=None):
        self.symbol = symbol
        self.market = market
        self.bids = BookSide(is_buy=True)
        self.asks = BookSide(is_buy=False)

    # -- access ------------------------------------------------------------

    def side(self, side):
        return self.bids if Side.is_buy(side) else self.asks

    def opposite(self, side):
        return self.asks if Side.is_buy(side) else self.bids

    @property
    def best_bid(self):
        return self.bids.best_price

    @property
    def best_ask(self):
        return self.asks.best_price

    @property
    def spread(self):
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None:
            return None
        return ask - bid

    @property
    def is_crossed(self):
        """True when the book is locked or crossed.

        Continuous matching should make this impossible; it can legitimately
        occur while accumulating orders for an auction.
        """
        bid, ask = self.best_bid, self.best_ask
        return bid is not None and ask is not None and bid >= ask

    # -- mutation ----------------------------------------------------------

    def add(self, order):
        return self.side(order.side).add(order)

    def remove(self, order):
        return self.side(order.side).remove(order)

    def clear(self):
        self.bids.clear()
        self.asks.clear()

    # -- views -------------------------------------------------------------

    def depth(self, levels=5):
        return {"bids": self.bids.depth(levels), "asks": self.asks.depth(levels)}

    def orders(self):
        return list(self.bids.orders()) + list(self.asks.orders())

    def __len__(self):
        return len(self.bids) + len(self.asks)

    def __repr__(self):
        return "OrderBook(%s, bids=%d, asks=%d)" % (
            self.symbol, len(self.bids), len(self.asks))
