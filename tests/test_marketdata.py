"""Market data: top of book, depth, the trade tape, statistics and publishing."""

import unittest

from exchangesim.core.enums import Side, TradingState

from .coresupport import CODEC, EngineHarness


class RecordingPublisher(object):
    """Collects published topics, standing in for the control-plane publisher."""

    def __init__(self):
        self.events = []

    def publish(self, topic, data):
        self.events.append((topic, data))
        return 1

    def topics(self):
        return [topic for topic, _data in self.events]

    def of(self, prefix):
        return [data for topic, data in self.events if topic.startswith(prefix)]


class BboTest(unittest.TestCase):

    def setUp(self):
        self.harness = EngineHarness()
        self.data = self.harness.market("DAY").data

    def test_an_empty_book_has_no_prices(self):
        self.harness.buy("C1", 100, "2845.5")
        self.harness.cancel("C2", "C1")

        bbo = self.data.bbo("7203")

        self.assertIsNone(bbo["bid"])
        self.assertIsNone(bbo["ask"])
        self.assertEqual(0, bbo["bid_qty"])

    def test_top_of_book_reports_price_and_aggregate_size(self):
        self.harness.buy("C1", 100, "2845.5")
        self.harness.buy("C2", 200, "2845.5")
        self.harness.sell("S1O", 300, "2846")

        bbo = self.data.bbo("7203")

        self.assertEqual("2845.5", bbo["bid"])
        self.assertEqual(300, bbo["bid_qty"])
        self.assertEqual("2846.0", bbo["ask"])
        self.assertEqual(300, bbo["ask_qty"])
        self.assertEqual("0.5", bbo["spread"])

    def test_an_unknown_symbol_has_no_book(self):
        self.assertIsNone(self.data.bbo("nope"))

    def test_only_the_best_level_counts_towards_the_size(self):
        self.harness.buy("C1", 100, "2845.5")
        self.harness.buy("C2", 500, "2840")

        self.assertEqual(100, self.data.bbo("7203")["bid_qty"])


class DepthTest(unittest.TestCase):

    def setUp(self):
        self.harness = EngineHarness()
        self.data = self.harness.market("DAY").data

    def test_depth_is_ordered_best_first_with_formatted_prices(self):
        self.harness.buy("C1", 100, "2840")
        self.harness.buy("C2", 200, "2845.5")
        self.harness.buy("C3", 300, "2843")

        bids = self.data.depth("7203")["bids"]

        self.assertEqual(["2845.5", "2843.0", "2840.0"],
                         [level["price"] for level in bids])
        self.assertEqual([200, 300, 100], [level["quantity"] for level in bids])

    def test_depth_reports_the_order_count_per_level(self):
        self.harness.buy("C1", 100, "2845.5")
        self.harness.buy("C2", 100, "2845.5")

        self.assertEqual(2, self.data.depth("7203")["bids"][0]["orders"])

    def test_depth_honours_the_requested_number_of_levels(self):
        for index in range(6):
            self.harness.buy("C%d" % index, 100, str(2800 + index))

        self.assertEqual(2, len(self.data.depth("7203", levels=2)["bids"]))


class TradeTapeTest(unittest.TestCase):

    def setUp(self):
        self.harness = EngineHarness()
        self.data = self.harness.market("DAY").data

    def test_a_trade_is_recorded_on_the_tape(self):
        self.harness.sell("S1O", 100, "2846")
        self.harness.buy("B1", 100, "2846")

        trades = self.data.trades("7203")

        self.assertEqual(1, len(trades))
        self.assertEqual("2846.0", trades[0]["price"])
        self.assertEqual(100, trades[0]["quantity"])
        self.assertEqual(Side.BUY, trades[0]["aggressor"])

    def test_the_tape_keeps_trades_in_order(self):
        self.harness.sell("S1O", 100, "2846")
        self.harness.sell("S2O", 100, "2847")
        self.harness.buy("B1", 200, "2847")

        prices = [trade["price"] for trade in self.data.trades("7203")]

        self.assertEqual(["2846.0", "2847.0"], prices)

    def test_the_tape_is_bounded_by_its_configured_length(self):
        harness = EngineHarness(tape_length=2)

        for index in range(4):
            harness.sell("S%d" % index, 100, "2846")
            harness.buy("B%d" % index, 100, "2846")

        trades = harness.market("DAY").data.trades("7203")

        self.assertEqual(2, len(trades))
        # The oldest are the ones dropped.
        self.assertEqual(4, harness.market("DAY").data.statistics("7203").trade_count)

    def test_the_tape_can_be_limited_on_read(self):
        for index in range(3):
            self.harness.sell("S%d" % index, 100, "2846")
            self.harness.buy("B%d" % index, 100, "2846")

        self.assertEqual(2, len(self.data.trades("7203", limit=2)))

    def test_an_unknown_symbol_has_no_tape(self):
        self.assertIsNone(self.data.trades("nope"))


class StatisticsTest(unittest.TestCase):

    def setUp(self):
        self.harness = EngineHarness()
        self.data = self.harness.market("DAY").data

    def _trade(self, price, quantity=100, tag="X"):
        self.harness.sell("S-%s" % tag, quantity, price)
        self.harness.buy("B-%s" % tag, quantity, price)

    def test_open_high_low_last_track_the_trade_sequence(self):
        self._trade("2846", tag="1")
        self._trade("2850", tag="2")
        self._trade("2840", tag="3")
        self._trade("2845", tag="4")

        stats = self.data.statistics("7203").describe(CODEC)

        self.assertEqual("2846.0", stats["open"])
        self.assertEqual("2850.0", stats["high"])
        self.assertEqual("2840.0", stats["low"])
        self.assertEqual("2845.0", stats["last"])

    def test_volume_and_trade_count_accumulate(self):
        self._trade("2846", 100, tag="1")
        self._trade("2847", 200, tag="2")

        stats = self.data.statistics("7203").describe(CODEC)

        self.assertEqual(300, stats["volume"])
        self.assertEqual(2, stats["trades"])

    def test_vwap_is_volume_weighted(self):
        self._trade("2846", 100, tag="1")
        self._trade("2850", 300, tag="2")

        # (2846*100 + 2850*300) / 400 = 2849.0
        self.assertEqual("2849.0000",
                         self.data.statistics("7203").describe(CODEC)["vwap"])

    def test_an_instrument_without_trades_has_empty_statistics(self):
        self.harness.buy("C1", 100, "2845.5")

        stats = self.data.statistics("7203").describe(CODEC)

        self.assertIsNone(stats["last"])
        self.assertEqual(0, stats["volume"])
        self.assertIsNone(stats["vwap"])

    def test_statistics_can_be_reset_for_a_new_session(self):
        self._trade("2846")

        self.data.reset_statistics("7203")

        stats = self.data.statistics("7203").describe(CODEC)
        self.assertEqual(0, stats["volume"])
        self.assertEqual([], self.data.trades("7203"))

    def test_resetting_without_a_symbol_clears_everything(self):
        self._trade("2846", tag="1")

        self.data.reset_statistics()

        self.assertEqual(0, self.data.statistics("7203").volume)

    def test_summary_has_one_row_per_instrument(self):
        self._trade("2846", tag="1")
        self.harness.buy("C-9984", 100, "150", symbol="9984")

        rows = {row["symbol"]: row for row in self.data.summary()}

        self.assertEqual("2846.0", rows["7203"]["last"])
        self.assertEqual(100, rows["7203"]["volume"])
        self.assertEqual("150.0", rows["9984"]["bid"])
        self.assertIsNone(rows["9984"]["last"])


class PublishingTest(unittest.TestCase):

    def setUp(self):
        self.publisher = RecordingPublisher()
        self.harness = EngineHarness(publisher=self.publisher)

    def test_resting_an_order_publishes_the_new_top_of_book(self):
        self.harness.buy("C1", 100, "2845.5")

        updates = self.publisher.of("book:DAY:7203")

        self.assertEqual(1, len(updates))
        self.assertEqual("2845.5", updates[0]["bid"])

    def test_a_trade_publishes_on_the_trade_topic(self):
        self.harness.sell("S1O", 100, "2846")
        self.harness.buy("B1", 100, "2846")

        trades = self.publisher.of("trade:DAY:7203")

        self.assertEqual(1, len(trades))
        self.assertEqual("2846.0", trades[0]["price"])

    def test_topics_are_namespaced_by_market(self):
        self.harness.buy("C1", 100, "2845.5", market="NGHT")

        self.assertIn("book:NGHT:7203", self.publisher.topics())

    def test_a_rejected_order_publishes_nothing(self):
        self.harness.buy("C1", 150, "2845.5")  # odd lot

        self.assertEqual([], self.publisher.topics())

    def test_a_failing_subscriber_cannot_disturb_matching(self):
        class Broken(object):
            def publish(self, topic, data):
                raise RuntimeError("publisher exploded")

        harness = EngineHarness(publisher=Broken())

        with self.assertLogs("exchangesim.core.marketdata", level="ERROR"):
            events = harness.buy("C1", 100, "2845.5")

        self.assertEqual(["OrderAccepted"], [e.name for e in events])


class MarketDescriptionTest(unittest.TestCase):

    def test_describe_summarises_the_market(self):
        harness = EngineHarness()
        harness.buy("C1", 100, "2845.5")
        harness.market("DAY").set_state(TradingState.HALTED, symbol="9984")

        described = harness.market("DAY").describe()

        self.assertEqual("DAY", described["market"])
        self.assertEqual(TradingState.OPEN, described["state"])
        self.assertEqual({"9984": TradingState.HALTED}, described["overrides"])
        self.assertEqual(1, described["resting_orders"])


if __name__ == "__main__":
    unittest.main()
