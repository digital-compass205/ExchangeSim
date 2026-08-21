"""Call-auction uncrossing: one price, maximum volume.

An auction accumulates orders without matching (see
``TradingState.ACCUMULATING``) and then executes them all at a single price.
Finding that price is the whole problem, because several prices usually trade
the same maximum volume and the venue must pick one deterministically.

The rule chain implemented here is HKEX's, from the CAS and POS trading
mechanism documents, and it is the same chain most call auctions use:

1. the price at which the **matchable quantity is maximised**;
2. of those, the price with the **lowest order imbalance** -- the surplus that
   would be left unfilled;
3. of those, the **highest** price if the surplus is on the buy side at all of
   them, the **lowest** if it is on the sell side;
4. of those, the price **closest to the reference price**;
5. of those, the **higher**; or, with no reference price, the **highest**.

Rule 3 is the one worth reading twice. A buy surplus means unfilled demand, so
the auction resolves upwards; a sell surplus resolves downwards. If the surplus
direction is not the same at every remaining price the rule cannot apply and
the chain falls through to rule 4, which is why it is expressed as "at all
these prices".

**At-auction orders** -- priceless orders resting in the book's at-market queue
-- count towards the volume at every candidate price and execute ahead of limit
orders, but they never *set* the price: an auction of nothing but at-auction
orders has no price of its own and falls back on the reference price.

Everything here is integer price units, like the rest of the core.
"""

from .enums import Liquidity, Side
from .events import OrderFilled, TradeExecuted


class AuctionResult(object):
    """The outcome of an uncrossing, before anything has been executed."""

    __slots__ = ("price", "volume", "imbalance", "surplus_side", "candidates",
                 "reason")

    def __init__(self, price=None, volume=0, imbalance=0, surplus_side=None,
                 candidates=(), reason=""):
        #: The auction price (HKEX: the IEP), or None when none could be found.
        self.price = price
        #: Quantity that would execute at ``price`` (HKEX: the IEV).
        self.volume = volume
        #: Unfilled quantity on the heavier side at ``price``.
        self.imbalance = imbalance
        #: Which side that surplus is on, or None when the book balances.
        self.surplus_side = surplus_side
        #: Every price that survived rule 1, for diagnostics.
        self.candidates = tuple(candidates)
        #: Which rule settled it, for the control plane and for tests.
        self.reason = reason

    @property
    def crossed(self):
        return self.price is not None and self.volume > 0

    def describe(self, codec=None):
        fmt = codec.format if codec else (lambda value: value)
        return {
            "price": fmt(self.price) if self.price is not None else None,
            "volume": self.volume,
            "imbalance": self.imbalance,
            "surplus_side": self.surplus_side,
            "candidates": [fmt(price) for price in self.candidates],
            "reason": self.reason,
        }

    def __repr__(self):
        return "AuctionResult(price=%s, volume=%d, %s)" % (
            self.price, self.volume, self.reason)


def uncross(book, reference_price=None):
    """Find the auction price for ``book``. Executes nothing.

    Returns an :class:`AuctionResult`. ``price`` is None when no price can be
    established -- an empty side, or a book that does not cross -- in which case
    the caller decides what to do; HKEX falls back on the reference price.
    """
    bids, asks = book.bids, book.asks
    best_bid, best_ask = bids.best_price, asks.best_price

    # An auction price is only available when the limit books overlap. That is
    # the specification's own precondition, and it is why a book holding
    # nothing but at-auction orders yields no price: they name none.
    if best_bid is None or best_ask is None or best_bid < best_ask:
        return AuctionResult(reason="book does not cross")

    buy_market = bids.at_market_quantity
    sell_market = asks.at_market_quantity

    candidates = _candidate_prices(book)
    profiles = [(price,) + _demand_at(book, price, buy_market, sell_market)
                for price in candidates]
    tradable = [row for row in profiles if row[1] > 0]
    if not tradable:
        return AuctionResult(candidates=candidates, reason="nothing would trade")

    # Rule 1: maximise the matchable quantity.
    best_volume = max(row[1] for row in tradable)
    survivors = [row for row in tradable if row[1] == best_volume]
    reason = "maximum volume"

    # Rule 2: of those, the lowest imbalance.
    if len(survivors) > 1:
        least = min(abs(row[2]) for row in survivors)
        narrowed = [row for row in survivors if abs(row[2]) == least]
        if len(narrowed) < len(survivors):
            reason = "lowest imbalance"
        survivors = narrowed

    # Rule 3: of those, resolve in the direction of the surplus, but only when
    # every remaining price agrees about which side that surplus is on.
    if len(survivors) > 1:
        signs = set(_sign(row[2]) for row in survivors)
        if signs == set([1]):
            survivors = [max(survivors, key=lambda row: row[0])]
            reason = "buy surplus, highest price"
        elif signs == set([-1]):
            survivors = [min(survivors, key=lambda row: row[0])]
            reason = "sell surplus, lowest price"

    # Rule 4: of those, closest to the reference price.
    if len(survivors) > 1 and reference_price is not None:
        nearest = min(abs(row[0] - reference_price) for row in survivors)
        narrowed = [row for row in survivors
                    if abs(row[0] - reference_price) == nearest]
        if len(narrowed) < len(survivors):
            reason = "closest to the reference price"
        survivors = narrowed

    # Rule 5: of those, the higher -- which is also the fallback when there is
    # no reference price to be close to.
    if len(survivors) > 1:
        survivors = [max(survivors, key=lambda row: row[0])]
        reason = ("higher of two equidistant prices" if reference_price is not None
                  else "highest price")

    price, volume, imbalance = survivors[0]
    return AuctionResult(
        price=price, volume=volume, imbalance=abs(imbalance),
        surplus_side=(Side.BUY if imbalance > 0
                      else Side.SELL if imbalance < 0 else None),
        candidates=candidates, reason=reason)


def _candidate_prices(book):
    """Every limit price that could be the auction price, ascending.

    Only prices that orders actually name are candidates -- an auction never
    invents a price nobody quoted.
    """
    prices = set()
    for side in (book.bids, book.asks):
        for price in side.prices_in_priority():
            prices.add(price)
    return tuple(sorted(prices))


def _demand_at(book, price, buy_market, sell_market):
    """``(matchable, imbalance)`` if the auction were held at ``price``.

    Demand at a price is every buy willing to pay at least it, plus every
    at-auction buy; supply is the mirror. The imbalance is signed: positive
    means a buy surplus.
    """
    demand = buy_market + book.bids.quantity_above(price - 1)
    supply = sell_market + book.asks.quantity_below(price + 1)
    matchable = min(demand, supply)
    return matchable, demand - supply


def _sign(value):
    return (value > 0) - (value < 0)


# -- execution ---------------------------------------------------------------

def execute(book, price, trade_ids, clock=None):
    """Trade everything that can trade at ``price``, and report it.

    Priority is order type, then price, then time -- the order HKEX states for
    auction matching. At-auction orders come first because they accept any
    price; within the limit orders, the more aggressive price goes first, and
    arrival order settles the rest.

    Self-trade prevention is deliberately *not* applied: HKEX says plainly that
    a trade matched during POS or CAS does not trigger it. Every other venue the
    core serves runs its prevention in continuous trading only, so this is not
    a Hong Kong special case.

    Returns the core events. Whatever cannot trade is left resting; the caller
    decides what happens to it, because that differs by phase -- an unfilled
    at-auction order is cancelled, an unfilled limit order is usually carried
    forward into continuous trading.
    """
    if price is None:
        return []

    buyers = _executable_side(book.bids, price)
    sellers = _executable_side(book.asks, price)

    events = []
    if not buyers or not sellers:
        return events

    when = clock.now() if clock else None
    sell_index = 0
    for buyer in buyers:
        while buyer.leaves_qty > 0 and sell_index < len(sellers):
            seller = sellers[sell_index]
            if seller.leaves_qty == 0:
                sell_index += 1
                continue
            events.extend(
                _execute_pair(book, buyer, seller, price, trade_ids, when))
            if seller.leaves_qty == 0:
                sell_index += 1
        if sell_index >= len(sellers):
            break

    return events


def _executable_side(side, price):
    """Orders that would trade at ``price``, in auction priority order."""
    orders = [order for order in side.at_market_orders() if order.leaves_qty > 0]
    for resting in side.orders_in_priority():
        if resting.leaves_qty <= 0:
            continue
        if side.is_buy and resting.price < price:
            break
        if not side.is_buy and resting.price > price:
            break
        orders.append(resting)
    return orders


def _execute_pair(book, buyer, seller, price, trade_ids, when):
    """One fill between two orders, at the auction price."""
    quantity = min(buyer.leaves_qty, seller.leaves_qty)
    trade_id = trade_ids.next()

    buyer.apply_fill(quantity, price, when)
    seller.apply_fill(quantity, price, when)

    for order in (buyer, seller):
        if order.leaves_qty == 0:
            book.remove(order)

    # Neither side removed liquidity: an auction executes everyone at once, so
    # both are reported as having added it.
    return [
        OrderFilled(buyer, quantity, price, trade_id, Liquidity.ADDED,
                    seller.order_id, seller.mpid),
        OrderFilled(seller, quantity, price, trade_id, Liquidity.ADDED,
                    buyer.order_id, buyer.mpid),
        TradeExecuted(buyer.symbol, buyer.market, price, quantity, trade_id,
                      buyer.order_id, seller.order_id, None, when),
    ]
