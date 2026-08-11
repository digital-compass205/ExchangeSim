"""Order book: price ordering, FIFO within a level, depth and removal."""

import unittest

from exchangesim.core.enums import Side

from .coresupport import OrderFactory, ladder, make_book, px


class BookOrderingTest(unittest.TestCase):

    def setUp(self):
        self.book = make_book()
        self.order = OrderFactory()

    def test_best_bid_is_the_highest_price(self):
        for price in ("2840", "2845.5", "2843"):
            self.book.add(self.order.buy(100, price))

        self.assertEqual(px("2845.5"), self.book.best_bid)

    def test_best_ask_is_the_lowest_price(self):
        for price in ("2850", "2846.5", "2848"):
            self.book.add(self.order.sell(100, price))

        self.assertEqual(px("2846.5"), self.book.best_ask)

    def test_empty_sides_have_no_best_price(self):
        self.assertIsNone(self.book.best_bid)
        self.assertIsNone(self.book.best_ask)
        self.assertIsNone(self.book.spread)

    def test_spread_is_the_difference_in_price_units(self):
        self.book.add(self.order.buy(100, "2845.5"))
        self.book.add(self.order.sell(100, "2846.0"))

        self.assertEqual(5, self.book.spread)  # 0.5 JPY at one decimal place

    def test_bids_are_reported_best_first(self):
        for price in ("2840", "2845.5", "2843"):
            self.book.add(self.order.buy(100, price))

        self.assertEqual([("2845.5", 100), ("2843.0", 100), ("2840.0", 100)],
                         ladder(self.book, "bids"))

    def test_asks_are_reported_best_first(self):
        for price in ("2850", "2846.5", "2848"):
            self.book.add(self.order.sell(100, price))

        self.assertEqual([("2846.5", 100), ("2848.0", 100), ("2850.0", 100)],
                         ladder(self.book, "asks"))

    def test_decimal_ticks_order_correctly(self):
        # 0.1 increments are exactly why prices are integers internally.
        for price in ("100.1", "100.9", "100.2"):
            self.book.add(self.order.buy(100, price))

        self.assertEqual(px("100.9"), self.book.best_bid)
        self.assertEqual([("100.9", 100), ("100.2", 100), ("100.1", 100)],
                         ladder(self.book, "bids"))


class LevelAggregationTest(unittest.TestCase):

    def setUp(self):
        self.book = make_book()
        self.order = OrderFactory()

    def test_quantities_at_a_price_are_summed(self):
        self.book.add(self.order.buy(100, "2845.5"))
        self.book.add(self.order.buy(250, "2845.5"))

        self.assertEqual([("2845.5", 350)], ladder(self.book, "bids"))

    def test_depth_counts_orders_at_each_level(self):
        self.book.add(self.order.buy(100, "2845.5"))
        self.book.add(self.order.buy(250, "2845.5"))

        level = self.book.depth(5)["bids"][0]

        self.assertEqual(2, level["orders"])
        self.assertEqual(350, level["quantity"])

    def test_depth_is_limited_to_the_requested_levels(self):
        for offset in range(10):
            self.book.add(self.order.buy(100, str(2840 - offset)))

        self.assertEqual(3, len(self.book.depth(3)["bids"]))

    def test_partially_filled_orders_show_only_their_remainder(self):
        order = self.order.buy(100, "2845.5")
        self.book.add(order)
        order.apply_fill(40, px("2845.5"))

        self.assertEqual([("2845.5", 60)], ladder(self.book, "bids"))


class PriorityTest(unittest.TestCase):

    def setUp(self):
        self.book = make_book()
        self.order = OrderFactory()

    def test_orders_at_one_price_keep_arrival_order(self):
        first = self.order.buy(100, "2845.5")
        second = self.order.buy(100, "2845.5")
        third = self.order.buy(100, "2845.5")
        for order in (first, second, third):
            self.book.add(order)

        queued = list(self.book.bids.orders_in_priority())

        self.assertEqual([first, second, third], queued)

    def test_better_prices_come_first_across_levels(self):
        low = self.order.buy(100, "2840")
        high = self.order.buy(100, "2845.5")
        self.book.add(low)
        self.book.add(high)

        self.assertEqual([high, low], list(self.book.bids.orders_in_priority()))

    def test_sell_priority_favours_the_lowest_price(self):
        high = self.order.sell(100, "2850")
        low = self.order.sell(100, "2846")
        self.book.add(high)
        self.book.add(low)

        self.assertEqual([low, high], list(self.book.asks.orders_in_priority()))


class RemovalTest(unittest.TestCase):

    def setUp(self):
        self.book = make_book()
        self.order = OrderFactory()

    def test_removing_the_only_order_empties_the_level(self):
        order = self.order.buy(100, "2845.5")
        self.book.add(order)

        self.assertTrue(self.book.remove(order))
        self.assertIsNone(self.book.best_bid)
        self.assertEqual(0, len(self.book))

    def test_removing_one_order_leaves_the_others_at_that_level(self):
        first = self.order.buy(100, "2845.5")
        second = self.order.buy(250, "2845.5")
        self.book.add(first)
        self.book.add(second)

        self.book.remove(first)

        self.assertEqual([("2845.5", 250)], ladder(self.book, "bids"))
        self.assertEqual([second], list(self.book.bids.orders_in_priority()))

    def test_removing_the_best_level_promotes_the_next(self):
        best = self.order.buy(100, "2845.5")
        next_best = self.order.buy(100, "2843")
        self.book.add(best)
        self.book.add(next_best)

        self.book.remove(best)

        self.assertEqual(px("2843"), self.book.best_bid)

    def test_removing_an_absent_order_is_reported_not_raised(self):
        self.assertFalse(self.book.remove(self.order.buy(100, "2845.5")))

    def test_removing_twice_is_safe(self):
        order = self.order.buy(100, "2845.5")
        self.book.add(order)

        self.assertTrue(self.book.remove(order))
        self.assertFalse(self.book.remove(order))

    def test_clear_empties_both_sides(self):
        self.book.add(self.order.buy(100, "2845"))
        self.book.add(self.order.sell(100, "2846"))

        self.book.clear()

        self.assertEqual(0, len(self.book))
        self.assertIsNone(self.book.best_bid)
        self.assertIsNone(self.book.best_ask)


class CrossingTest(unittest.TestCase):

    def setUp(self):
        self.book = make_book()
        self.order = OrderFactory()

    def test_a_buy_crosses_an_offer_at_or_below_its_price(self):
        self.book.add(self.order.sell(100, "2846"))

        self.assertTrue(self.book.asks.crosses(px("2846")))
        self.assertTrue(self.book.asks.crosses(px("2847")))
        self.assertFalse(self.book.asks.crosses(px("2845.9")))

    def test_a_sell_crosses_a_bid_at_or_above_its_price(self):
        self.book.add(self.order.buy(100, "2845.5"))

        self.assertTrue(self.book.bids.crosses(px("2845.5")))
        self.assertTrue(self.book.bids.crosses(px("2845")))
        self.assertFalse(self.book.bids.crosses(px("2845.6")))

    def test_an_empty_side_never_crosses(self):
        self.assertFalse(self.book.asks.crosses(px("2846")))

    def test_a_locked_book_is_reported_as_crossed(self):
        self.book.add(self.order.buy(100, "2846"))
        self.book.add(self.order.sell(100, "2846"))

        self.assertTrue(self.book.is_crossed)

    def test_a_normal_book_is_not_crossed(self):
        self.book.add(self.order.buy(100, "2845"))
        self.book.add(self.order.sell(100, "2846"))

        self.assertFalse(self.book.is_crossed)


class SideSelectionTest(unittest.TestCase):

    def setUp(self):
        self.book = make_book()

    def test_buy_orders_rest_on_the_bid(self):
        self.assertIs(self.book.bids, self.book.side(Side.BUY))
        self.assertIs(self.book.asks, self.book.opposite(Side.BUY))

    def test_every_selling_side_rests_on_the_offer(self):
        for side in (Side.SELL, Side.SELL_SHORT, Side.SELL_SHORT_EXEMPT):
            self.assertIs(self.book.asks, self.book.side(side), side)
            self.assertIs(self.book.bids, self.book.opposite(side), side)


if __name__ == "__main__":
    unittest.main()
