"""Injected behaviour, for testing a client's unhappy paths.

A client's error handling is the part hardest to exercise against a real
exchange: you cannot ask a venue to reject your next order, or to go quiet for
five seconds, on demand. These rules make those situations reproducible.

Three actions are supported:

``reject``
    Force the next matching new orders to be rejected with a chosen reason,
    ahead of ordinary validation.
``delay``
    Hold outbound reports for a number of milliseconds, so a client's
    response-timeout logic runs.
``drop``
    Discard outbound reports entirely, simulating a lost message that the
    client must recover by resend.

Deliberately **not** supported: injecting PendingNew / PendingCancel reports.
The Japannext dialect's ``ExecType(150)`` has no pending value -- its
enumeration is 0, 1, 2, 4, 5, 8 and I -- so a pending ExecutionReport is not
something a conformant client would ever receive from this venue, and emitting
one would be testing against fiction.
"""

import itertools
import logging

log = logging.getLogger(__name__)

REJECT = "reject"
DELAY = "delay"
DROP = "drop"

ACTIONS = frozenset((REJECT, DELAY, DROP))

#: A count of 0 means "apply until removed".
UNLIMITED = 0


class Rule(object):
    """One injected behaviour and the orders it applies to."""

    __slots__ = ("id", "action", "count", "applied", "session_key", "symbol",
                 "market", "reason", "text", "delay_ms")

    def __init__(self, rule_id, action, count=1, session_key=None, symbol=None,
                 market=None, reason=None, text="", delay_ms=0):
        self.id = rule_id
        self.action = action
        #: How many times the rule may fire; 0 means unlimited.
        self.count = count
        self.applied = 0
        self.session_key = session_key
        self.symbol = symbol
        self.market = market
        self.reason = reason
        self.text = text
        self.delay_ms = delay_ms

    @property
    def exhausted(self):
        return self.count != UNLIMITED and self.applied >= self.count

    @property
    def remaining(self):
        if self.count == UNLIMITED:
            return None
        return max(0, self.count - self.applied)

    def matches(self, order):
        if self.exhausted:
            return False
        if self.session_key is not None and order.session_key != self.session_key:
            return False
        if self.symbol is not None and order.symbol != self.symbol:
            return False
        if self.market is not None and order.market != self.market:
            return False
        return True

    def fire(self):
        self.applied += 1
        return self

    def describe(self):
        return {
            "id": self.id,
            "action": self.action,
            "count": self.count or "unlimited",
            "applied": self.applied,
            "remaining": self.remaining,
            "session": "%s" % (self.session_key,) if self.session_key else None,
            "symbol": self.symbol,
            "market": self.market,
            "reason": self.reason,
            "delay_ms": self.delay_ms or None,
            "text": self.text or None,
        }


class BehaviourStore(object):
    """The active rule set. Empty in normal operation, and free when empty."""

    def __init__(self):
        self._rules = []
        self._ids = itertools.count(1)

    # -- management --------------------------------------------------------

    def add(self, action, **kwargs):
        if action not in ACTIONS:
            raise ValueError("unknown behaviour action '%s'; expected one of: %s"
                             % (action, ", ".join(sorted(ACTIONS))))
        rule = Rule(next(self._ids), action, **kwargs)
        self._rules.append(rule)
        log.info("behaviour rule %d added: %s", rule.id, rule.describe())
        return rule

    def remove(self, rule_id):
        for index, rule in enumerate(self._rules):
            if rule.id == rule_id:
                self._rules.pop(index)
                log.info("behaviour rule %d removed", rule_id)
                return True
        return False

    def clear(self):
        count = len(self._rules)
        self._rules = []
        if count:
            log.info("cleared %d behaviour rule(s)", count)
        return count

    def rules(self):
        return list(self._rules)

    def describe(self):
        return [rule.describe() for rule in self._rules]

    @property
    def active(self):
        return any(not rule.exhausted for rule in self._rules)

    # -- matching ----------------------------------------------------------

    def find(self, action, order):
        """The first non-exhausted rule of ``action`` matching ``order``."""
        if not self._rules:
            return None
        for rule in self._rules:
            if rule.action == action and rule.matches(order):
                return rule
        return None

    def take(self, action, order):
        """Find a matching rule and consume one of its applications."""
        rule = self.find(action, order)
        if rule is None:
            return None
        rule.fire()
        self._reap()
        return rule

    def _reap(self):
        """Drop rules that have fired as often as they were asked to."""
        self._rules = [rule for rule in self._rules if not rule.exhausted]
