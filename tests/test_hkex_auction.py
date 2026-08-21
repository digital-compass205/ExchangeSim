"""The HKEX Pre-opening Session and Closing Auction Session.

The uncrossing algorithm is the core's and is tested in tests/test_auction.py.
What is exercised here is everything Hong Kong wraps around it: the reference
price each session is anchored on, the two-stage price limits, the treatment of
orders carried in from continuous trading, and how it all reads on the wire.
"""

import unittest

from exchangesim.control.commands import CommandError
from exchangesim.core.enums import TradingState
from exchangesim.fix import constants as C
from exchangesim.venues.hkex import dictionary as D
from exchangesim.venues.hkex.auctions import AuctionSession, Stage

from .hkexsupport import BROKER1, BROKER2, SYMBOL, VenueHarness, venue_config


class AuctionTestCase(unittest.TestCase):

    def setUp(self):
        self.harness = VenueHarness()
        self.addCleanup(self.harness.close)
        self.client = self.harness.client(BROKER1)
        self.other = self.harness.client(BROKER2)
        self.client.drain()
        self.other.drain()

    # -- driving the session ------------------------------------------------

    def open_pos(self):
        """Deliberately does not drain: opening a session can itself cancel
        carried-forward orders, and those reports are worth asserting on."""
        return self.harness.command("state.set", market="MAIN",
                                    state="PRE_OPEN")

    def open_cas(self):
        return self.harness.command("state.set", market="MAIN",
                                    state="CLOSING_AUCTION")

    def lock(self):
        return self.harness.command("auction.lock", market="MAIN")

    def close_to(self, state):
        self.harness.command("state.set", market="MAIN", state=state)

    def drain_all(self):
        self.client.drain()
        self.other.drain()

    def auction(self, symbol=SYMBOL):
        return self.harness.command("auction", market="MAIN", symbol=symbol)

    # -- order helpers ------------------------------------------------------

    def buy(self, cl_ord_id, quantity, price, client=None):
        (client or self.client).new_order(
            cl_ord_id, side=D.SideValue.BUY, quantity=quantity, price=price)
        return (client or self.client)

    def sell(self, cl_ord_id, quantity, price, client=None):
        (client or self.other).new_order(
            cl_ord_id, side=D.SideValue.SELL, quantity=quantity, price=price)
        return (client or self.other)

    def at_auction(self, cl_ord_id, side, quantity, client=None):
        (client or self.client).new_order(
            cl_ord_id, side=side, quantity=quantity, price=None,
            ord_type=D.OrdType.MARKET)

    def book(self, symbol=SYMBOL):
        return self.harness.command("book", market="MAIN", symbol=symbol)

    def fills_for(self, client):
        return [report for report in client.reports()
                if report.get(D.EXEC_TYPE) == D.ExecType.TRADE]


class SessionLifecycleTest(AuctionTestCase):

    def test_pre_open_starts_a_pre_opening_session(self):
        self.open_pos()

        self.assertEqual(AuctionSession.POS, self.auction()["auction"])
        self.assertEqual(Stage.INPUT, self.auction()["stage"])

    def test_the_closing_auction_phase_starts_a_CAS(self):
        self.open_cas()

        self.assertEqual(AuctionSession.CAS, self.auction()["auction"])

    def test_the_two_POS_periods_are_one_session(self):
        self.open_pos()
        self.harness.command("state.set", market="MAIN",
                             state="OPENING_AUCTION")

        # The reference price would be refixed if a second session had started.
        self.assertEqual(AuctionSession.POS, self.auction()["auction"])

    def test_locking_advances_the_opening_auction_to_its_second_phase(self):
        self.open_pos()

        self.lock()

        self.assertEqual(TradingState.OPENING_AUCTION,
                         self.harness.venue.markets["MAIN"].state.market_state)

    def test_locking_a_closing_auction_leaves_its_phase_alone(self):
        self.open_cas()

        self.lock()

        self.assertEqual(TradingState.CLOSING_AUCTION,
                         self.harness.venue.markets["MAIN"].state.market_state)

    def test_opening_the_market_ends_the_session(self):
        self.open_pos()

        self.close_to("OPEN")

        with self.assertRaises(CommandError):
            self.auction()

    def test_asking_about_an_auction_that_is_not_running_is_refused(self):
        with self.assertRaises(CommandError):
            self.auction()

    def test_locking_when_nothing_is_running_is_refused(self):
        with self.assertRaises(CommandError):
            self.lock()


class AcknowledgementTest(AuctionTestCase):
    """An order accumulated by a call auction is still acknowledged once.

    The auction path accumulates without matching, so the acceptance has to
    come from one place or the other -- never both.
    """

    def test_an_order_entered_into_the_auction_is_acknowledged_once(self):
        self.open_pos()
        self.drain_all()

        self.buy("PA1", 100, "395.800")
        reports = self.client.reports()

        self.assertEqual(1, len(reports))
        self.assertEqual(D.ExecType.NEW, reports[0].get(D.EXEC_TYPE))
        self.assertEqual("100", reports[0].get(D.LEAVES_QTY))

    def test_an_auction_fill_follows_the_acknowledgement_it_already_had(self):
        self.open_pos()
        self.drain_all()
        self.buy("PA2", 100, "395.800")
        self.sell("PA3", 100, "395.800")
        self.drain_all()

        self.lock()
        self.close_to("OPEN")

        reports = self.client.reports()
        self.assertEqual([D.ExecType.TRADE],
                         [report.get(D.EXEC_TYPE) for report in reports])


class ReferencePriceTest(AuctionTestCase):

    def test_the_pre_opening_session_anchors_on_the_previous_close(self):
        self.open_pos()

        self.assertEqual("395.800", self.auction()["reference_price"])

    def test_the_closing_auction_anchors_on_the_last_traded_price(self):
        self.sell("R1", 100, "396.000")
        self.buy("R2", 100, "396.000")
        self.drain_all()

        self.open_cas()

        self.assertEqual("396.000", self.auction()["reference_price"])

    def test_it_falls_back_to_the_previous_close_with_no_trade(self):
        self.open_cas()

        self.assertEqual("395.800", self.auction()["reference_price"])

    def test_it_can_be_overridden(self):
        self.open_cas()

        self.harness.command("auction.reference", market="MAIN",
                             symbol=SYMBOL, price="400.000")

        self.assertEqual("400.000", self.auction()["reference_price"])

    def test_an_override_for_an_unknown_security_is_refused(self):
        self.open_cas()

        with self.assertRaises(CommandError):
            self.harness.command("auction.reference", market="MAIN",
                                 symbol="99999", price="400.000")


class PriceLimitTest(AuctionTestCase):
    """Stage 1 is a band around the reference; stage 2 is the book itself."""

    def test_the_pre_opening_band_is_15_percent_of_the_reference(self):
        self.open_pos()

        state = self.auction()

        # 395.800 +/- 15% is 336.430 to 455.170.
        self.assertEqual("336.430", state["price_low"])
        self.assertEqual("455.170", state["price_high"])

    def test_the_closing_band_is_5_percent_of_the_reference(self):
        self.open_cas()

        state = self.auction()

        self.assertEqual("376.010", state["price_low"])
        self.assertEqual("415.590", state["price_high"])

    def test_a_price_outside_the_stage_one_band_is_rejected(self):
        self.open_cas()

        self.buy("L1", 100, "300.000")
        report = self.client.reports()[-1]

        self.assertEqual(D.ExecType.REJECTED, report.get(D.EXEC_TYPE))
        self.assertEqual(str(D.OrdRejReason.PRICE_EXCEEDS_BAND),
                         report.get(D.ORD_REJ_REASON))
        self.assertIn("CAS input limit", report.get(D.REJECT_TEXT))

    def test_a_price_inside_the_band_is_accepted(self):
        self.open_cas()

        self.buy("L2", 100, "390.000")

        self.assertEqual(D.ExecType.NEW,
                         self.client.reports()[-1].get(D.EXEC_TYPE))

    def test_locking_pins_the_band_to_the_recorded_bid_and_ask(self):
        self.open_cas()
        self.buy("L3", 100, "390.000")
        self.sell("L4", 100, "400.000")
        self.drain_all()

        self.lock()
        state = self.auction()

        # The band is the interval the two span, whichever way round they are.
        self.assertEqual("390.000", state["price_low"])
        self.assertEqual("400.000", state["price_high"])

    def test_the_pinned_band_holds_even_when_the_book_does_not_cross(self):
        """HKEX's own worked example: a bid of 98 and an ask of 101."""
        self.open_cas()
        self.buy("L5", 100, "390.000")
        self.sell("L6", 100, "400.000")
        self.drain_all()
        self.lock()

        self.buy("L7", 100, "395.000")

        self.assertEqual(D.ExecType.NEW,
                         self.client.reports()[-1].get(D.EXEC_TYPE))

    def test_a_price_outside_the_pinned_band_is_rejected(self):
        self.open_cas()
        self.buy("L8", 100, "390.000")
        self.sell("L9", 100, "400.000")
        self.drain_all()
        self.lock()

        self.buy("LA", 100, "380.000")
        report = self.client.reports()[-1]

        self.assertEqual(D.ExecType.REJECTED, report.get(D.EXEC_TYPE))
        self.assertIn("CAS locked limit", report.get(D.REJECT_TEXT))

    def test_an_at_auction_order_has_no_price_to_check(self):
        self.open_cas()
        self.buy("LB", 100, "390.000")
        self.sell("LC", 100, "400.000")
        self.drain_all()
        self.lock()

        self.at_auction("LD", D.SideValue.BUY, 100)

        self.assertEqual(D.ExecType.NEW,
                         self.client.reports()[-1].get(D.EXEC_TYPE))


class CancellationTest(AuctionTestCase):

    def test_orders_may_be_cancelled_during_the_input_period(self):
        self.open_cas()
        self.buy("K1", 100, "390.000")
        self.drain_all()

        self.client.cancel("K2", "K1", quantity=100)

        self.assertEqual(D.ExecType.CANCELED,
                         self.client.reports()[-1].get(D.EXEC_TYPE))

    def test_the_no_cancellation_period_refuses_a_cancel(self):
        self.open_cas()
        self.buy("K3", 100, "390.000")
        self.drain_all()
        self.lock()

        self.client.cancel("K4", "K3", quantity=100)
        reject = self.client.received()[0]

        self.assertEqual(C.ORDER_CANCEL_REJECT, reject.msg_type)
        self.assertEqual(str(D.CxlRejReason.TOO_LATE_TO_CANCEL),
                         reject.get(D.CXL_REJ_REASON))
        self.assertIn("No Cancellation Period", reject.get(D.REJECT_TEXT))

    def test_it_refuses_an_amend_too(self):
        self.open_cas()
        self.buy("K5", 100, "390.000")
        self.drain_all()
        self.lock()

        self.client.replace("K6", "K5", quantity=200, price="391.000")
        reject = self.client.received()[0]

        self.assertEqual(C.ORDER_CANCEL_REJECT, reject.msg_type)
        self.assertEqual(D.CxlRejResponseTo.CANCEL_REPLACE_REQUEST,
                         reject.get(D.CXL_REJ_RESPONSE_TO))

    def test_the_order_survives_the_refused_cancel(self):
        self.open_cas()
        self.buy("K7", 100, "390.000")
        self.drain_all()
        self.lock()
        self.client.cancel("K8", "K7", quantity=100)

        self.assertEqual(1, len(self.harness.venue.engine.orders()))


class OrderTypeTest(AuctionTestCase):

    def test_an_IOC_order_is_refused_during_an_auction(self):
        self.open_cas()

        self.client.new_order("O1", quantity=100, price="390.000",
                              tif=D.TimeInForce.IOC)
        report = self.client.reports()[-1]

        self.assertEqual(D.ExecType.REJECTED, report.get(D.EXEC_TYPE))
        self.assertIn("cannot be entered during the CAS",
                      report.get(D.REJECT_TEXT))

    def test_an_at_auction_order_rests_rather_than_expiring(self):
        self.open_cas()

        self.at_auction("O2", D.SideValue.BUY, 100)
        report = self.client.reports()[-1]

        self.assertEqual(D.ExecType.NEW, report.get(D.EXEC_TYPE))
        self.assertEqual(1, len(self.harness.venue.engine.orders()))

    def test_an_at_auction_order_stays_out_of_the_visible_book(self):
        self.open_cas()

        self.at_auction("O3", D.SideValue.BUY, 100)

        self.assertEqual([], self.book()["bids"])

    def test_nothing_matches_while_the_auction_accumulates(self):
        self.open_cas()
        self.buy("O4", 100, "396.000")
        self.sell("O5", 100, "396.000")

        self.assertEqual([], self.fills_for(self.client))
        self.assertEqual([], self.fills_for(self.other))


class IndicativePriceTest(AuctionTestCase):

    def test_the_indicative_price_and_volume_are_reported(self):
        self.open_cas()
        self.buy("I1", 1000, "396.000")
        self.sell("I2", 600, "395.000")
        self.drain_all()

        state = self.auction()

        self.assertEqual("396.000", state["iep"])
        self.assertEqual(600, state["iev"])

    def test_the_imbalance_and_its_side_are_reported(self):
        self.open_cas()
        self.buy("I3", 1000, "396.000")
        self.sell("I4", 600, "395.000")
        self.drain_all()

        state = self.auction()

        self.assertEqual(400, state["imbalance"])
        self.assertEqual("BUY", state["imbalance_side"])

    def test_an_uncrossed_book_reports_no_indicative_price(self):
        self.open_cas()
        self.buy("I5", 100, "390.000")
        self.sell("I6", 400, "400.000")
        self.drain_all()

        state = self.auction()

        self.assertIsNone(state["iep"])
        self.assertEqual("book does not cross", state["reason"])


class UncrossingTest(AuctionTestCase):

    def test_the_auction_trades_everyone_at_one_price(self):
        self.open_cas()
        self.buy("U1", 1000, "396.000")
        self.sell("U2", 600, "395.000")
        self.drain_all()
        self.lock()

        self.close_to("CLOSED")

        mine = self.fills_for(self.client)
        theirs = self.fills_for(self.other)
        self.assertEqual("396.000", mine[0].get(D.LAST_PX))
        self.assertEqual("396.000", theirs[0].get(D.LAST_PX))

    def test_both_sides_of_an_auction_trade_are_passive(self):
        self.open_cas()
        self.buy("U3", 100, "396.000")
        self.sell("U4", 100, "395.000")
        self.drain_all()

        self.close_to("CLOSED")

        for report in self.fills_for(self.client) + self.fills_for(self.other):
            self.assertEqual(C.NO, report.get(D.AGGRESSOR_INDICATOR),
                             "an auction has no aggressor")

    def test_at_auction_orders_are_filled_first(self):
        self.open_pos()
        self.buy("U5", 1000, "396.000")
        self.at_auction("U6", D.SideValue.BUY, 500)
        self.sell("U7", 600, "395.000")
        self.drain_all()

        self.close_to("OPEN")

        by_id = dict((report.get(D.CL_ORD_ID), report)
                     for report in self.fills_for(self.client))
        self.assertEqual("500", by_id["U6"].get(D.LAST_QTY))
        self.assertEqual("100", by_id["U5"].get(D.LAST_QTY))

    def test_the_unfilled_limit_balance_is_carried_into_continuous_trading(self):
        self.open_pos()
        self.buy("U8", 1000, "396.000")
        self.sell("U9", 600, "395.000")
        self.drain_all()

        self.close_to("OPEN")

        self.assertEqual([{"price": "396.000", "quantity": 400, "orders": 1}],
                         self.book()["bids"])

    def test_an_unfilled_at_auction_order_is_cancelled(self):
        self.open_pos()
        self.at_auction("UA", D.SideValue.BUY, 500)
        self.drain_all()

        self.close_to("OPEN")

        report = self.client.reports()[-1]
        self.assertEqual(D.ExecType.EXPIRED, report.get(D.EXEC_TYPE))
        self.assertEqual([], self.harness.venue.engine.orders())

    def test_a_book_that_does_not_cross_trades_nothing(self):
        self.open_cas()
        self.buy("UB", 100, "390.000")
        self.sell("UC", 100, "400.000")
        self.drain_all()

        self.close_to("CLOSED")

        self.assertEqual([], self.fills_for(self.client))
        self.assertEqual([], self.fills_for(self.other))

    def test_without_an_IEP_orders_still_match_at_the_reference_price(self):
        """HKEX's worked example: a limit sell below the reference price."""
        self.open_cas()
        self.at_auction("UD", D.SideValue.BUY, 100)
        self.sell("UE", 100, "390.000")
        self.drain_all()

        self.close_to("CLOSED")

        self.assertEqual("395.800", self.fills_for(self.client)[0].get(D.LAST_PX))

    def test_a_limit_buy_below_the_reference_price_does_not_match(self):
        self.open_cas()
        self.buy("UF", 100, "390.000")
        self.at_auction("UG", D.SideValue.SELL, 100, client=self.other)
        self.drain_all()

        self.close_to("CLOSED")

        self.assertEqual([], self.fills_for(self.client))

    def test_the_uncrossing_price_becomes_the_last_traded_price(self):
        self.open_cas()
        self.buy("UH", 100, "396.000")
        self.sell("UI", 100, "395.000")
        self.drain_all()

        self.close_to("CLOSED")

        stats = self.harness.command("stats", market="MAIN", symbol=SYMBOL)
        self.assertEqual("396.000", stats["last"])


class CarriedForwardTest(AuctionTestCase):
    """Orders resting from continuous trading when the auction opens."""

    def test_a_passive_order_outside_the_band_is_carried_in(self):
        self.buy("F1", 100, "370.000")
        self.drain_all()

        self.open_cas()

        self.assertEqual([{"price": "370.000", "quantity": 100, "orders": 1}],
                         self.book()["bids"])

    def test_an_aggressive_order_outside_the_band_is_cancelled(self):
        self.buy("F2", 100, "420.000")
        self.drain_all()

        self.open_cas()

        report = self.client.reports()[-1]
        self.assertEqual(D.ExecType.CANCELED, report.get(D.EXEC_TYPE))
        self.assertEqual(str(D.ExecRestatementReason.MARKET_OPERATION),
                         report.get(D.EXEC_RESTATEMENT_REASON))
        self.assertEqual([], self.book()["bids"])

    def test_a_passive_order_outside_the_band_never_matches(self):
        self.buy("F3", 100, "370.000")
        self.drain_all()
        self.open_cas()
        self.at_auction("F4", D.SideValue.SELL, 100, client=self.other)
        self.drain_all()

        self.close_to("CLOSED")

        self.assertEqual([], self.fills_for(self.client))

    def test_an_order_inside_the_band_is_untouched(self):
        self.buy("F5", 100, "390.000")
        self.drain_all()

        self.open_cas()

        self.assertEqual([], self.client.reports())


class MarketSegmentTest(AuctionTestCase):

    def test_each_segment_runs_its_own_auction(self):
        self.open_cas()

        self.assertIsNotNone(self.harness.venue.auction_state("MAIN"))
        self.assertIsNone(self.harness.venue.auction_state("GEM"))

    def test_an_auction_uncrosses_every_security_in_its_segment(self):
        self.open_cas()
        self.buy("S1", 100, "396.000")
        self.sell("S2", 100, "395.000")
        self.client.new_order("S3", side=D.SideValue.BUY, quantity=1000,
                              price="6.180", symbol="939")
        self.other.new_order("S4", side=D.SideValue.SELL, quantity=1000,
                             price="6.170", symbol="939")
        self.drain_all()

        self.close_to("CLOSED")

        traded = set(report.get(D.SECURITY_ID)
                     for report in self.fills_for(self.client))
        self.assertEqual(set([SYMBOL, "939"]), traded)


class AssumptionTest(AuctionTestCase):

    def test_the_auction_choices_are_declared(self):
        topics = [entry["topic"] for entry
                  in self.harness.command("venue.assumptions")["assumptions"]]

        for topic in ("auction periods", "CAS reference price",
                      "POS reference price",
                      "carried-forward orders outside the auction band",
                      "self-match prevention during an auction"):
            self.assertIn(topic, topics)


if __name__ == "__main__":
    unittest.main()
