"""The NSE pre-open call auction.

Two things are worth pinning. The chain NSE publishes is not HKEX's -- it has
no rule resolving a tie in the direction of the surplus -- and the difference
shows only on a book where that rule would have decided, which is exactly the
kind of divergence a shared chain would hide. And the phases are three, not two:
order entry, then a locked window where the price is known and nothing has
executed, then the uncrossing.
"""

import unittest

from exchangesim.control.commands import CommandError
from exchangesim.core import auction
from exchangesim.core.enums import TradingState
from exchangesim.venues.nse import dictionary as D
from exchangesim.venues.nse import transactions as X
from exchangesim.venues.nse.auctions import PREOPEN_RULES

from tests.nsesupport import (
    BOX_ONE,
    INSTRUMENT,
    USER_ONE,
    USER_TWO,
    VenueHarness,
    venue_config,
)


def preopen_harness():
    return VenueHarness(venue_config(
        markets=[{"name": "NORMAL", "description": "Normal Market",
                  "state": "PRE_OPEN"}]))


class RuleChainTest(unittest.TestCase):
    """What NSE's chain is, and where it differs from the default."""

    def test_the_chain_has_no_surplus_direction_rule(self):
        self.assertEqual(PREOPEN_RULES, (
            auction.MAX_VOLUME,
            auction.LOWEST_IMBALANCE,
            auction.CLOSEST_TO_REFERENCE,
            auction.HIGHER_PRICE,
        ))
        self.assertIn(auction.SURPLUS_DIRECTION, auction.STANDARD_RULES)
        self.assertNotIn(auction.SURPLUS_DIRECTION, PREOPEN_RULES)

    def test_the_default_chain_is_unchanged(self):
        # HKEX's behaviour must be byte-identical to what it was before the
        # chain became an argument.
        self.assertEqual(auction.STANDARD_RULES[0], auction.MAX_VOLUME)
        self.assertEqual(len(auction.STANDARD_RULES), 5)


class PreOpenTest(unittest.TestCase):

    def setUp(self):
        self.harness = preopen_harness()
        self.addCleanup(self.harness.close)
        self.client = self.harness.client(BOX_ONE, users=(USER_ONE, USER_TWO))
        self.client.clear()

    def market(self):
        return self.harness.venue.markets["NORMAL"]

    def security(self, result, symbol=INSTRUMENT):
        for entry in result["securities"]:
            if entry["symbol"] == symbol:
                return entry
        return None

    # -- order entry -------------------------------------------------------

    def test_an_order_rests_without_matching(self):
        self.client.new_order(USER_ONE, quantity=100, price="1545.00")
        self.client.new_order(USER_TWO, side=D.BuySell.SELL, quantity=100,
                              price="1540.00")
        # They cross, and in a continuous phase would have traded at once.
        self.assertEqual(
            self.harness.command("trades", symbol=INSTRUMENT,
                                 market="NORMAL")["trades"], [])
        self.assertEqual(
            len(self.harness.command("orders", market="NORMAL")["orders"]), 2)

    def test_an_at_the_opening_order_is_accepted_without_a_price(self):
        # ATO is a bit of ST_ORDER_FLAGS, and the order it describes has no
        # price at all -- which is why the book keeps it off the ladder.
        self.client.new_order(USER_ONE, quantity=50, price=None,
                              extra={D.FLAG_ATO: "Y"})
        reports = self.client.received()
        self.assertEqual([int(m.msg_type) for m in reports],
                         [X.ORDER_CONFIRMATION])
        self.assertEqual(reports[0].get(D.FLAG_ATO), "Y")
        self.assertEqual(reports[0].get(D.FLAG_PREOPEN), "Y")

    def test_an_at_the_opening_order_stays_out_of_the_quote(self):
        self.client.new_order(USER_ONE, quantity=50, price=None,
                              extra={D.FLAG_ATO: "Y"})
        bbo = self.harness.command("bbo", symbol=INSTRUMENT, market="NORMAL")
        self.assertIsNone(bbo["bid"])
        # But it is still an order, and cancel-all must find it.
        self.assertEqual(
            len(self.harness.command("orders", market="NORMAL")["orders"]), 1)

    # -- the indicative price ----------------------------------------------

    def test_the_indicative_price_moves_with_the_book(self):
        self.client.new_order(USER_ONE, quantity=100, price="1545.00")
        self.client.new_order(USER_TWO, side=D.BuySell.SELL, quantity=80,
                              price="1540.00")
        first = self.security(self.harness.command("preopen"))
        self.assertEqual(first["quantity"], 80)

        self.client.new_order(USER_TWO, side=D.BuySell.SELL, quantity=40,
                              price="1540.00")
        second = self.security(self.harness.command("preopen"))
        self.assertEqual(second["quantity"], 100)

    def test_the_reference_price_is_the_previous_close(self):
        self.client.new_order(USER_ONE, quantity=10, price="1545.00")
        self.client.new_order(USER_TWO, side=D.BuySell.SELL, quantity=10,
                              price="1540.00")
        entry = self.security(self.harness.command("preopen"))
        self.assertEqual(entry["reference_price"], "1543.25")

    def test_a_tie_is_broken_towards_the_previous_close(self):
        # Both candidate prices trade the whole 100 with no imbalance, so the
        # first two rules decide nothing. HKEX would resolve towards the
        # surplus; NSE has no such rule and goes to the reference price, which
        # is 3.25 from 1540.00 and 6.75 from 1550.00. The candidates are the
        # prices on the book, not every tick between them.
        self.client.new_order(USER_ONE, quantity=100, price="1550.00")
        self.client.new_order(USER_TWO, side=D.BuySell.SELL, quantity=100,
                              price="1540.00")
        entry = self.security(self.harness.command("preopen"))
        self.assertEqual(entry["reason"], auction.CLOSEST_TO_REFERENCE)
        self.assertEqual(entry["price"], "1540.00")

    # -- locking -----------------------------------------------------------

    def test_locking_ends_order_entry(self):
        self.client.new_order(USER_ONE, quantity=100, price="1545.00")
        result = self.harness.command("preopen.lock")
        self.assertTrue(result["locked"])
        self.assertEqual(result["state"], TradingState.OPENING_AUCTION)
        self.assertEqual(self.market().state.market_state,
                         TradingState.OPENING_AUCTION)

    def test_locking_executes_nothing(self):
        self.client.new_order(USER_ONE, quantity=100, price="1545.00")
        self.client.new_order(USER_TWO, side=D.BuySell.SELL, quantity=100,
                              price="1540.00")
        self.harness.command("preopen.lock")
        self.assertEqual(
            self.harness.command("trades", symbol=INSTRUMENT,
                                 market="NORMAL")["trades"], [])

    def test_locking_a_market_that_is_not_in_the_pre_open_is_refused(self):
        self.harness.set_state(TradingState.OPEN)
        self.assertRaises(CommandError, self.harness.command, "preopen.lock")

    # -- uncrossing --------------------------------------------------------

    def test_opening_uncrosses_at_one_price(self):
        self.client.new_order(USER_ONE, quantity=100, price="1545.00")
        self.client.new_order(USER_TWO, side=D.BuySell.SELL, quantity=80,
                              price="1540.00")
        self.harness.command("preopen.lock")
        self.client.clear()
        self.harness.set_state(TradingState.OPEN)

        fills = [m for m in self.client.received()
                 if int(m.msg_type) == X.TRADE_CONFIRMATION]
        self.assertEqual(len(fills), 2)                 # one per side
        self.assertEqual({m.get(D.FILL_PRICE) for m in fills}, {"1545.00"})
        self.assertEqual({m.get(D.FILL_QTY) for m in fills}, {"80"})

    def test_an_at_the_opening_order_trades_against_a_limit_order(self):
        self.client.new_order(USER_ONE, quantity=100, price="1545.00")
        self.client.new_order(USER_TWO, side=D.BuySell.SELL, quantity=100,
                              price=None, extra={D.FLAG_ATO: "Y"})
        self.harness.command("preopen.lock")
        self.client.clear()
        self.harness.set_state(TradingState.OPEN)

        # The sell side has no *limit* price, so the limit books do not cross
        # and no equilibrium price exists. The auction then executes at the
        # reference price -- the previous close -- rather than not at all,
        # which is the core's documented fallback and NSE's rule too.
        fills = [m for m in self.client.received()
                 if int(m.msg_type) == X.TRADE_CONFIRMATION]
        self.assertEqual(len(fills), 2)
        self.assertEqual({m.get(D.FILL_PRICE) for m in fills}, {"1543.25"})
        self.assertEqual({m.get(D.FILL_QTY) for m in fills}, {"100"})

    def test_two_at_the_opening_orders_trade_at_the_previous_close(self):
        self.client.new_order(USER_ONE, quantity=60, price=None,
                              extra={D.FLAG_ATO: "Y"})
        self.client.new_order(USER_TWO, side=D.BuySell.SELL, quantity=60,
                              price=None, extra={D.FLAG_ATO: "Y"})
        self.harness.command("preopen.lock")
        self.client.clear()
        self.harness.set_state(TradingState.OPEN)

        fills = [m for m in self.client.received()
                 if int(m.msg_type) == X.TRADE_CONFIRMATION]
        self.assertEqual(len(fills), 2)
        self.assertEqual({m.get(D.FILL_PRICE) for m in fills}, {"1543.25"})

    def test_what_did_not_trade_rests_into_continuous_trading(self):
        self.client.new_order(USER_ONE, quantity=100, price="1545.00")
        self.client.new_order(USER_TWO, side=D.BuySell.SELL, quantity=40,
                              price="1540.00")
        self.harness.command("preopen.lock")
        self.harness.set_state(TradingState.OPEN)

        bbo = self.harness.command("bbo", symbol=INSTRUMENT, market="NORMAL")
        self.assertEqual(bbo["bid"], "1545.00")
        self.assertEqual(bbo["bid_qty"], 60)

    def test_an_unfilled_at_the_opening_order_never_reaches_the_book(self):
        # A priceless order cannot rest into continuous trading at any venue.
        self.client.new_order(USER_ONE, quantity=50, price=None,
                              extra={D.FLAG_ATO: "Y"})
        self.harness.command("preopen.lock")
        self.client.clear()
        self.harness.set_state(TradingState.OPEN)

        self.assertEqual(
            self.harness.command("orders", market="NORMAL")["orders"], [])
        cancels = [m for m in self.client.received()
                   if int(m.msg_type) == X.ORDER_CANCEL_CONFIRMATION]
        self.assertEqual(len(cancels), 1)

    def test_going_straight_from_pre_open_to_open_still_uncrosses(self):
        # Locking is a separate step, not a required one.
        self.client.new_order(USER_ONE, quantity=100, price="1545.00")
        self.client.new_order(USER_TWO, side=D.BuySell.SELL, quantity=80,
                              price="1540.00")
        self.client.clear()
        self.harness.set_state(TradingState.OPEN)
        trades = self.harness.command("trades", symbol=INSTRUMENT,
                                      market="NORMAL")["trades"]
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["price"], "1545.00")

    def test_closing_from_the_pre_open_executes_nothing(self):
        # Only the move to Open executes. Closing expires the book instead,
        # which is why the uncrossing has to happen before the state changes.
        self.client.new_order(USER_ONE, quantity=100, price="1545.00")
        self.client.new_order(USER_TWO, side=D.BuySell.SELL, quantity=80,
                              price="1540.00")
        self.harness.set_state(TradingState.CLOSED)
        self.assertEqual(
            self.harness.command("trades", symbol=INSTRUMENT,
                                 market="NORMAL")["trades"], [])
        self.assertEqual(
            self.harness.command("orders", market="NORMAL")["orders"], [])

    def test_the_market_status_a_client_sees_follows_the_phase(self):
        self.client.clear()
        self.harness.command("preopen.lock")
        status = [m for m in self.client.received()
                  if int(m.msg_type) == X.SYSTEM_INFORMATION_OUT]
        self.assertTrue(status)
        self.assertEqual(status[-1].get(D.NORMAL_STATUS),
                         D.MarketStatus.PRE_OPEN_ENDED)


if __name__ == "__main__":
    unittest.main()
