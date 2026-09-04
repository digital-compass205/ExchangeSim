"""The pre-open call auction, and what NSE wraps around the core's uncrossing.

The split is the usual one. Finding a price -- maximise the matchable quantity,
then break ties -- is the standard call auction and lives in
:mod:`exchangesim.core.auction`. What belongs here is what NSE decides: which
tie-breaks apply, what the reference price *is*, and what happens to an order
that was entered in the pre-open and did not trade.

**NSE's chain is not HKEX's, and the difference is a rule that is missing.**
The pre-open equilibrium price is the price at which the maximum quantity is
tradable; if several qualify, the one leaving the minimum unmatched quantity;
if several still qualify, the one closest to the previous close. There is no
rule resolving a tie in the direction of the surplus, which HKEX applies
unconditionally -- so a shared chain would sometimes pick a different price
from the exchange's, and only sometimes, which is the worst way to be wrong.

**The reference price is the previous close.** A simulator has no yesterday, so
it is the security's base price from reference data, which is what that column
is for at every venue here.

The phases are command-driven, as they are for HKEX and for the same reason: a
pre-open that closed on a timer would make a test's outcome depend on the clock.
``state.set PRE_OPEN`` opens order entry, ``preopen.lock`` ends it -- NSE calls
that state "Preopen ended", and the core calls it ``OPENING_AUCTION`` -- and
``state.set OPEN`` uncrosses and starts continuous trading.
"""

import logging

from ...core import auction
from ...core.enums import TradingState

log = logging.getLogger(__name__)

#: NSE's pre-open tie-breaks, in order. Compare
#: :data:`exchangesim.core.auction.STANDARD_RULES`, which has one more.
PREOPEN_RULES = (
    auction.MAX_VOLUME,
    auction.LOWEST_IMBALANCE,
    auction.CLOSEST_TO_REFERENCE,
    auction.HIGHER_PRICE,
)

#: The phases in which order entry reaches the pre-open book.
ACCUMULATING = (TradingState.PRE_OPEN,)

#: The phase in which the book is locked and the equilibrium price is known but
#: nothing has executed. NSE's "Preopen ended".
LOCKED = TradingState.OPENING_AUCTION


class PreOpenSession(object):
    """The pre-open for one market: its reference prices and its outcome.

    Holds no orders -- those are in the book like any others -- and no timer.
    What it holds is the reference price per security and the last result, so
    ``preopen`` can report an indicative price while the session is still open,
    which is the one thing a client watching a pre-open actually wants.
    """

    def __init__(self, venue):
        self.venue = venue
        self.locked = False
        #: symbol -> the result of the last uncrossing, for reporting.
        self.results = {}

    # -- reference prices --------------------------------------------------

    def reference_price(self, symbol):
        """The previous close, which reference data holds as the base price."""
        instrument = self.venue.instruments.get(symbol)
        return instrument.base_price if instrument is not None else None

    def reference_prices(self):
        return dict((symbol, self.reference_price(symbol))
                    for symbol in self.venue.instruments)

    # -- the session -------------------------------------------------------

    def open(self):
        self.locked = False
        self.results = {}
        log.info("pre-open opened on '%s'", self.venue.market_name)

    def lock(self):
        """End the order-entry period. Nothing executes yet.

        Separate from uncrossing because NSE separates them: the book is frozen
        in "Preopen ended" while the equilibrium price is published, and only
        the move to Open executes anything.
        """
        self.locked = True
        log.info("pre-open locked on '%s'", self.venue.market_name)
        return self.indicative()

    def indicative(self):
        """What each security would trade at if the auction ran now.

        Computed rather than remembered, because it changes with every order
        until the book is locked.
        """
        market = self.venue.markets[self.venue.market_name]
        prices = {}
        for symbol in sorted(market.symbols):
            result = auction.uncross(market.book(symbol),
                                     self.reference_price(symbol),
                                     PREOPEN_RULES)
            prices[symbol] = result
        return prices

    def uncross(self):
        """Execute every security's auction at its equilibrium price."""
        market = self.venue.markets[self.venue.market_name]
        results, events = market.uncross_all(self.reference_prices())
        self.results = results
        self.locked = False
        traded = sum(1 for result in results.values() if result.crossed)
        log.info("pre-open uncrossed on '%s': %d of %d securities traded",
                 self.venue.market_name, traded, len(results))
        return results, events

    # -- reporting ---------------------------------------------------------

    def describe(self, indicative=True):
        codec = self.venue.codec
        source = self.indicative() if indicative else self.results
        return {
            "market": self.venue.market_name,
            "state": self.venue.markets[self.venue.market_name].state.market_state,
            "locked": self.locked,
            "rules": list(PREOPEN_RULES),
            "securities": [
                {
                    "symbol": symbol,
                    "reference_price": codec.format(self.reference_price(symbol))
                    if self.reference_price(symbol) is not None else None,
                    "price": codec.format(result.price) if result.price else None,
                    "quantity": result.volume,
                    "imbalance": result.imbalance,
                    "surplus_side": result.surplus_side,
                    "reason": result.reason,
                }
                for symbol, result in sorted(source.items())
                if result.crossed or indicative is False
            ],
        }
