"""Market phase state machine.

Two scopes, with instrument-level overriding market-level. That is what lets an
operator halt one name while the rest of the market keeps trading -- the case
the Japannext trading rules describe, where a security is suspended because its
primary exchange suspended it.

Transitions are entirely command-driven: there is no schedule and no timer. A
CI job cannot wait six hours for the afternoon session, so the simulator simply
sits in whatever phase it was told to occupy.

The full phase set (pre-open, auctions, lunch break) exists here because the
core must serve venues that have them. Japannext itself uses only OPEN, CLOSED
and HALTED -- its rules state that it conducts no opening or closing auctions.
"""

import logging

from .enums import TradingState
from .events import TradingStateChanged

log = logging.getLogger(__name__)


class TradingStateMachine(object):
    """Tracks the phase of a market and of individual instruments within it."""

    def __init__(self, market, initial=TradingState.CLOSED):
        if initial not in TradingState.ALL:
            raise ValueError("unknown trading state %r" % (initial,))
        self.market = market
        self._market_state = initial
        self._instrument_states = {}

    # -- queries -----------------------------------------------------------

    @property
    def market_state(self):
        return self._market_state

    def state_for(self, symbol):
        """The effective phase for an instrument: its override, else the market."""
        return self._instrument_states.get(symbol, self._market_state)

    def has_override(self, symbol):
        return symbol in self._instrument_states

    def overrides(self):
        return dict(self._instrument_states)

    def allows_entry(self, symbol):
        """False when the phase refuses new orders outright."""
        return self.state_for(symbol) not in TradingState.NO_ENTRY

    def allows_matching(self, symbol):
        """True only during continuous trading."""
        return self.state_for(symbol) in TradingState.CONTINUOUS

    def is_accumulating(self, symbol):
        """True when orders rest without matching, as in an auction call."""
        return self.state_for(symbol) in TradingState.ACCUMULATING

    # -- transitions -------------------------------------------------------

    def set_market_state(self, state):
        """Move the whole market. Returns the events to publish."""
        state = _validate(state)
        previous = self._market_state
        if previous == state:
            return []
        self._market_state = state
        log.info("market %s: %s -> %s", self.market, previous, state)

        events = [TradingStateChanged(self.market, None, previous, state)]
        return events

    def set_instrument_state(self, symbol, state):
        """Override one instrument's phase."""
        state = _validate(state)
        previous = self.state_for(symbol)
        if previous == state and self.has_override(symbol):
            return []
        self._instrument_states[symbol] = state
        log.info("market %s instrument %s: %s -> %s",
                 self.market, symbol, previous, state)
        return [TradingStateChanged(self.market, symbol, previous, state)]

    def clear_instrument_state(self, symbol):
        """Drop an override so the instrument follows the market again."""
        if symbol not in self._instrument_states:
            return []
        previous = self._instrument_states.pop(symbol)
        current = self._market_state
        log.info("market %s instrument %s: override cleared, %s -> %s",
                 self.market, symbol, previous, current)
        if previous == current:
            return []
        return [TradingStateChanged(self.market, symbol, previous, current)]

    def clear_all_overrides(self):
        events = []
        for symbol in list(self._instrument_states):
            events.extend(self.clear_instrument_state(symbol))
        return events

    # -- presentation ------------------------------------------------------

    def describe(self):
        return {
            "market": self.market,
            "state": self._market_state,
            "overrides": self.overrides(),
        }

    def __repr__(self):
        return "TradingStateMachine(%s, %s, %d overrides)" % (
            self.market, self._market_state, len(self._instrument_states))


def _validate(state):
    normalised = str(state).upper()
    if normalised not in TradingState.ALL:
        raise ValueError("unknown trading state '%s'; expected one of: %s"
                         % (state, ", ".join(sorted(TradingState.ALL))))
    return normalised
