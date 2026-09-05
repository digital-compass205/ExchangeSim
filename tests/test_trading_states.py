"""Which trading phases a venue has, and what happens when you ask for another.

The core supports every phase any venue here needs -- a pre-open, two auctions,
a lunch break -- which is not a claim that any one venue has all of them. NSE's
Normal market runs continuously from open to close and has no lunch break;
Japannext's Trading Rules say outright that it runs no auction. Until this file
existed, ``state.set`` accepted the whole core vocabulary at every venue, and
each venue's state-to-wire-status map quietly folded the phases it does not have
onto the nearest thing it could say -- so setting NSE to LUNCH_BREAK put the
book in a state the venue then reported to clients as CLOSED.

Two halves, and both matter:

* the venue **offers** only what it runs, so the board's phase menu never shows
  a button whose only possible outcome is an error;
* and it **refuses** the rest, so a client that hard-codes the core's vocabulary
  is told plainly rather than silently given a different phase.

The maps stay total, deliberately: they are keyed on a core enum, and a fallback
that can no longer be reached through a command is cheaper than a KeyError on
some future path that sets a state directly. That is asserted here too, so
nobody "tidies" them away on the strength of this file.
"""

import unittest

from exchangesim.control.commands import CommandError
from exchangesim.core.enums import TradingState
from exchangesim.venues.japannext import rules as jnx_rules
from exchangesim.venues.nse import rules as nse_rules
from exchangesim.venues.nsefo import rules as nsefo_rules

from .hkexsupport import VenueHarness as HkexHarness
from .jnxsupport import VenueHarness as JapannextHarness
from .nsesupport import VenueHarness as NseHarness
from .nsefosupport import VenueHarness as NsefoHarness


class DeclaredPhasesTest(unittest.TestCase):
    """What each venue says it runs, checked against why."""

    def _states(self, harness):
        return harness.dispatch("markets", {})["states"]

    def test_japannext_runs_no_auction_and_no_break(self):
        harness = JapannextHarness()
        self.addCleanup(harness.close)

        self.assertEqual(
            [TradingState.OPEN, TradingState.CLOSED, TradingState.HALTED],
            self._states(harness))

    def test_nse_has_a_pre_open_but_no_lunch_break(self):
        harness = NseHarness()
        self.addCleanup(harness.close)

        states = self._states(harness)
        self.assertIn(TradingState.PRE_OPEN, states)
        self.assertIn(TradingState.OPENING_AUCTION, states)
        self.assertNotIn(TradingState.LUNCH_BREAK, states)
        self.assertNotIn(TradingState.CLOSING_AUCTION, states)

    def test_hkex_runs_the_whole_vocabulary(self):
        """POS and CAS are both built and the midday break is a published
        session, so HKEX is the one venue that uses every phase."""
        harness = HkexHarness()
        self.addCleanup(harness.close)

        self.assertEqual(list(TradingState.ORDER), self._states(harness))

    def test_nsefo_runs_no_preopen_and_no_postclose(self):
        """F&O publishes a pre-open and a fifth status, Postclose, that CM
        does not have -- neither is built in this slice (see
        rules.NOT_IMPLEMENTED), so this venue runs the plainest cycle of the
        four."""
        harness = NsefoHarness()
        self.addCleanup(harness.close)

        self.assertEqual(
            [TradingState.OPEN, TradingState.CLOSED, TradingState.HALTED],
            self._states(harness))

    def test_the_phases_are_offered_in_the_order_a_session_runs_them(self):
        """A menu, not a set. Alphabetical order would read as nonsense."""
        for harness in (JapannextHarness(), NseHarness(), HkexHarness(),
                        NsefoHarness()):
            self.addCleanup(harness.close)
            states = self._states(harness)
            positions = [TradingState.ORDER.index(name) for name in states]
            self.assertEqual(sorted(positions), positions)


class RefusalTest(unittest.TestCase):
    """A phase the venue does not run is an error, not a near-enough match."""

    def _refused(self, harness, state):
        with self.assertRaises(CommandError) as caught:
            harness.dispatch("state.set", {"state": state})
        return str(caught.exception)

    def test_nse_refuses_a_lunch_break_it_does_not_have(self):
        harness = NseHarness()
        self.addCleanup(harness.close)

        message = self._refused(harness, TradingState.LUNCH_BREAK)
        self.assertIn(TradingState.OPEN, message)

    def test_japannext_refuses_a_closing_auction_its_rules_rule_out(self):
        harness = JapannextHarness()
        self.addCleanup(harness.close)

        self._refused(harness, TradingState.CLOSING_AUCTION)

    def test_hkex_accepts_every_phase(self):
        harness = HkexHarness()
        self.addCleanup(harness.close)

        for state in TradingState.ORDER:
            harness.dispatch("state.set", {"state": state})

    def test_nsefo_refuses_a_pre_open_it_does_not_have(self):
        harness = NsefoHarness()
        self.addCleanup(harness.close)

        message = self._refused(harness, TradingState.PRE_OPEN)
        self.assertIn(TradingState.OPEN, message)

    def test_a_phase_the_core_has_never_heard_of_is_still_refused(self):
        harness = NseHarness()
        self.addCleanup(harness.close)

        self._refused(harness, "TEA_BREAK")

    def test_a_venue_still_reaches_the_phases_it_does_run(self):
        """The refusal must not have narrowed anything real away."""
        harness = NseHarness()
        self.addCleanup(harness.close)

        for state in (TradingState.PRE_OPEN, TradingState.OPENING_AUCTION,
                      TradingState.OPEN, TradingState.CLOSED,
                      TradingState.HALTED):
            result = harness.dispatch("state.set", {"state": state})
            self.assertEqual(state, result["state"])

    def test_nsefo_still_reaches_the_phases_it_does_run(self):
        harness = NsefoHarness()
        self.addCleanup(harness.close)

        for state in (TradingState.OPEN, TradingState.CLOSED,
                      TradingState.HALTED):
            result = harness.dispatch("state.set", {"state": state})
            self.assertEqual(state, result["state"])


class WireStatusMapTest(unittest.TestCase):
    """The maps stay total even where the phase is now unreachable.

    Narrowing ``trading_states`` is a statement about which phases a *command*
    may select. It is not licence to delete a mapping: these are keyed on a core
    enum, and any future path that sets a state directly would otherwise raise a
    KeyError deep inside a report renderer rather than being refused up front.
    """

    def test_japannext_still_maps_every_core_phase(self):
        self.assertEqual(set(TradingState.ALL),
                         set(jnx_rules.STATE_TO_TRAD_SES_STATUS))

    def test_nse_still_maps_every_core_phase(self):
        self.assertEqual(set(TradingState.ALL),
                         set(nse_rules.STATE_TO_MARKET_STATUS))

    def test_nsefo_still_maps_every_core_phase(self):
        self.assertEqual(set(TradingState.ALL),
                         set(nsefo_rules.STATE_TO_MARKET_STATUS))
