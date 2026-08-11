"""The Pre-opening Session (POS) and Closing Auction Session (CAS).

The uncrossing algorithm itself is venue-agnostic and lives in
:mod:`exchangesim.core.auction`. What is Hong Kong's, and therefore lives here,
is everything *around* it: which reference price the auction is anchored on,
the two-stage price limits, and what happens to orders carried in from
continuous trading.

Both sessions have the same shape -- an input period, then a locked period
during which orders may not be cancelled and prices are pinned to what the book
looked like when the input period closed, then the uncrossing:

===============  =========================  ================================
Period           POS                        CAS
===============  =========================  ================================
reference        previous closing price     median nominal price over the
                                            last minute of continuous trading
stage 1 limit    +/-15% of the reference    +/-5% of the reference
stage 2 limit    within the highest bid and lowest ask recorded when the
                 input period closed
cancellations    allowed in stage 1, refused in stage 2
===============  =========================  ================================

Timings are deliberately absent. The simulator has no scheduler -- a CI job
cannot wait until 16:08 -- so the periods are driven by control commands:
``state.set`` opens the session, ``auction.lock`` closes the input period, and
``state.set`` again ends it and uncrosses.
"""

import logging

log = logging.getLogger(__name__)


class Stage(object):
    #: Orders may be entered, amended and cancelled freely.
    INPUT = "INPUT"
    #: The No Cancellation and Random Closing/Matching periods.
    LOCKED = "LOCKED"


class AuctionSession(object):
    """One market's auction, from its opening to its uncrossing."""

    POS = "POS"
    CAS = "CAS"

    #: Percentage deviation from the reference price allowed in stage 1.
    STAGE1_PERCENT = {POS: 15, CAS: 5}

    def __init__(self, kind, market, codec):
        self.kind = kind
        self.market = market
        self.codec = codec
        self.stage = Stage.INPUT
        #: symbol -> reference price, in integer price units.
        self.reference = {}
        #: symbol -> (low, high) recorded when the input period closed.
        self.stage2 = {}

    # -- reference price ---------------------------------------------------

    def set_reference(self, symbol, price):
        if price is not None:
            self.reference[symbol] = price

    def reference_for(self, symbol):
        return self.reference.get(symbol)

    # -- price limits ------------------------------------------------------

    def limits_for(self, symbol):
        """The permissible price range for ``symbol``, or None if unbounded."""
        if self.stage == Stage.LOCKED and symbol in self.stage2:
            return self.stage2[symbol]
        return self._stage1_limits(symbol)

    def lock(self, books):
        """Close the input period, pinning stage-2 limits to the book.

        The band becomes the interval spanned by the highest bid and the lowest
        ask *as recorded now*. Which of the two is the upper bound depends on
        whether the book crosses, so they are taken as a min/max pair rather
        than assigned by side: HKEX's own worked example has a bid of 98 below
        an ask of 101 and yields a limit of 98 to 101, while a crossed book
        yields the same interval the other way round.

        A security quoted on only one side has no such interval, so it keeps
        its stage 1 band -- see the 'auction stage 2 with a one-sided book'
        entry in :data:`exchangesim.venues.hkex.rules.ASSUMPTIONS`.
        """
        self.stage = Stage.LOCKED
        self.stage2 = {}
        for symbol, book in books.items():
            bid, ask = book.best_bid, book.best_ask
            if bid is None or ask is None:
                continue
            self.stage2[symbol] = (min(bid, ask), max(bid, ask))
        return self.stage2

    def _stage1_limits(self, symbol):
        reference = self.reference.get(symbol)
        if reference is None:
            return None
        margin = reference * self.STAGE1_PERCENT[self.kind] // 100
        return max(1, reference - margin), reference + margin

    @property
    def stage_name(self):
        return "input" if self.stage == Stage.INPUT else "locked"

    def permits(self, symbol, price):
        """True when ``price`` may be entered for ``symbol`` right now."""
        if price is None:
            return True                     # an at-auction order has no price
        limits = self.limits_for(symbol)
        if limits is None:
            return True
        low, high = limits
        if low is not None and price < low:
            return False
        if high is not None and price > high:
            return False
        return True

    @property
    def cancellable(self):
        return self.stage == Stage.INPUT

    # -- carried-forward orders --------------------------------------------

    def carried_forward_verdict(self, order):
        """What to do with a live order when this session opens.

        HKEX distinguishes the two sides of the band. An *aggressive* order --
        a buy above the upper limit or a sell below the lower -- is cancelled,
        because leaving it would let it trade through the band. A *passive* one
        outside the band is carried in and simply never matches.
        """
        limits = self.limits_for(order.symbol)
        if limits is None or order.price is None:
            return "carry"
        low, high = limits
        if order.is_buy and high is not None and order.price > high:
            return "cancel"
        if not order.is_buy and low is not None and order.price < low:
            return "cancel"
        return "carry"

    # -- presentation ------------------------------------------------------

    def describe(self, symbol=None):
        fmt = self.codec.format
        summary = {"auction": self.kind, "market": self.market,
                   "stage": self.stage, "cancellable": self.cancellable}
        if symbol is None:
            return summary

        limits = self.limits_for(symbol)
        reference = self.reference.get(symbol)
        summary.update({
            "symbol": symbol,
            "reference_price": fmt(reference) if reference is not None else None,
            "price_low": fmt(limits[0]) if limits and limits[0] else None,
            "price_high": fmt(limits[1]) if limits and limits[1] else None,
        })
        return summary

    def __repr__(self):
        return "AuctionSession(%s, %s, %s)" % (self.kind, self.market, self.stage)
