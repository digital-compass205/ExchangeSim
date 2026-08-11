"""Identifier generation for orders, executions and trades.

The Japannext specification caps OrderID and ExecID at 20 characters but does
not prescribe a format, so these are chosen to be short, strictly increasing
and self-describing: a prefix, the session date, and a zero-padded counter.
Monotonicity matters because tests and log readers rely on ordering.
"""

import itertools


class IdGenerator(object):
    """Produces identifiers of the form ``<prefix><date><counter>``."""

    __slots__ = ("prefix", "date", "width", "_counter", "_max_length")

    def __init__(self, prefix, date="", width=9, max_length=20):
        self.prefix = prefix
        self.date = date
        self.width = width
        self._max_length = max_length
        self._counter = itertools.count(1)

        fixed = len(prefix) + len(date) + width
        if fixed > max_length:
            raise ValueError(
                "identifier format '%s%s' + %d digits exceeds %d characters"
                % (prefix, date, width, max_length))

    def next(self):
        value = next(self._counter)
        identifier = "%s%s%0*d" % (self.prefix, self.date, self.width, value)
        if len(identifier) > self._max_length:
            # The counter has outgrown its padding; still valid, just wider.
            raise OverflowError(
                "identifier counter exhausted for prefix '%s'" % self.prefix)
        return identifier

    def peek(self):
        """The number the next call will use, without consuming it."""
        return self._counter.__reduce__()[1][0]


class SequenceGenerator(object):
    """A plain monotonic counter, used for book time priority."""

    __slots__ = ("_counter",)

    def __init__(self, start=1):
        self._counter = itertools.count(start)

    def next(self):
        return next(self._counter)
