"""Market data derived from the book and the trade flow.

Everything a user would want from a real feed -- top of book, depth, the trade
tape, session statistics -- maintained in memory and served over the control
plane. No wire protocol yet; :class:`MarketDataService` is deliberately the only
thing that knows how a book change becomes an observable event, so an ITCH-style
publisher can later subscribe to the same notifications.
"""

import logging
from collections import deque

log = logging.getLogger(__name__)

#: Trades retained per instrument for the ``trades`` command.
DEFAULT_TAPE_LENGTH = 500


def _tick_at(instrument, price):
    """The tick size in force at ``price``, defaulting to one price unit.

    An instrument with no tick table is not an error -- a test fixture may not
    need one -- and the smallest representable increment is the honest fallback.
    """
    if instrument is None or price is None:
        return 1
    tick = instrument.tick_size(price)
    return int(tick) if tick else 1


class InstrumentStatistics(object):
    """Session statistics for one instrument, in integer price units."""

    __slots__ = ("symbol", "market", "open_price", "high_price", "low_price",
                 "last_price", "last_quantity", "last_time", "volume",
                 "turnover_units", "trade_count")

    def __init__(self, symbol, market=None):
        self.symbol = symbol
        self.market = market
        self.open_price = None
        self.high_price = None
        self.low_price = None
        self.last_price = None
        self.last_quantity = None
        self.last_time = None
        self.volume = 0
        #: Sum of price_units * quantity, for an exact VWAP.
        self.turnover_units = 0
        self.trade_count = 0

    def record(self, price, quantity, when=None):
        if self.open_price is None:
            self.open_price = price
        if self.high_price is None or price > self.high_price:
            self.high_price = price
        if self.low_price is None or price < self.low_price:
            self.low_price = price
        self.last_price = price
        self.last_quantity = quantity
        self.last_time = when
        self.volume += quantity
        self.turnover_units += price * quantity
        self.trade_count += 1

    def reset(self):
        """Clear statistics, as a new trading session would."""
        self.__init__(self.symbol, self.market)

    def vwap_units(self):
        if not self.volume:
            return None
        return self.turnover_units // self.volume

    def describe(self, codec=None):
        fmt = codec.format if codec else (lambda value: value)
        return {
            "symbol": self.symbol,
            "market": self.market,
            "open": fmt(self.open_price) if self.open_price is not None else None,
            "high": fmt(self.high_price) if self.high_price is not None else None,
            "low": fmt(self.low_price) if self.low_price is not None else None,
            "last": fmt(self.last_price) if self.last_price is not None else None,
            "last_qty": self.last_quantity,
            "vwap": (codec.format_average(self.turnover_units, self.volume)
                     if codec and self.volume else None),
            "volume": self.volume,
            "turnover": (str(codec.exact_notional(self.turnover_units, 1))
                         if codec and self.volume else None),
            "trades": self.trade_count,
            "last_time": self.last_time.isoformat() if self.last_time else None,
        }


class MarketDataService(object):
    """Aggregates books, trades and statistics for one market.

    ``on_book_change`` and ``on_trade`` are called after each update with the
    topic and payload to publish, which keeps the control plane's pub/sub out
    of the matching path.
    """

    def __init__(self, market, codec, tape_length=DEFAULT_TAPE_LENGTH,
                 publisher=None):
        self.market = market
        self.codec = codec
        self.tape_length = tape_length
        self.publisher = publisher
        self._books = {}
        self._statistics = {}
        self._tapes = {}

    # -- registration ------------------------------------------------------

    def register(self, book):
        """Track an instrument's book."""
        self._books[book.symbol] = book
        self._statistics.setdefault(
            book.symbol, InstrumentStatistics(book.symbol, self.market))
        self._tapes.setdefault(book.symbol, deque(maxlen=self.tape_length))
        return book

    def unregister(self, symbol):
        """Forget an instrument entirely, including its statistics and tape."""
        self._books.pop(symbol, None)
        self._statistics.pop(symbol, None)
        self._tapes.pop(symbol, None)

    def book(self, symbol):
        return self._books.get(symbol)

    def symbols(self):
        return sorted(self._books)

    def statistics(self, symbol):
        return self._statistics.get(symbol)

    # -- updates -----------------------------------------------------------

    def record_trade(self, trade):
        """Fold a :class:`~.events.TradeExecuted` into statistics and the tape."""
        statistics = self._statistics.get(trade.symbol)
        if statistics is None:
            statistics = InstrumentStatistics(trade.symbol, self.market)
            self._statistics[trade.symbol] = statistics
            self._tapes[trade.symbol] = deque(maxlen=self.tape_length)

        statistics.record(trade.price, trade.quantity, trade.timestamp)
        self._tapes[trade.symbol].append({
            "trade_id": trade.trade_id,
            "price": self.codec.format(trade.price),
            "quantity": trade.quantity,
            "aggressor": trade.aggressor_side,
            "time": trade.timestamp.isoformat() if trade.timestamp else None,
        })
        self._publish("trade:%s:%s" % (self.market, trade.symbol),
                      self._tapes[trade.symbol][-1])

    def notify_book_change(self, symbol):
        """Publish the new top of book after the book has been modified."""
        self._publish("book:%s:%s" % (self.market, symbol), self.bbo(symbol))

    def reset_statistics(self, symbol=None):
        """Clear statistics and the tape, for one instrument or all of them."""
        symbols = [symbol] if symbol else list(self._statistics)
        for name in symbols:
            if name in self._statistics:
                self._statistics[name].reset()
            if name in self._tapes:
                self._tapes[name].clear()

    # -- views -------------------------------------------------------------

    def bbo(self, symbol):
        """Best bid and offer with their aggregate sizes."""
        book = self._books.get(symbol)
        if book is None:
            return None
        bid, ask = book.best_bid, book.best_ask
        return {
            "symbol": symbol,
            "market": self.market,
            "bid": self.codec.format(bid) if bid is not None else None,
            "bid_qty": book.bids.quantity_at(bid) if bid is not None else 0,
            "ask": self.codec.format(ask) if ask is not None else None,
            "ask_qty": book.asks.quantity_at(ask) if ask is not None else 0,
            "spread": self.codec.format(book.spread)
                      if book.spread is not None else None,
        }

    def depth(self, symbol, levels=5):
        """Aggregated ladder, rendered with formatted prices."""
        book = self._books.get(symbol)
        if book is None:
            return None
        raw = book.depth(levels)
        return {
            "symbol": symbol,
            "market": self.market,
            "bids": [self._level(level) for level in raw["bids"]],
            "asks": [self._level(level) for level in raw["asks"]],
        }

    def ladder(self, symbol, instrument=None, rows=10, center=None):
        """A tick-aligned board: one row per price, empty ones included.

        This is the Japanese *ita* view, and it has to be built here rather than
        in a client because tick size varies with price. A client stepping a
        single tick it read once would misalign every row the moment the ladder
        crossed a threshold in the instrument's tick table, and would have no way
        to know it had.

        Rows run from the highest price down. ``over`` and ``under`` carry the
        quantity resting outside the window, so the board still accounts for the
        whole book at a fixed height.
        """
        book = self._books.get(symbol)
        if book is None:
            return None

        anchor = self._anchor(book, instrument, center)
        if anchor is not None:
            # Snap here rather than inside the walk, so the anchor reported to
            # the caller is the row the board actually centres on.
            anchor -= anchor % _tick_at(instrument, anchor)
        limits = instrument.price_limits if instrument is not None else None

        prices = self._walk(anchor, instrument, rows, limits)
        statistics = self._statistics.get(symbol)
        last = statistics.last_price if statistics else None

        top, bottom = (prices[0], prices[-1]) if prices else (None, None)
        return {
            "symbol": symbol,
            "market": self.market,
            "anchor": self.codec.format(anchor) if anchor is not None else None,
            "tick": (self.codec.format(_tick_at(instrument, anchor))
                     if anchor is not None else None),
            "last": self.codec.format(last) if last is not None else None,
            "limit_down": self.codec.format(limits[0]) if limits else None,
            "limit_up": self.codec.format(limits[1]) if limits else None,
            "rows": [{
                "price": self.codec.format(price),
                "bid_qty": book.bids.quantity_at(price),
                "bid_orders": book.bids.orders_at(price),
                "ask_qty": book.asks.quantity_at(price),
                "ask_orders": book.asks.orders_at(price),
            } for price in prices],
            # At-auction orders have no price and so no row of their own. They
            # are reported alongside the ladder rather than dropped, because
            # during a call phase they can be most of the volume.
            "bid_at_auction": book.bids.at_market_quantity,
            "ask_at_auction": book.asks.at_market_quantity,
            "over": book.asks.quantity_above(top) if top is not None else 0,
            "under": book.bids.quantity_below(bottom) if bottom is not None else 0,
        }

    def _anchor(self, book, instrument, center):
        """Where to centre the ladder, in order of how informative it is."""
        if center is not None:
            return center
        bid, ask = book.best_bid, book.best_ask
        if bid is not None and ask is not None:
            return (bid + ask) // 2
        if bid is not None:
            return bid
        if ask is not None:
            return ask
        statistics = self._statistics.get(book.symbol)
        if statistics is not None and statistics.last_price is not None:
            return statistics.last_price
        if instrument is not None:
            return instrument.base_price
        return None

    def _walk(self, anchor, instrument, rows, limits):
        """Tick-aligned prices around ``anchor``, highest first.

        Each step asks the instrument for the tick *at the price being left*,
        which is what keeps the ladder correct across a tick-table threshold.
        Stepping down consults ``price - 1`` so a price sitting exactly on a
        boundary takes the lower band's tick, not the one it is entering.

        ``anchor`` is expected to be snapped onto the ladder already.
        """
        if anchor is None:
            return []

        low, high = limits if limits else (None, None)

        above = []
        price = anchor
        for _ in range(rows):
            step = _tick_at(instrument, price)
            price += step
            if high is not None and price > high:
                break
            above.append(price)

        below = []
        price = anchor
        for _ in range(rows):
            step = _tick_at(instrument, max(1, price - 1))
            price -= step
            if price < 1 or (low is not None and price < low):
                break
            below.append(price)

        within = (low is None or anchor >= low) and (high is None or anchor <= high)
        centre = [anchor] if within else []
        return list(reversed(above)) + centre + below

    def trades(self, symbol, limit=20):
        tape = self._tapes.get(symbol)
        if tape is None:
            return None
        return list(tape)[-limit:] if limit else list(tape)

    def summary(self):
        """One row per instrument: top of book plus session statistics."""
        rows = []
        for symbol in self.symbols():
            bbo = self.bbo(symbol) or {}
            statistics = self._statistics[symbol].describe(self.codec)
            rows.append({
                "symbol": symbol,
                "bid": bbo.get("bid"),
                "bid_qty": bbo.get("bid_qty"),
                "ask": bbo.get("ask"),
                "ask_qty": bbo.get("ask_qty"),
                "last": statistics.get("last"),
                "volume": statistics.get("volume"),
                "trades": statistics.get("trades"),
            })
        return rows

    def _level(self, level):
        return {"price": self.codec.format(level["price"]),
                "quantity": level["quantity"],
                "orders": level["orders"]}

    def _publish(self, topic, data):
        if self.publisher is None or data is None:
            return
        try:
            self.publisher.publish(topic, data)
        except Exception:
            # Market data must never be able to disturb matching.
            log.exception("failed publishing %s", topic)
