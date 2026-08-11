"""Orders entered through the control plane rather than over FIX.

The point of `order.new` is that it is not a parallel path: it builds the same
NewOrderRequest a gateway builds and hands it to the same engine, so validation,
matching and reporting behave identically. The tests that matter most are the
ones where an injected order meets a real FIX client, because that is where a
separate path would show.
"""

import unittest

from exchangesim.control.commands import CommandError
from exchangesim.venues.common_commands import INJECTED_OWNER_PREFIX
from exchangesim.venues.japannext import dictionary as D

from .jnxsupport import CLIENT1, SYMBOL, VenueHarness


class InjectionTest(unittest.TestCase):

    def setUp(self):
        self.harness = VenueHarness()
        self.addCleanup(self.harness.close)
        self.venue = self.harness.venue

    def enter(self, **overrides):
        args = {"market": "DAY", "symbol": SYMBOL, "side": "BUY",
                "quantity": 100, "price": "2845.5"}
        args.update(overrides)
        return self.harness.dispatch("order.new", args)


class AcceptanceTest(InjectionTest):

    def test_an_injected_order_rests_on_the_book(self):
        result = self.enter()

        self.assertEqual("NEW", result["order"]["status"])
        self.assertEqual("2845.5", result["order"]["price"])
        self.assertEqual(100, result["order"]["leaves_qty"])
        self.assertEqual(
            "2845.5",
            self.harness.command("bbo", market="DAY", symbol=SYMBOL)["bid"])

    def test_it_is_tagged_with_its_owner_not_a_session(self):
        self.enter(owner="bot1")

        orders = self.venue.engine.orders()
        self.assertEqual(INJECTED_OWNER_PREFIX + "BOT1",
                         orders[0].session_key)

    def test_the_default_owner_is_used_when_none_is_given(self):
        self.assertEqual("WEB", self.enter()["owner"])

    def test_owners_are_upper_cased_so_they_address_one_book(self):
        self.assertEqual("BOT1", self.enter(owner="bot1")["owner"])

    def test_orders_can_be_listed_by_owner(self):
        self.enter(owner="BOT1", cl_ord_id="A1")
        self.enter(owner="BOT2", cl_ord_id="A2", price="2845.4")

        listed = self.harness.command("orders", owner="BOT1")["orders"]

        self.assertEqual(["A1"], [order["cl_ord_id"] for order in listed])

    def test_asking_by_both_session_and_owner_is_refused(self):
        with self.assertRaises(CommandError):
            self.harness.command("orders", owner="BOT1", session=CLIENT1)

    def test_a_generated_cl_ord_id_is_unique_per_order(self):
        first = self.enter()["order"]["cl_ord_id"]
        second = self.enter(price="2845.4")["order"]["cl_ord_id"]

        self.assertNotEqual(first, second)

    def test_an_explicit_cl_ord_id_is_honoured(self):
        self.assertEqual("MINE-1", self.enter(cl_ord_id="MINE-1")
                         ["order"]["cl_ord_id"])

    def test_a_duplicate_cl_ord_id_is_rejected_as_it_is_over_fix(self):
        self.enter(cl_ord_id="DUP")

        result = self.enter(cl_ord_id="DUP")

        self.assertEqual("REJECTED", result["order"]["status"])
        self.assertEqual("DUPLICATE_ORDER", result["events"][0]["reason"])


class ValidationTest(InjectionTest):
    """The same rules a FIX order meets, reached the same way."""

    def test_a_price_outside_the_band_is_rejected(self):
        result = self.enter(price="9999.0")

        self.assertEqual("REJECTED", result["order"]["status"])
        self.assertEqual("PRICE_OUTSIDE_BAND", result["events"][0]["reason"])

    def test_an_odd_lot_is_rejected(self):
        result = self.enter(quantity=150)

        self.assertEqual("REJECTED", result["order"]["status"])
        self.assertEqual("INVALID_QUANTITY", result["events"][0]["reason"])

    def test_a_closed_market_rejects_it(self):
        self.harness.command("state.set", market="DAY", state="CLOSED")

        result = self.enter()

        self.assertEqual("REJECTED", result["order"]["status"])

    def test_an_unknown_symbol_is_refused_before_the_engine(self):
        with self.assertRaises(CommandError):
            self.enter(symbol="0000")

    def test_an_unknown_market_is_refused(self):
        with self.assertRaises(CommandError):
            self.enter(market="NOPE")

    def test_a_missing_price_is_refused(self):
        with self.assertRaises(CommandError) as caught:
            self.harness.dispatch("order.new", {
                "market": "DAY", "symbol": SYMBOL, "side": "BUY",
                "quantity": 100})

        self.assertIn("price", str(caught.exception))

    def test_an_unparseable_price_is_refused(self):
        with self.assertRaises(CommandError):
            self.enter(price="2845.55")

    def test_a_zero_quantity_is_refused(self):
        with self.assertRaises(CommandError):
            self.enter(quantity=0)

    def test_an_unknown_side_is_refused_with_the_valid_list(self):
        with self.assertRaises(CommandError) as caught:
            self.enter(side="SIDEWAYS")

        self.assertIn("BUY", str(caught.exception))

    def test_an_unknown_time_in_force_is_refused(self):
        with self.assertRaises(CommandError):
            self.enter(tif="GTC")

    def test_an_invalid_owner_is_refused(self):
        for owner in ("", "has space", "way-too-long-an-owner-name", "a/b"):
            with self.assertRaises(CommandError):
                self.enter(owner=owner)


class MeetingAFixClientTest(InjectionTest):
    """Where a separate order-entry path would give itself away."""

    def setUp(self):
        InjectionTest.setUp(self)
        self.client = self.harness.client(CLIENT1)

    def test_an_injected_order_fills_a_resting_fix_order(self):
        self.client.new_order("F1", side=D.SideValue.SELL, quantity=100,
                              price="2845.5")
        self.client.drain()

        result = self.enter(side="BUY", quantity=100, price="2845.5")

        self.assertEqual("FILLED", result["order"]["status"])

    def test_the_fix_client_receives_its_own_execution_report(self):
        self.client.new_order("F2", side=D.SideValue.SELL, quantity=100,
                              price="2845.5")
        self.client.drain()

        self.enter(side="BUY", quantity=100, price="2845.5")

        report = self.client.reports()[0]
        self.assertEqual(D.ExecType.FILL, report.get(D.EXEC_TYPE))
        self.assertEqual("F2", report.get(D.CL_ORD_ID))
        self.assertEqual("100", report.get(D.LAST_SHARES))
        self.assertEqual("2845.5", report.get(D.LAST_PX))

    def test_the_resting_side_is_reported_as_having_added_liquidity(self):
        self.client.new_order("F3", side=D.SideValue.SELL, quantity=100,
                              price="2845.5")
        self.client.drain()

        self.enter(side="BUY", quantity=100, price="2845.5")

        self.assertEqual(D.LastLiquidityInd.ADDED,
                         self.client.reports()[0].get(D.LAST_LIQUIDITY_IND))

    def test_a_partial_fill_leaves_the_injected_remainder_resting(self):
        self.client.new_order("F4", side=D.SideValue.SELL, quantity=100,
                              price="2845.5")
        self.client.drain()

        result = self.enter(side="BUY", quantity=300, price="2845.5")

        self.assertEqual("PARTIALLY_FILLED", result["order"]["status"])
        self.assertEqual(200, result["order"]["leaves_qty"])

    def test_an_injected_ioc_cancels_its_remainder(self):
        self.client.new_order("F5", side=D.SideValue.SELL, quantity=100,
                              price="2845.5")
        self.client.drain()

        result = self.enter(side="BUY", quantity=300, price="2845.5", tif="IOC")

        self.assertEqual(0, result["order"]["leaves_qty"])
        self.assertEqual(100, result["order"]["cum_qty"])

    def test_an_injected_fok_that_cannot_fill_is_cancelled_untouched(self):
        self.client.new_order("F6", side=D.SideValue.SELL, quantity=100,
                              price="2845.5")
        self.client.drain()

        result = self.enter(side="BUY", quantity=300, price="2845.5", tif="FOK")

        self.assertEqual(0, result["order"]["cum_qty"])
        self.assertEqual("CANCELLED", result["order"]["status"])

    def test_no_report_is_invented_for_the_injected_side(self):
        """It has no FIX session, so nothing may be written to one."""
        self.client.new_order("F7", side=D.SideValue.SELL, quantity=100,
                              price="2845.5")
        self.client.drain()

        self.enter(side="BUY", quantity=100, price="2845.5")

        reports = self.client.reports()
        self.assertEqual(1, len(reports), "only the resting side owes a report")

    def test_an_injected_order_can_be_cancelled_administratively(self):
        result = self.enter()
        order_id = result["order"]["order_id"]

        self.harness.command("order.cancel", order_id=order_id)

        self.assertEqual([], self.harness.command("orders", owner="WEB")["orders"])


class PublishingTest(InjectionTest):

    def setUp(self):
        InjectionTest.setUp(self)
        self.events = []
        self.harness.publisher.subscribe(self, ["order:*"])

    def push(self, topic, data):
        self.events.append((topic, data))

    def test_acceptance_is_published_on_the_owners_topic(self):
        self.enter(owner="BOT1")

        self.assertEqual(1, len(self.events))
        topic, data = self.events[0]
        self.assertEqual("order:DAY:BOT1", topic)
        self.assertEqual("OrderAccepted", data["event"])

    def test_a_rejection_is_published_too(self):
        self.enter(price="9999.0")

        self.assertEqual("OrderRejected", self.events[0][1]["event"])

    def test_fill_prices_are_published_formatted_not_as_raw_units(self):
        self.harness.client(CLIENT1).new_order(
            "P1", side=D.SideValue.SELL, quantity=100, price="2845.5")

        self.enter(side="BUY", quantity=100, price="2845.5")

        fills = [data for _topic, data in self.events
                 if data["event"] == "OrderFilled"]
        self.assertEqual(1, len(fills))
        self.assertEqual("2845.5", fills[0]["price"])


if __name__ == "__main__":
    unittest.main()
