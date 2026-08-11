"""Call-auction uncrossing: the price-finding rules and the execution.

The rule chain is HKEX's, from the CAS and POS trading mechanism documents, but
nothing here is Hong Kong specific -- it is the standard maximum-volume auction
every venue with a call phase runs, so it lives in the core.
"""

import unittest

from exchangesim.core import auction
from exchangesim.core.enums import CancelReason, OrderStatus, Side
from exchangesim.core.ids import IdGenerator

from .coresupport import CODEC, OrderFactory, events_of, ladder, make_book


class AuctionTestCase(unittest.TestCase):

    def setUp(self):
        self.book = make_book()
        self.orders = OrderFactory()
        self.trade_ids = IdGenerator("T", width=6)

    def bid(self, quantity, price):
        order = self.orders.buy(quantity, price)
        self.book.add(order)
        return order

    def ask(self, quantity, price):
        order = self.orders.sell(quantity, price)
        self.book.add(order)
        return order

    def at_auction(self, side, quantity):
        builder = self.orders.buy if side == Side.BUY else self.orders.sell
        order = builder(quantity, price=None)
        self.book.add(order)
        return order

    def uncross(self, reference=None):
        return auction.uncross(
            self.book, CODEC.parse(reference) if reference else None)

    def price_of(self, result):
        return CODEC.format(result.price) if result.price is not None else None


class PriceDiscoveryTest(AuctionTestCase):
    """Rule 1: the price that trades the most."""

    def test_the_crossing_price_is_where_most_would_trade(self):
        self.bid(300, "101.0")
        self.ask(100, "100.0")
        self.ask(200, "101.0")

        result = self.uncross()

        self.assertEqual("101.0", self.price_of(result))
        self.assertEqual(300, result.volume)
        self.assertEqual("maximum volume", result.reason)

    def test_a_book_that_does_not_cross_yields_no_price(self):
        self.bid(100, "99.0")
        self.ask(100, "101.0")

        result = self.uncross("100.0")

        self.assertIsNone(result.price)
        self.assertFalse(result.crossed)
        self.assertEqual("book does not cross", result.reason)

    def test_a_one_sided_book_yields_no_price(self):
        self.bid(100, "100.0")

        self.assertIsNone(self.uncross("100.0").price)

    def test_an_empty_book_yields_no_price(self):
        self.assertIsNone(self.uncross("100.0").price)

    def test_only_quoted_prices_are_candidates(self):
        """An auction never invents a price nobody named."""
        self.bid(100, "105.0")
        self.ask(100, "100.0")

        result = self.uncross()

        self.assertIn(self.price_of(result), ("100.0", "105.0"))


class TieBreakTest(AuctionTestCase):
    """Rules 2 to 5, each shown firing on its own."""

    def test_rule_2_prefers_the_lowest_imbalance(self):
        self.bid(500, "102.0")
        self.bid(500, "101.0")
        self.ask(300, "100.0")

        result = self.uncross()

        # Every price trades 300; only 102.0 leaves 200 unfilled rather than 700.
        self.assertEqual("102.0", self.price_of(result))
        self.assertEqual("lowest imbalance", result.reason)
        self.assertEqual(200, result.imbalance)
        self.assertEqual(Side.BUY, result.surplus_side)

    def test_rule_3_resolves_upwards_on_a_buy_surplus(self):
        self.bid(300, "105.0")
        self.ask(100, "100.0")

        result = self.uncross()

        self.assertEqual("105.0", self.price_of(result))
        self.assertEqual("buy surplus, highest price", result.reason)

    def test_rule_3_resolves_downwards_on_a_sell_surplus(self):
        self.bid(100, "105.0")
        self.ask(300, "100.0")

        result = self.uncross()

        self.assertEqual("100.0", self.price_of(result))
        self.assertEqual("sell surplus, lowest price", result.reason)
        self.assertEqual(Side.SELL, result.surplus_side)

    def crossed_both_ways(self):
        """A book whose surplus flips sign, so rule 3 cannot apply."""
        self.bid(100, "101.0")
        self.bid(100, "100.0")
        self.ask(100, "101.0")
        self.ask(100, "100.0")

    def test_rule_4_takes_the_price_closest_to_the_reference(self):
        self.crossed_both_ways()

        self.assertEqual("100.0", self.price_of(self.uncross("100.0")))
        self.assertEqual("101.0", self.price_of(self.uncross("101.0")))

    def test_rule_4_names_itself(self):
        self.crossed_both_ways()

        self.assertEqual("closest to the reference price",
                         self.uncross("100.0").reason)

    def test_rule_5_takes_the_higher_of_two_equidistant_prices(self):
        self.crossed_both_ways()

        result = self.uncross("100.5")

        self.assertEqual("101.0", self.price_of(result))
        self.assertEqual("higher of two equidistant prices", result.reason)

    def test_rule_5_takes_the_highest_when_there_is_no_reference_price(self):
        self.crossed_both_ways()

        result = self.uncross()

        self.assertEqual("101.0", self.price_of(result))
        self.assertEqual("highest price", result.reason)


class AtAuctionOrderTest(AuctionTestCase):
    """Priceless orders: they add volume, but they never set the price."""

    def test_they_count_towards_the_matchable_volume(self):
        self.bid(100, "100.0")
        self.ask(100, "100.0")
        self.at_auction(Side.BUY, 400)

        result = self.uncross()

        self.assertEqual("100.0", self.price_of(result))
        self.assertEqual(400, result.imbalance)
        self.assertEqual(Side.BUY, result.surplus_side)

    def test_a_book_of_only_at_auction_orders_has_no_price_of_its_own(self):
        self.at_auction(Side.BUY, 100)
        self.at_auction(Side.SELL, 100)

        result = self.uncross("100.0")

        self.assertIsNone(result.price)
        self.assertEqual("book does not cross", result.reason)

    def test_they_stay_out_of_the_best_bid_and_offer(self):
        self.at_auction(Side.BUY, 100)

        self.assertIsNone(self.book.best_bid)
        self.assertEqual([], ladder(self.book, "bids"))

    def test_they_are_still_orders_on_the_book(self):
        order = self.at_auction(Side.BUY, 100)

        self.assertIn(order, self.book.orders())
        self.assertEqual(1, len(self.book))
        self.assertEqual(100, self.book.bids.at_market_quantity)

    def test_removing_one_works(self):
        order = self.at_auction(Side.SELL, 100)

        self.assertTrue(self.book.remove(order))
        self.assertEqual(0, self.book.asks.at_market_quantity)


class ExecutionTest(AuctionTestCase):
    """Everyone trades at one price, at-auction orders first."""

    def run_auction(self, reference=None):
        result = self.uncross(reference)
        price = result.price if result.crossed else (
            CODEC.parse(reference) if reference else None)
        events = auction.execute(self.book, price, self.trade_ids)
        return result, events

    def test_both_sides_execute_at_the_auction_price(self):
        buyer = self.bid(100, "101.0")
        seller = self.ask(100, "100.0")

        _result, events = self.run_auction()
        fills = events_of(events, "OrderFilled")

        self.assertEqual(2, len(fills))
        self.assertEqual({"101.0"},
                         set(CODEC.format(fill.price) for fill in fills))
        self.assertEqual(OrderStatus.FILLED, buyer.status)
        self.assertEqual(OrderStatus.FILLED, seller.status)

    def test_a_buyer_who_bid_more_than_the_auction_price_pays_the_auction_price(self):
        buyer = self.bid(100, "105.0")
        self.ask(100, "100.0")
        self.ask(100, "105.0")

        result, events = self.run_auction()

        # 100.0 and 105.0 both trade 100, but 100.0 leaves nothing unfilled.
        self.assertEqual("100.0", self.price_of(result))
        prices = set(CODEC.format(fill.price)
                     for fill in events_of(events, "OrderFilled"))
        self.assertEqual(set(["100.0"]), prices,
                         "one auction, one price, for everybody")
        self.assertEqual("100.0000", CODEC.format_average(
            buyer.notional_units, buyer.cum_qty))

    def test_at_auction_orders_are_filled_before_limit_orders(self):
        early_limit = self.bid(100, "100.0")
        at_auction = self.at_auction(Side.BUY, 100)
        self.ask(100, "100.0")

        self.run_auction()

        self.assertEqual(100, at_auction.cum_qty)
        self.assertEqual(0, early_limit.cum_qty,
                         "order type outranks arrival time in an auction")

    def test_within_a_price_the_earlier_order_is_filled_first(self):
        first = self.bid(100, "100.0")
        second = self.bid(100, "100.0")
        self.ask(100, "100.0")

        self.run_auction()

        self.assertEqual(100, first.cum_qty)
        self.assertEqual(0, second.cum_qty)

    def test_a_more_aggressive_limit_price_is_filled_first(self):
        patient = self.bid(100, "100.0")
        keen = self.bid(100, "101.0")
        self.ask(100, "100.0")

        self.run_auction()

        self.assertEqual(100, keen.cum_qty)
        self.assertEqual(0, patient.cum_qty)

    def test_an_order_outside_the_auction_price_does_not_trade(self):
        self.bid(100, "101.0")
        self.ask(100, "100.0")
        stale = self.ask(100, "110.0")

        self.run_auction()

        self.assertEqual(0, stale.cum_qty)
        self.assertEqual(OrderStatus.NEW, stale.status)

    def test_the_unfilled_balance_stays_on_the_book(self):
        buyer = self.bid(300, "101.0")
        self.ask(100, "100.0")

        self.run_auction()

        self.assertEqual(100, buyer.cum_qty)
        self.assertEqual(200, buyer.leaves_qty)
        self.assertEqual([("101.0", 200)], ladder(self.book, "bids"))

    def test_a_trade_is_reported_once_for_market_data(self):
        self.bid(100, "101.0")
        self.ask(100, "100.0")

        _result, events = self.run_auction()

        self.assertEqual(1, len(events_of(events, "TradeExecuted")))

    def test_an_auction_trade_has_no_aggressor(self):
        self.bid(100, "101.0")
        self.ask(100, "100.0")

        _result, events = self.run_auction()
        trade = events_of(events, "TradeExecuted")[0]

        self.assertIsNone(trade.aggressor_side)

    def test_nothing_executes_without_a_price(self):
        self.bid(100, "99.0")
        self.ask(100, "101.0")

        _result, events = self.run_auction()

        self.assertEqual([], events)


class ReferencePriceFallbackTest(AuctionTestCase):
    """When no auction price forms, the reference price still matches orders.

    These three are the worked examples from HKEX's own CAS FAQ, which is why
    they are spelled out rather than folded into one test.
    """

    def execute_at(self, reference):
        return auction.execute(self.book, CODEC.parse(reference),
                               self.trade_ids)

    def test_a_limit_buy_below_the_reference_price_does_not_trade(self):
        self.bid(100, "99.0")
        self.at_auction(Side.SELL, 100)

        self.assertEqual([], self.execute_at("100.0"))

    def test_a_limit_sell_below_the_reference_price_trades_at_it(self):
        self.at_auction(Side.BUY, 100)
        self.ask(100, "99.0")

        fills = events_of(self.execute_at("100.0"), "OrderFilled")

        self.assertEqual(2, len(fills))
        self.assertEqual("100.0", CODEC.format(fills[0].price))

    def test_two_at_auction_orders_trade_at_the_reference_price(self):
        self.at_auction(Side.BUY, 100)
        self.at_auction(Side.SELL, 100)

        fills = events_of(self.execute_at("100.0"), "OrderFilled")

        self.assertEqual(2, len(fills))
        self.assertEqual("100.0", CODEC.format(fills[0].price))


class DescribeTest(AuctionTestCase):

    def test_the_result_formats_its_prices_through_the_codec(self):
        self.bid(300, "101.0")
        self.ask(100, "100.0")

        described = self.uncross().describe(CODEC)

        self.assertEqual("101.0", described["price"])
        self.assertEqual(["100.0", "101.0"], described["candidates"])
        self.assertIn("reason", described)


if __name__ == "__main__":
    unittest.main()
