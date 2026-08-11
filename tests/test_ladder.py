"""The ita board: tick alignment, the OVER/UNDER totals, and its anchor.

Tick size varies with price, which is the whole reason this is computed on the
server. The tests that matter most are the ones that cross a threshold in the
tick table, because that is where a client stepping a fixed tick would drift.
"""

import unittest

from exchangesim.control.commands import CommandError
from exchangesim.core.instrument import Instrument, TickTable
from exchangesim.core.marketdata import MarketDataService
from exchangesim.core.prices import PriceCodec
from exchangesim.venues.japannext import dictionary as D

from .coresupport import OrderFactory
from .jnxsupport import CLIENT1, CLIENT2, SYMBOL, VenueHarness

CODEC = PriceCodec(1)


def prices(view):
    return [row["price"] for row in view["rows"]]


def row_at(view, price):
    for row in view["rows"]:
        if row["price"] == price:
            return row
    raise AssertionError("no row at %s in %s" % (price, prices(view)))


class WalkTest(unittest.TestCase):
    """The price walk alone, against a deliberately awkward tick table."""

    def setUp(self):
        # Ticks of 0.1 below 100.0, then 1.0 at and above it: one threshold,
        # positioned so a ladder centred near it must straddle the change.
        self.ticks = TickTable([(0, {None: 1}), (1000, {None: 10})], "test")
        self.data = MarketDataService("DAY", CODEC)

    def instrument(self, base="100.0", limits=None):
        instrument = Instrument("T", base_price=CODEC.parse(base),
                                tick_table=self.ticks)
        if limits is not None:
            instrument.band_override = (CODEC.parse(limits[0]),
                                        CODEC.parse(limits[1]))
        return instrument

    def ladder(self, instrument, rows=3, center=None):
        from exchangesim.core.book import OrderBook
        self.data.register(OrderBook("T", "DAY"))
        return self.data.ladder(
            "T", instrument, rows=rows,
            center=CODEC.parse(center) if center else None)

    def test_a_ladder_steps_by_the_tick_in_force(self):
        view = self.ladder(self.instrument(), rows=2, center="200.0")

        self.assertEqual(["202.0", "201.0", "200.0", "199.0", "198.0"],
                         prices(view))

    def test_stepping_up_across_a_threshold_takes_the_larger_tick(self):
        # Below 100.0 the tick is 0.1; at and above it, 1.0. So the row above
        # 100.0 is 101.0, not 100.1 -- which is the drift a client stepping a
        # single remembered tick would introduce.
        view = self.ladder(self.instrument(), rows=3, center="99.8")

        self.assertEqual(["101.0", "100.0", "99.9",
                          "99.8",
                          "99.7", "99.6", "99.5"], prices(view))

    def test_stepping_down_across_a_threshold_takes_the_smaller_tick(self):
        """At exactly the boundary the row below belongs to the lower band."""
        view = self.ladder(self.instrument(), rows=2, center="100.0")

        self.assertEqual(["102.0", "101.0", "100.0", "99.9", "99.8"],
                         prices(view))

    def test_the_anchor_is_snapped_onto_the_ladder(self):
        view = self.ladder(self.instrument(), rows=1, center="200.5")

        self.assertEqual("200.0", view["anchor"])
        self.assertIn("200.0", prices(view))

    def test_the_ladder_stops_at_the_price_band(self):
        instrument = self.instrument(limits=("199.0", "201.0"))

        view = self.ladder(instrument, rows=5, center="200.0")

        self.assertEqual(["201.0", "200.0", "199.0"], prices(view))
        self.assertEqual("199.0", view["limit_down"])
        self.assertEqual("201.0", view["limit_up"])

    def test_the_ladder_never_descends_below_one_price_unit(self):
        view = self.ladder(self.instrument(base="0.3"), rows=10, center="0.3")

        self.assertEqual("0.1", prices(view)[-1])

    def test_an_instrument_with_no_tick_table_uses_single_units(self):
        instrument = Instrument("T", base_price=CODEC.parse("10.0"))

        view = self.ladder(instrument, rows=2, center="10.0")

        self.assertEqual(["10.2", "10.1", "10.0", "9.9", "9.8"], prices(view))

    def test_an_instrument_with_nothing_to_anchor_on_yields_no_rows(self):
        instrument = Instrument("T")

        view = self.ladder(instrument, rows=3)

        self.assertEqual([], view["rows"])
        self.assertIsNone(view["anchor"])
        self.assertEqual(0, view["over"])


class LadderAgainstABookTest(unittest.TestCase):
    """The board over a real book, driven through the venue."""

    def setUp(self):
        self.harness = VenueHarness()
        self.addCleanup(self.harness.close)
        self.buyer = self.harness.client(CLIENT1)
        self.seller = self.harness.client(CLIENT2)

    def ladder(self, rows=3, **extra):
        args = {"symbol": SYMBOL, "market": "DAY", "rows": rows}
        args.update(extra)
        return self.harness.dispatch("ladder", args)

    def rest(self, prices_and_sides):
        for index, (side, price, quantity) in enumerate(prices_and_sides):
            client = self.buyer if side == D.SideValue.BUY else self.seller
            client.new_order("L%d" % index, side=side, price=price,
                             quantity=quantity)
        self.buyer.drain()
        self.seller.drain()

    def test_an_empty_book_anchors_on_the_base_price(self):
        view = self.ladder()

        self.assertEqual("2845.5", view["anchor"])
        self.assertEqual(7, len(view["rows"]))
        self.assertTrue(all(row["bid_qty"] == 0 and row["ask_qty"] == 0
                            for row in view["rows"]))

    def test_resting_quantity_appears_on_the_right_side(self):
        # 7203 ticks in 0.1, so a few rows either side is a narrow window.
        self.rest([(D.SideValue.BUY, "2845.4", 300),
                   (D.SideValue.SELL, "2845.6", 200)])

        view = self.ladder()

        self.assertEqual(300, row_at(view, "2845.4")["bid_qty"])
        self.assertEqual(0, row_at(view, "2845.4")["ask_qty"])
        self.assertEqual(200, row_at(view, "2845.6")["ask_qty"])
        self.assertEqual(0, row_at(view, "2845.6")["bid_qty"])

    def test_orders_at_a_price_are_counted(self):
        self.rest([(D.SideValue.BUY, "2845.0", 100),
                   (D.SideValue.BUY, "2845.0", 200)])

        row = row_at(self.ladder(), "2845.0")

        self.assertEqual(300, row["bid_qty"])
        self.assertEqual(2, row["bid_orders"])

    def test_the_ladder_centres_between_the_touch(self):
        self.rest([(D.SideValue.BUY, "2845.0", 100),
                   (D.SideValue.SELL, "2846.0", 100)])

        self.assertEqual("2845.5", self.ladder()["anchor"])

    def test_one_sided_books_anchor_on_the_side_that_exists(self):
        self.rest([(D.SideValue.BUY, "2840.0", 100)])

        self.assertEqual("2840.0", self.ladder()["anchor"])

    def test_prices_run_from_the_highest_down(self):
        rows = prices(self.ladder())

        self.assertEqual(rows, sorted(rows, key=float, reverse=True))

    def test_quantity_outside_the_window_is_reported_as_over_and_under(self):
        self.rest([(D.SideValue.BUY, "2845.0", 100),
                   (D.SideValue.SELL, "2846.0", 100),
                   (D.SideValue.BUY, "2800.0", 700),
                   (D.SideValue.SELL, "2900.0", 400)])

        # rows=10 at a 0.1 tick spans 2844.5-2846.5: the touch is inside the
        # window and only the far orders fall outside it.
        view = self.ladder(rows=10)

        self.assertEqual(400, view["over"], "the far offer is above the window")
        self.assertEqual(700, view["under"], "the far bid is below the window")

    def test_nothing_outside_the_window_means_no_over_or_under(self):
        self.rest([(D.SideValue.BUY, "2845.0", 100)])

        view = self.ladder(rows=10)

        self.assertEqual(0, view["over"])
        self.assertEqual(0, view["under"])

    def test_over_and_under_exclude_what_is_already_shown(self):
        """A row's quantity must not be double-counted in the totals."""
        self.rest([(D.SideValue.SELL, "2845.5", 500)])

        view = self.ladder(rows=10)

        self.assertEqual(500, row_at(view, "2845.5")["ask_qty"])
        self.assertEqual(0, view["over"])

    def test_the_band_limits_are_reported(self):
        view = self.ladder()

        self.assertEqual("2345.5", view["limit_down"])
        self.assertEqual("3345.5", view["limit_up"])

    def test_the_last_traded_price_is_reported(self):
        self.rest([(D.SideValue.BUY, "2845.5", 100)])
        self.seller.new_order("X1", side=D.SideValue.SELL, price="2845.5",
                              quantity=100)
        self.seller.drain()

        self.assertEqual("2845.5", self.ladder()["last"])

    def test_an_explicit_centre_overrides_the_touch(self):
        self.rest([(D.SideValue.BUY, "2845.0", 100)])

        view = self.ladder(center="2800.0")

        self.assertEqual("2800.0", view["anchor"])

    def test_the_row_count_is_honoured(self):
        self.assertEqual(11, len(self.ladder(rows=5)["rows"]))

    def test_an_unknown_symbol_is_refused(self):
        with self.assertRaises(CommandError):
            self.ladder(symbol="0000")

    def test_an_absurd_row_count_is_refused(self):
        with self.assertRaises(CommandError):
            self.ladder(rows=5000)

    def test_the_ladder_reflects_a_cancellation(self):
        # Pinned by an explicit centre: an emptied book would otherwise fall
        # back to the base price and move the window out from under the row.
        self.rest([(D.SideValue.BUY, "2845.0", 300)])
        before = self.ladder(center="2845.0")
        self.assertEqual(300, row_at(before, "2845.0")["bid_qty"])

        self.harness.command("orders.cancel_all", symbol=SYMBOL)

        after = self.ladder(center="2845.0")
        self.assertEqual(0, row_at(after, "2845.0")["bid_qty"])


class LadderTickTableTest(unittest.TestCase):
    """The shipped Japannext ladder, where the tick changes with price."""

    def setUp(self):
        self.harness = VenueHarness()
        self.addCleanup(self.harness.close)

    def test_a_topix100_name_above_the_threshold_steps_by_its_larger_tick(self):
        # 6758 has base 13500 and tier TOPIX100, whose tick is 1 from 5000.
        view = self.harness.dispatch(
            "ladder", {"symbol": "6758", "market": "DAY", "rows": 2})

        self.assertEqual(["13502.0", "13501.0", "13500.0", "13499.0", "13498.0"],
                         prices(view))
        self.assertEqual("1.0", view["tick"])

    def test_a_name_below_the_threshold_steps_by_the_smaller_tick(self):
        # 9432 has base 152.3, below every threshold: the 0.1 tick applies.
        view = self.harness.dispatch(
            "ladder", {"symbol": "9432", "market": "DAY", "rows": 2})

        self.assertEqual(["152.5", "152.4", "152.3", "152.2", "152.1"],
                         prices(view))
        self.assertEqual("0.1", view["tick"])

    def test_every_row_is_a_price_the_venue_would_accept(self):
        """A board that offers an untradeable price is worse than useless."""
        instrument = self.harness.venue.instruments["6758"]
        view = self.harness.dispatch(
            "ladder", {"symbol": "6758", "market": "DAY", "rows": 20})

        for row in view["rows"]:
            price = CODEC.parse(row["price"])
            self.assertTrue(instrument.is_on_tick(price),
                            "%s is not on a valid tick" % row["price"])
            self.assertTrue(instrument.is_within_band(price),
                            "%s is outside the price band" % row["price"])


if __name__ == "__main__":
    unittest.main()
