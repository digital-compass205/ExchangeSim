"""Trading phases: market scope, instrument overrides, entry and matching rules."""

import unittest

from exchangesim.core.enums import TradingState
from exchangesim.core.trading_state import TradingStateMachine


class MarketScopeTest(unittest.TestCase):

    def setUp(self):
        self.machine = TradingStateMachine("DAY", TradingState.CLOSED)

    def test_the_initial_state_is_as_configured(self):
        self.assertEqual(TradingState.CLOSED, self.machine.market_state)

    def test_changing_state_reports_the_transition(self):
        events = self.machine.set_market_state(TradingState.OPEN)

        self.assertEqual(1, len(events))
        self.assertEqual(TradingState.CLOSED, events[0].previous)
        self.assertEqual(TradingState.OPEN, events[0].current)
        self.assertEqual("market", events[0].scope)
        self.assertIsNone(events[0].symbol)

    def test_setting_the_same_state_is_a_no_op(self):
        self.machine.set_market_state(TradingState.OPEN)
        self.assertEqual([], self.machine.set_market_state(TradingState.OPEN))

    def test_state_names_are_normalised(self):
        self.machine.set_market_state("open")
        self.assertEqual(TradingState.OPEN, self.machine.market_state)

    def test_an_unknown_state_is_rejected_with_the_valid_list(self):
        with self.assertRaises(ValueError) as caught:
            self.machine.set_market_state("SIESTA")
        self.assertIn("OPEN", str(caught.exception))

    def test_an_unknown_initial_state_is_rejected(self):
        with self.assertRaises(ValueError):
            TradingStateMachine("DAY", "NAPPING")


class InstrumentOverrideTest(unittest.TestCase):
    """Halting one name while the rest of the market trades."""

    def setUp(self):
        self.machine = TradingStateMachine("DAY", TradingState.OPEN)

    def test_instruments_follow_the_market_by_default(self):
        self.assertEqual(TradingState.OPEN, self.machine.state_for("7203"))
        self.assertFalse(self.machine.has_override("7203"))

    def test_an_override_applies_to_one_instrument_only(self):
        self.machine.set_instrument_state("7203", TradingState.HALTED)

        self.assertEqual(TradingState.HALTED, self.machine.state_for("7203"))
        self.assertEqual(TradingState.OPEN, self.machine.state_for("9984"))

    def test_an_override_reports_the_transition_with_its_symbol(self):
        events = self.machine.set_instrument_state("7203", TradingState.HALTED)

        self.assertEqual("instrument", events[0].scope)
        self.assertEqual("7203", events[0].symbol)
        self.assertEqual(TradingState.OPEN, events[0].previous)

    def test_an_override_survives_a_market_state_change(self):
        self.machine.set_instrument_state("7203", TradingState.HALTED)
        self.machine.set_market_state(TradingState.CLOSED)

        self.assertEqual(TradingState.HALTED, self.machine.state_for("7203"))

    def test_clearing_an_override_returns_the_instrument_to_the_market(self):
        self.machine.set_instrument_state("7203", TradingState.HALTED)

        events = self.machine.clear_instrument_state("7203")

        self.assertEqual(TradingState.OPEN, self.machine.state_for("7203"))
        self.assertEqual(TradingState.HALTED, events[0].previous)
        self.assertEqual(TradingState.OPEN, events[0].current)

    def test_clearing_an_override_that_matches_the_market_reports_nothing(self):
        self.machine.set_instrument_state("7203", TradingState.OPEN)
        self.assertEqual([], self.machine.clear_instrument_state("7203"))

    def test_clearing_an_absent_override_is_a_no_op(self):
        self.assertEqual([], self.machine.clear_instrument_state("7203"))

    def test_all_overrides_can_be_cleared_at_once(self):
        self.machine.set_instrument_state("7203", TradingState.HALTED)
        self.machine.set_instrument_state("9984", TradingState.HALTED)

        events = self.machine.clear_all_overrides()

        self.assertEqual(2, len(events))
        self.assertEqual({}, self.machine.overrides())


class PermissionTest(unittest.TestCase):

    def _machine(self, state):
        return TradingStateMachine("DAY", state)

    def test_only_open_allows_continuous_matching(self):
        for state in TradingState.ALL:
            machine = self._machine(state)
            self.assertEqual(state == TradingState.OPEN,
                             machine.allows_matching("7203"), state)

    def test_closed_halted_and_lunch_refuse_new_orders(self):
        for state in (TradingState.CLOSED, TradingState.HALTED,
                      TradingState.LUNCH_BREAK):
            self.assertFalse(self._machine(state).allows_entry("7203"), state)

    def test_auction_phases_accept_orders_without_matching(self):
        for state in (TradingState.PRE_OPEN, TradingState.OPENING_AUCTION,
                      TradingState.CLOSING_AUCTION):
            machine = self._machine(state)
            self.assertTrue(machine.allows_entry("7203"), state)
            self.assertFalse(machine.allows_matching("7203"), state)
            self.assertTrue(machine.is_accumulating("7203"), state)

    def test_permissions_respect_an_instrument_override(self):
        machine = self._machine(TradingState.OPEN)
        machine.set_instrument_state("7203", TradingState.HALTED)

        self.assertFalse(machine.allows_entry("7203"))
        self.assertTrue(machine.allows_entry("9984"))

    def test_describe_reports_state_and_overrides(self):
        machine = self._machine(TradingState.OPEN)
        machine.set_instrument_state("7203", TradingState.HALTED)

        described = machine.describe()

        self.assertEqual("DAY", described["market"])
        self.assertEqual(TradingState.OPEN, described["state"])
        self.assertEqual({"7203": TradingState.HALTED}, described["overrides"])


if __name__ == "__main__":
    unittest.main()
