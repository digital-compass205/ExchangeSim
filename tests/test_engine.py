"""Engine: order lifecycle, validation, the ClOrdID chain and bulk cancels."""

import unittest

from exchangesim.core.enums import (
    CancelRejectReason,
    CancelReason,
    ExecInst,
    OrderStatus,
    RejectReason,
    Side,
    StpMode,
    TimeInForce,
    TradingState,
)
from exchangesim.core.validation import ValidationLimits

from .coresupport import CODEC, EngineHarness, events_of, ladder


class NewOrderTest(unittest.TestCase):

    def setUp(self):
        self.harness = EngineHarness()

    def test_a_valid_order_is_accepted_and_rests(self):
        events = self.harness.buy("C1", 100, "2845.5")

        self.assertEqual(["OrderAccepted"], [e.name for e in events])
        self.assertEqual([("2845.5", 100)], ladder(self.harness.book(), "bids"))

    def test_the_order_receives_a_venue_identifier(self):
        self.harness.buy("C1", 100, "2845.5")

        order = self.harness.order("C1")

        self.assertTrue(order.order_id.startswith("O"))
        self.assertLessEqual(len(order.order_id), 20)

    def test_two_orders_cross(self):
        self.harness.sell("C1", 100, "2846")
        events = self.harness.buy("C2", 100, "2846")

        self.assertEqual(1, len(events_of(events, "TradeExecuted")))
        self.assertEqual(OrderStatus.FILLED, self.harness.order("C2").status)

    def test_markets_have_separate_books(self):
        self.harness.sell("C1", 100, "2846", market="NGHT")
        events = self.harness.buy("C2", 100, "2846", market="DAY")

        self.assertEqual([], events_of(events, "TradeExecuted"))
        self.assertEqual([("2846.0", 100)], ladder(self.harness.book(), "bids"))

    def test_an_unknown_market_is_rejected(self):
        events = self.harness.buy("C1", 100, "2845.5", market="NOPE")

        self.assertEqual(RejectReason.OTHER, events[0].reason)

    def test_a_duplicate_clordid_is_rejected(self):
        self.harness.buy("C1", 100, "2845.5")

        events = self.harness.buy("C1", 100, "2845.5")

        self.assertEqual(["OrderRejected"], [e.name for e in events])
        self.assertEqual(RejectReason.DUPLICATE_ORDER, events[0].reason)

    def test_the_same_clordid_from_another_session_is_fine(self):
        self.harness.buy("C1", 100, "2845.5", session="S1")

        events = self.harness.buy("C1", 100, "2845.5", session="S2")

        self.assertEqual(["OrderAccepted"], [e.name for e in events])


class ValidationTest(unittest.TestCase):

    def setUp(self):
        self.harness = EngineHarness()

    def test_an_unknown_symbol_is_rejected(self):
        events = self.harness.buy("C1", 100, "2845.5", symbol="9999")

        self.assertEqual(RejectReason.UNKNOWN_SYMBOL, events[0].reason)

    def test_an_untradable_symbol_is_rejected(self):
        events = self.harness.buy("C1", 100, "100", symbol="0000")

        self.assertEqual(RejectReason.UNKNOWN_SYMBOL, events[0].reason)

    def test_orders_are_refused_while_the_market_is_closed(self):
        self.harness.market("DAY").set_state(TradingState.CLOSED)

        events = self.harness.buy("C1", 100, "2845.5")

        self.assertEqual(RejectReason.MARKET_CLOSED, events[0].reason)

    def test_orders_are_refused_for_a_halted_instrument_only(self):
        self.harness.market("DAY").set_state(TradingState.HALTED, symbol="7203")

        halted = self.harness.buy("C1", 100, "2845.5", symbol="7203")
        other = self.harness.buy("C2", 100, "150", symbol="9984")

        self.assertEqual(RejectReason.MARKET_CLOSED, halted[0].reason)
        self.assertEqual(["OrderAccepted"], [e.name for e in other])

    def test_an_odd_lot_is_rejected(self):
        events = self.harness.buy("C1", 150, "2845.5")

        self.assertEqual(RejectReason.INVALID_QUANTITY, events[0].reason)
        self.assertIn("lot size", events[0].text)

    def test_a_zero_quantity_is_rejected(self):
        events = self.harness.buy("C1", 0, "2845.5")

        self.assertEqual(RejectReason.INVALID_QUANTITY, events[0].reason)

    def test_quantity_above_five_percent_of_shares_outstanding_is_rejected(self):
        # 7203 has 1,000,000 shares outstanding, so the cap is 50,000.
        events = self.harness.buy("C1", 50100, "2845.5")

        self.assertEqual(RejectReason.EXCEEDS_QUANTITY_LIMIT, events[0].reason)

    def test_quantity_at_the_cap_is_accepted(self):
        # 1301 is cheap enough that the value cap does not bind first.
        events = self.harness.buy("C1", 50000, "100", symbol="1301")

        self.assertEqual(["OrderAccepted"], [e.name for e in events])

    def test_a_price_outside_the_band_is_rejected(self):
        # Base 2845.5 sits in the 1,000+ bracket, a band of 300, so the
        # permitted range is 2545.5 to 3145.5.
        events = self.harness.buy("C1", 100, "3146")

        self.assertEqual(RejectReason.PRICE_OUTSIDE_BAND, events[0].reason)

    def test_a_price_at_the_band_edge_is_accepted(self):
        events = self.harness.buy("C1", 100, "2545.5")

        self.assertEqual(["OrderAccepted"], [e.name for e in events])

    def test_an_off_tick_price_is_rejected(self):
        # Above 3,000 the tick is 0.5, so 3100.1 is invalid.
        events = self.harness.buy("C1", 100, "3100.1")

        self.assertEqual(RejectReason.TICK_SIZE, events[0].reason)

    def test_an_on_tick_price_is_accepted(self):
        events = self.harness.buy("C1", 100, "3100.5")

        self.assertEqual(["OrderAccepted"], [e.name for e in events])

    def test_order_value_above_the_cap_is_rejected(self):
        # 100,000,000 JPY cap. 9984's band tops out at 200, so 500,100 shares
        # at 200 is 100,020,000 -- just over, and inside the price band.
        events = self.harness.buy("C1", 500100, "200", symbol="9984")

        self.assertEqual(RejectReason.EXCEEDS_VALUE_LIMIT, events[0].reason)

    def test_the_notional_check_can_be_waived(self):
        events = self.harness.buy("C1", 500100, "200", symbol="9984",
                                  exec_inst=(ExecInst.IGNORE_NOTIONAL_CHECK,))

        self.assertEqual(["OrderAccepted"], [e.name for e in events])

    def test_min_qty_without_ioc_is_rejected(self):
        events = self.harness.buy("C1", 100, "2845.5", min_qty=50)

        self.assertEqual(RejectReason.UNSUPPORTED_CHARACTERISTIC, events[0].reason)
        self.assertIn("IOC", events[0].text)

    def test_min_qty_above_the_order_quantity_is_rejected(self):
        events = self.harness.buy("C1", 100, "2845.5",
                                  time_in_force=TimeInForce.IOC, min_qty=200)

        self.assertEqual(RejectReason.INVALID_QUANTITY, events[0].reason)

    def test_post_only_with_ioc_is_rejected(self):
        events = self.harness.buy("C1", 100, "2845.5",
                                  time_in_force=TimeInForce.IOC,
                                  exec_inst=(ExecInst.POST_ONLY,))

        self.assertEqual(RejectReason.UNSUPPORTED_CHARACTERISTIC, events[0].reason)

    def test_a_rejected_order_does_not_reserve_its_clordid(self):
        self.harness.buy("C1", 150, "2845.5")  # odd lot, rejected

        events = self.harness.buy("C1", 100, "2845.5")

        self.assertEqual(["OrderAccepted"], [e.name for e in events])


class CancelTest(unittest.TestCase):

    def setUp(self):
        self.harness = EngineHarness()
        # 200 shares, so a 100-share fill leaves it partially filled and the
        # lot size of 100 is respected throughout.
        self.harness.buy("C1", 200, "2845.5")

    def test_a_live_order_is_cancelled(self):
        events = self.harness.cancel("C2", "C1")

        self.assertEqual(["OrderCancelled"], [e.name for e in events])
        self.assertEqual(CancelReason.USER_REQUEST, events[0].reason)
        self.assertEqual(0, len(self.harness.book()))

    def test_the_cancel_advances_the_clordid_chain(self):
        self.harness.cancel("C2", "C1")

        order = self.harness.order("C1")

        self.assertEqual("C2", order.cl_ord_id)
        self.assertEqual("C1", order.orig_cl_ord_id)

    def test_an_unknown_order_is_reported_as_such(self):
        events = self.harness.cancel("C2", "NEVER-EXISTED")

        self.assertEqual(["CancelRejected"], [e.name for e in events])
        self.assertEqual(CancelRejectReason.UNKNOWN_ORDER, events[0].reason)

    def test_cancelling_twice_is_too_late(self):
        self.harness.cancel("C2", "C1")

        events = self.harness.cancel("C3", "C2")

        self.assertEqual(CancelRejectReason.TOO_LATE_TO_CANCEL, events[0].reason)

    def test_a_superseded_clordid_is_no_longer_a_valid_handle(self):
        # The specification is explicit that OrigClOrdID is the *previous*
        # ClOrdID, not the initial one.
        self.harness.replace("C2", "C1", quantity=300)

        events = self.harness.cancel("C3", "C1")

        self.assertEqual(CancelRejectReason.UNKNOWN_ORDER, events[0].reason)

    def test_a_reused_clordid_on_the_cancel_is_rejected(self):
        events = self.harness.cancel("C1", "C1")

        self.assertEqual(CancelRejectReason.DUPLICATE_CLORDID, events[0].reason)

    def test_a_mismatched_side_is_rejected(self):
        events = self.harness.cancel("C2", "C1", side=Side.SELL)

        self.assertEqual(CancelRejectReason.MISMATCHED_FIELD, events[0].reason)

    def test_a_mismatched_symbol_is_rejected(self):
        events = self.harness.cancel("C2", "C1", symbol="9984")

        self.assertEqual(CancelRejectReason.MISMATCHED_FIELD, events[0].reason)

    def test_another_session_cannot_cancel_the_order(self):
        events = self.harness.cancel("X1", "C1", session="S2")

        self.assertEqual(CancelRejectReason.UNKNOWN_ORDER, events[0].reason)

    def test_a_partially_filled_order_can_be_cancelled(self):
        self.harness.sell("S1O", 100, "2845.5")  # fills 100 of C1's 200

        events = self.harness.cancel("C2", "C1")

        self.assertEqual(["OrderCancelled"], [e.name for e in events])
        self.assertEqual(100, self.harness.order("C1").cum_qty)


class ReplaceTest(unittest.TestCase):

    def setUp(self):
        self.harness = EngineHarness()
        self.harness.buy("C1", 200, "2845.5")

    def test_increasing_quantity_is_accepted(self):
        events = self.harness.replace("C2", "C1", quantity=300)

        self.assertEqual("OrderReplaced", events[0].name)
        self.assertEqual(300, self.harness.order("C1").quantity)
        self.assertEqual([("2845.5", 300)], ladder(self.harness.book(), "bids"))

    def test_changing_price_moves_the_order(self):
        self.harness.replace("C2", "C1", price="2840")

        self.assertEqual([("2840.0", 200)], ladder(self.harness.book(), "bids"))

    def test_the_replace_advances_the_clordid_chain(self):
        events = self.harness.replace("C2", "C1", quantity=300)

        self.assertEqual("C1", events[0].previous_cl_ord_id)
        self.assertEqual("C2", self.harness.order("C1").cl_ord_id)

    def test_reducing_quantity_keeps_priority(self):
        self.harness.buy("D1", 100, "2845.5")  # queued behind C1

        events = self.harness.replace("C2", "C1", quantity=100)

        self.assertTrue(events[0].kept_priority)

    def test_increasing_quantity_loses_priority(self):
        events = self.harness.replace("C2", "C1", quantity=300)

        self.assertFalse(events[0].kept_priority)

    def test_an_amend_that_crosses_executes_immediately(self):
        self.harness.sell("S1O", 200, "2850")

        events = self.harness.replace("C2", "C1", price="2850")

        self.assertEqual(1, len(events_of(events, "TradeExecuted")))
        self.assertEqual(OrderStatus.FILLED, self.harness.order("C1").status)

    def test_an_invalid_new_price_is_rejected_without_changing_the_order(self):
        events = self.harness.replace("C2", "C1", price="9999")

        self.assertEqual(["CancelRejected"], [e.name for e in events])
        self.assertEqual(CancelRejectReason.PRICE_OUTSIDE_BAND, events[0].reason)
        self.assertEqual(CODEC.parse("2845.5"), self.harness.order("C1").price)

    def test_reducing_below_the_executed_quantity_is_rejected(self):
        self.harness.sell("S1O", 100, "2845.5")  # 100 of C1 executes

        events = self.harness.replace("C2", "C1", quantity=100)

        self.assertEqual(["CancelRejected"], [e.name for e in events])
        self.assertEqual(200, self.harness.order("C1").quantity)

    def test_replacing_an_unknown_order_is_rejected(self):
        events = self.harness.replace("C2", "NEVER-EXISTED", quantity=300)

        self.assertEqual(CancelRejectReason.UNKNOWN_ORDER, events[0].reason)
        self.assertTrue(events[0].is_replace)

    def test_a_rejected_replace_does_not_consume_its_clordid(self):
        self.harness.replace("C2", "C1", price="9999")  # rejected

        events = self.harness.replace("C2", "C1", quantity=300)

        self.assertEqual("OrderReplaced", events[0].name)


class BulkCancelTest(unittest.TestCase):

    def setUp(self):
        self.harness = EngineHarness()

    def test_cancel_on_disconnect_removes_only_that_session(self):
        self.harness.buy("A1", 100, "2845.5", session="S1")
        self.harness.buy("A2", 100, "2840", session="S1")
        self.harness.buy("B1", 100, "2830", session="S2")

        events = self.harness.engine.cancel_session_orders(
            "S1", CancelReason.CANCEL_ON_DISCONNECT, "connection lost")

        self.assertEqual(2, len(events))
        self.assertEqual([("2830.0", 100)], ladder(self.harness.book(), "bids"))

    def test_cancel_on_disconnect_spans_every_market(self):
        self.harness.buy("A1", 100, "2845.5", session="S1", market="DAY")
        self.harness.buy("A2", 100, "2845.5", session="S1", market="NGHT")

        events = self.harness.engine.cancel_session_orders(
            "S1", CancelReason.CANCEL_ON_DISCONNECT)

        self.assertEqual(2, len(events))

    def test_closing_a_market_expires_resting_orders(self):
        self.harness.buy("A1", 100, "2845.5")
        self.harness.buy("A2", 100, "2840")

        events = self.harness.market("DAY").set_state(TradingState.CLOSED)

        cancels = events_of(events, "OrderCancelled")
        self.assertEqual(2, len(cancels))
        self.assertEqual(CancelReason.MARKET_CLOSED, cancels[0].reason)
        self.assertEqual(0, len(self.harness.book()))

    def test_halting_one_instrument_expires_only_its_orders(self):
        self.harness.buy("A1", 100, "2845.5", symbol="7203")
        self.harness.buy("A2", 100, "150", symbol="9984")

        events = self.harness.market("DAY").set_state(
            TradingState.HALTED, symbol="7203")

        cancels = events_of(events, "OrderCancelled")
        self.assertEqual(1, len(cancels))
        self.assertEqual("7203", cancels[0].order.symbol)

    def test_opening_a_market_does_not_expire_anything(self):
        self.harness.market("DAY").set_state(TradingState.PRE_OPEN)
        self.harness.buy("A1", 100, "2845.5")

        events = self.harness.market("DAY").set_state(TradingState.OPEN)

        self.assertEqual([], events_of(events, "OrderCancelled"))


class QueryTest(unittest.TestCase):

    def setUp(self):
        self.harness = EngineHarness()
        self.harness.buy("A1", 100, "2845.5", session="S1", symbol="7203")
        self.harness.buy("B1", 100, "150", session="S2", symbol="9984")
        self.harness.buy("A2", 100, "2840", session="S1", market="NGHT")

    def test_all_live_orders(self):
        self.assertEqual(3, len(self.harness.engine.orders()))

    def test_filter_by_session(self):
        self.assertEqual(2, len(self.harness.engine.orders(session_key="S1")))

    def test_filter_by_symbol(self):
        self.assertEqual(1, len(self.harness.engine.orders(symbol="9984")))

    def test_filter_by_market(self):
        self.assertEqual(1, len(self.harness.engine.orders(market="NGHT")))

    def test_cancelled_orders_are_excluded_unless_asked_for(self):
        self.harness.cancel("A1C", "A1", session="S1")

        self.assertEqual(2, len(self.harness.engine.orders()))
        self.assertEqual(3, len(self.harness.engine.orders(live_only=False)))


class AccumulationTest(unittest.TestCase):
    """Auction phases accept orders without matching them."""

    def setUp(self):
        self.harness = EngineHarness(state=TradingState.PRE_OPEN)

    def test_crossing_orders_rest_without_trading(self):
        self.harness.sell("S1O", 100, "2846")
        events = self.harness.buy("B1", 100, "2846")

        self.assertEqual(["OrderAccepted"], [e.name for e in events])
        self.assertTrue(self.harness.book().is_crossed)

    def test_orders_match_once_the_market_opens(self):
        self.harness.sell("S1O", 100, "2846")
        self.harness.buy("B1", 100, "2846")

        self.harness.market("DAY").set_state(TradingState.OPEN)
        # A fresh aggressive order now trades against the crossed book.
        events = self.harness.sell("S2O", 100, "2846")

        self.assertEqual(1, len(events_of(events, "TradeExecuted")))


class StpIntegrationTest(unittest.TestCase):

    def test_the_engine_applies_the_market_stp_mode(self):
        harness = EngineHarness(stp_mode=StpMode.CANCEL_NEWEST)
        harness.sell("S1O", 100, "2846", mpid="A")

        events = harness.buy("B1", 100, "2846", mpid="A")

        cancels = events_of(events, "OrderCancelled")
        self.assertEqual(CancelReason.SELF_TRADE_PREVENTION, cancels[0].reason)
        self.assertEqual(0, harness.order("B1").cum_qty)


if __name__ == "__main__":
    unittest.main()
