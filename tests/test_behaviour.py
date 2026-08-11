"""Injected behaviour: rule matching, exhaustion, and its effect on the wire."""

import unittest

from exchangesim.core.behaviour import BehaviourStore, DELAY, DROP, REJECT
from exchangesim.core.enums import RejectReason
from exchangesim.venues.japannext import dictionary as D

from .coresupport import OrderFactory
from .jnxsupport import CLIENT1, SYMBOL, VenueHarness


class RuleMatchingTest(unittest.TestCase):

    def setUp(self):
        self.store = BehaviourStore()
        self.order = OrderFactory()

    def test_an_unfiltered_rule_matches_any_order(self):
        self.store.add(REJECT)
        self.assertIsNotNone(self.store.find(REJECT, self.order.buy(100, "100")))

    def test_a_rule_only_matches_its_own_action(self):
        self.store.add(REJECT)
        self.assertIsNone(self.store.find(DROP, self.order.buy(100, "100")))

    def test_a_symbol_filter_is_honoured(self):
        self.store.add(REJECT, symbol="7203")

        self.assertIsNotNone(self.store.find(
            REJECT, self.order.buy(100, "100", symbol="7203")))
        self.assertIsNone(self.store.find(
            REJECT, self.order.buy(100, "100", symbol="9984")))

    def test_a_session_filter_is_honoured(self):
        self.store.add(REJECT, session_key="S1")

        self.assertIsNotNone(self.store.find(
            REJECT, self.order.buy(100, "100", session="S1")))
        self.assertIsNone(self.store.find(
            REJECT, self.order.buy(100, "100", session="S2")))

    def test_filters_combine(self):
        self.store.add(REJECT, session_key="S1", symbol="7203")

        self.assertIsNone(self.store.find(
            REJECT, self.order.buy(100, "100", session="S1", symbol="9984")))

    def test_an_unknown_action_is_refused(self):
        with self.assertRaises(ValueError):
            self.store.add("explode")


class ExhaustionTest(unittest.TestCase):

    def setUp(self):
        self.store = BehaviourStore()
        self.order = OrderFactory()

    def test_a_rule_fires_only_as_often_as_asked(self):
        self.store.add(REJECT, count=2)

        for _ in range(2):
            self.assertIsNotNone(self.store.take(REJECT, self.order.buy(100, "1")))
        self.assertIsNone(self.store.take(REJECT, self.order.buy(100, "1")))

    def test_an_exhausted_rule_is_reaped(self):
        self.store.add(REJECT, count=1)
        self.store.take(REJECT, self.order.buy(100, "1"))

        self.assertEqual([], self.store.describe())
        self.assertFalse(self.store.active)

    def test_a_zero_count_means_unlimited(self):
        self.store.add(REJECT, count=0)

        for _ in range(50):
            self.assertIsNotNone(self.store.take(REJECT, self.order.buy(100, "1")))
        self.assertTrue(self.store.active)

    def test_remaining_is_reported(self):
        self.store.add(REJECT, count=3)
        self.store.take(REJECT, self.order.buy(100, "1"))

        self.assertEqual(1, self.store.describe()[0]["applied"])
        self.assertEqual(2, self.store.describe()[0]["remaining"])

    def test_an_unlimited_rule_reports_no_remaining_count(self):
        self.store.add(REJECT, count=0)
        self.assertIsNone(self.store.describe()[0]["remaining"])


class ManagementTest(unittest.TestCase):

    def setUp(self):
        self.store = BehaviourStore()

    def test_rules_receive_increasing_identifiers(self):
        first = self.store.add(REJECT)
        second = self.store.add(DROP)
        self.assertLess(first.id, second.id)

    def test_a_rule_can_be_removed_by_id(self):
        rule = self.store.add(REJECT)

        self.assertTrue(self.store.remove(rule.id))
        self.assertEqual([], self.store.describe())

    def test_removing_an_unknown_id_reports_failure(self):
        self.assertFalse(self.store.remove(999))

    def test_clear_removes_everything_and_counts(self):
        self.store.add(REJECT)
        self.store.add(DROP)

        self.assertEqual(2, self.store.clear())
        self.assertFalse(self.store.active)

    def test_an_empty_store_is_inactive(self):
        self.assertFalse(self.store.active)


class VenueIntegrationTest(unittest.TestCase):
    """The rules must actually change what goes on the wire."""

    def setUp(self):
        self.harness = VenueHarness()
        self.addCleanup(self.harness.close)
        self.client = self.harness.client(CLIENT1)

    def test_a_forced_reject_overrides_a_valid_order(self):
        self.harness.command("behaviour.set", action="reject", count=1,
                             reason="OTHER", text="forced by test")

        self.client.new_order("B1", quantity=100, price="2845.5")

        report = self.client.reports()[0]
        self.assertEqual(D.ExecType.REJECTED, report.get(D.EXEC_TYPE))
        self.assertEqual(str(D.OrdRejReason.OTHER), report.get(D.ORD_REJ_REASON))
        self.assertEqual("forced by test", report.get("58") or report.get(58))

    def test_the_reject_reason_is_mapped_to_the_venue_code(self):
        self.harness.command("behaviour.set", action="reject", count=1,
                             reason=RejectReason.MARKET_CLOSED)

        self.client.new_order("B1")

        self.assertEqual(str(D.OrdRejReason.VENUE_CLOSED),
                         self.client.reports()[0].get(D.ORD_REJ_REASON))

    def test_normal_behaviour_resumes_once_the_rule_is_spent(self):
        self.harness.command("behaviour.set", action="reject", count=1)
        self.client.new_order("B1")
        self.client.drain()

        self.client.new_order("B2")

        self.assertEqual(D.ExecType.NEW,
                         self.client.reports()[0].get(D.EXEC_TYPE))

    def test_a_dropped_report_never_reaches_the_client(self):
        self.harness.command("behaviour.set", action="drop", count=1,
                             symbol=SYMBOL)

        self.client.new_order("B1", quantity=100, price="2845.5")

        self.assertEqual([], self.client.reports())
        # The order really did reach the book.
        self.assertEqual(1, len(self.harness.command("orders")["orders"]))

    def test_a_symbol_filter_limits_the_blast_radius(self):
        self.harness.command("behaviour.set", action="drop", count=5,
                             symbol="9984")

        self.client.new_order("B1", symbol=SYMBOL, price="2845.5")

        self.assertEqual(1, len(self.client.reports()))

    def test_a_delayed_report_is_withheld_until_the_timer_fires(self):
        self.harness.command("behaviour.set", action="delay", count=1,
                             delay_ms=500)

        self.client.new_order("B1", quantity=100, price="2845.5")

        self.assertEqual([], self.client.reports(), "should not have arrived yet")

        # Advance the injected clock and let the reactor run the timer.
        self.harness.clock.advance(0.6)
        self.harness.reactor.step(0.0)

        self.assertEqual(D.ExecType.NEW,
                         self.client.reports()[0].get(D.EXEC_TYPE))

    def test_a_delay_rule_needs_a_duration(self):
        from exchangesim.control.commands import CommandError

        with self.assertRaises(CommandError):
            self.harness.command("behaviour.set", action="delay", count=1)

    def test_an_unknown_reason_is_refused_with_the_valid_list(self):
        from exchangesim.control.commands import CommandError

        with self.assertRaises(CommandError) as caught:
            self.harness.command("behaviour.set", action="reject",
                                 reason="NOT_A_REASON")

        self.assertIn("UNKNOWN_SYMBOL", str(caught.exception))

    def test_rules_are_listed_and_cleared_through_the_control_plane(self):
        self.harness.command("behaviour.set", action="reject", count=3)

        self.assertEqual(1, len(self.harness.command("behaviour.list")["rules"]))

        self.harness.command("behaviour.clear")

        self.assertEqual([], self.harness.command("behaviour.list")["rules"])

    def test_a_single_rule_can_be_cleared_by_id(self):
        first = self.harness.command("behaviour.set", action="reject", count=3)
        self.harness.command("behaviour.set", action="drop", count=3)

        self.harness.command("behaviour.clear", id=first["id"])

        remaining = self.harness.command("behaviour.list")["rules"]
        self.assertEqual(1, len(remaining))
        self.assertEqual("drop", remaining[0]["action"])


if __name__ == "__main__":
    unittest.main()
