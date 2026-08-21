"""A market: one book set, one trading state, one matching engine.

A venue may run several. Japannext has four -- J-Market Daytime, J-Market
Nighttime, X-Market and U-Market -- each with its own book and phase, addressed
on the wire by ``TargetSubID(57)``. They share instruments but not liquidity.
"""

import logging

from . import auction
from .book import OrderBook
from .enums import CancelReason, OrderStatus, RejectReason, TradingState
from .events import OrderRejected
from .marketdata import MarketDataService
from .matching import MatchingEngine
from .trading_state import TradingStateMachine

log = logging.getLogger(__name__)


class Market(object):
    """One trading segment within a venue."""

    def __init__(self, name, codec, trade_ids, clock=None, stp_mode="NONE",
                 initial_state=TradingState.CLOSED, publisher=None,
                 tape_length=None, description="", ack_on_entry=False):
        self.name = name
        self.codec = codec
        self.clock = clock
        self.description = description
        self.state = TradingStateMachine(name, initial_state)
        self.matching = MatchingEngine(codec, trade_ids, clock, stp_mode,
                                       ack_on_entry=ack_on_entry)
        self.data = MarketDataService(
            name, codec, tape_length or 500, publisher)
        self._books = {}

    # -- books -------------------------------------------------------------

    def book(self, symbol, create=True):
        book = self._books.get(symbol)
        if book is None and create:
            book = OrderBook(symbol, self.name)
            self._books[symbol] = book
            self.data.register(book)
        return book

    def remove_book(self, symbol):
        """Drop an instrument's book and its market data.

        The caller is responsible for having established that nothing rests on
        it; a book removed from under live orders would leave the registry
        pointing at orders no market can cancel.
        """
        book = self._books.pop(symbol, None)
        if book is None:
            return False
        self.data.unregister(symbol)
        return True

    @property
    def symbols(self):
        return sorted(self._books)

    @property
    def stp_mode(self):
        return self.matching.stp_mode

    @stp_mode.setter
    def stp_mode(self, mode):
        self.matching.stp_mode = mode

    # -- order flow --------------------------------------------------------

    def submit(self, order):
        """Enter a validated order. Phase decides whether it may match."""
        symbol = order.symbol

        if not self.state.allows_entry(symbol):
            order.status = OrderStatus.REJECTED
            return [OrderRejected(
                order, RejectReason.MARKET_CLOSED,
                "market is %s" % self.state.state_for(symbol))]

        book = self.book(symbol)
        events = self.matching.submit(
            book, order, allow_matching=self.state.allows_matching(symbol))
        self._absorb(events, symbol)
        return events

    def cancel(self, order, reason=CancelReason.USER_REQUEST, text=""):
        book = self.book(order.symbol)
        events = self.matching.cancel(book, order, reason, text)
        self._absorb(events, order.symbol)
        return events

    def amend(self, order, new_quantity=None, new_price=None):
        """Apply an amendment; returns (kept_priority, events).

        An amended order may immediately cross -- lifting its price through the
        offer, say -- so it is re-matched afterwards when the phase allows.
        """
        book = self.book(order.symbol)
        kept = self.matching.amend(book, order, new_quantity, new_price)

        events = []
        if self.state.allows_matching(order.symbol) and order.leaves_qty > 0:
            book.remove(order)
            events = self.matching.submit(book, order, allow_matching=True)
            # The order was already acknowledged, so a re-entry acceptance is
            # not a separate event for the client.
            events = [event for event in events
                      if type(event).__name__ != "OrderAccepted"]

        # An amendment changes the book whether or not it produces an event: a
        # quantity reduction that does not cross produces none at all, and a
        # subscriber told nothing would keep showing the old size.
        self._absorb(events, order.symbol, changed=True)
        return kept, events

    # -- auctions ----------------------------------------------------------

    def uncross(self, symbol, reference_price=None):
        """Run the call auction for one instrument.

        Returns ``(result, events)``. When no auction price can be established
        the venue's reference price is used instead, which is what lets an
        auction of nothing but at-auction orders still trade -- HKEX is explicit
        that it does, at the reference price.

        Any at-auction order still unfilled afterwards is cancelled here rather
        than left for the caller: a priceless order cannot rest into continuous
        trading at any venue, so it is a core invariant and not a venue rule.
        """
        book = self.book(symbol)
        result = auction.uncross(book, reference_price)
        price = result.price if result.crossed else reference_price

        events = auction.execute(book, price, self.matching.trade_ids, self.clock)
        events.extend(self._expire_at_auction_orders(book))
        self._absorb(events, symbol)

        log.info("market '%s' uncrossed %s at %s (%s): %d event(s)",
                 self.name, symbol,
                 self.codec.format(price) if price is not None else "no price",
                 result.reason, len(events))
        return result, events

    def uncross_all(self, reference_prices=None):
        """Uncross every instrument with a book. Returns ``{symbol: result}``."""
        prices = reference_prices or {}
        results, events = {}, []
        for symbol in self.symbols:
            result, produced = self.uncross(symbol, prices.get(symbol))
            results[symbol] = result
            events.extend(produced)
        return results, events

    def _expire_at_auction_orders(self, book):
        events = []
        for side in (book.bids, book.asks):
            for order in side.at_market_orders():
                events.extend(self.matching.cancel(
                    book, order, CancelReason.MARKET_REMAINDER,
                    "at-auction order not filled in the auction"))
        return events

    def cancel_all(self, predicate, reason, text=""):
        """Cancel every resting order matching ``predicate``."""
        events = []
        for book in self._books.values():
            for order in list(book.orders()):
                if predicate(order):
                    events.extend(self.matching.cancel(book, order, reason, text))
            self.data.notify_book_change(book.symbol)
        return events

    # -- trading state -----------------------------------------------------

    def set_state(self, new_state, symbol=None):
        """Move the market, or one instrument within it, to a new phase.

        Leaving a trading phase for a closed one expires resting day orders,
        which the venue reports as unsolicited cancels.
        """
        if symbol:
            events = self.state.set_instrument_state(symbol, new_state)
        else:
            events = self.state.set_market_state(new_state)

        if events and new_state in TradingState.NO_ENTRY:
            events.extend(self._expire_orders(symbol))
        return events

    def clear_instrument_state(self, symbol):
        return self.state.clear_instrument_state(symbol)

    def _expire_orders(self, symbol=None):
        """Cancel resting orders that a closed or halted phase must remove."""
        def affected(order):
            return symbol is None or order.symbol == symbol

        return self.cancel_all(affected, CancelReason.MARKET_CLOSED,
                               "market is %s" % self.state.state_for(symbol or ""))

    # -- market data -------------------------------------------------------

    def _absorb(self, events, symbol, changed=False):
        """Fold engine events into market data and publish the consequences.

        ``changed`` forces the book notification for a caller that altered the
        book without producing an event; events themselves imply it.
        """
        traded = False
        for event in events:
            if type(event).__name__ == "TradeExecuted":
                self.data.record_trade(event)
                traded = True
        if events or changed:
            self.data.notify_book_change(symbol)
        return traded

    # -- presentation ------------------------------------------------------

    def describe(self):
        return {
            "market": self.name,
            "description": self.description,
            "state": self.state.market_state,
            "overrides": self.state.overrides(),
            "stp_mode": self.stp_mode,
            "instruments": len(self._books),
            "resting_orders": sum(len(book) for book in self._books.values()),
        }

    def __repr__(self):
        return "Market(%s, %s, %d books)" % (
            self.name, self.state.market_state, len(self._books))
