"""Time sources.

Two distinct notions of time exist in the simulator and they must not be
confused:

* **Wall time** -- what goes into FIX ``SendingTime(52)``. A conformant client
  compares this against its own clock and will session-reject with
  ``SessionRejectReason=10`` if it drifts, so this is *always* the real clock
  in a running simulator.
* **Venue time** -- business timestamps such as ``TransactTime(60)`` and trade
  times. Identical to wall time at runtime, but tests freeze it so that
  generated messages are byte-for-byte reproducible.

Both are served by a ``Clock``. Production wiring passes a :class:`RealClock`
everywhere; tests pass a :class:`FixedClock`. Because market state transitions
are command-driven (there is no scheduler), no accelerated clock is needed.
"""

import time
from datetime import datetime, timedelta

#: FIX UTCTimestamp format with millisecond precision.
FIX_TIMESTAMP_FMT = "%Y%m%d-%H:%M:%S.%f"


def format_utc(dt):
    # type: (datetime) -> str
    """Render a datetime as a FIX UTCTimestamp, truncated to milliseconds."""
    return dt.strftime(FIX_TIMESTAMP_FMT)[:-3]


def parse_utc(text):
    # type: (str) -> datetime
    """Parse a FIX UTCTimestamp.

    Accepts both the millisecond form (``20260808-01:02:03.456``) and the
    second form (``20260808-01:02:03``). ``datetime.fromisoformat`` does not
    exist on Python 3.6, and the FIX format is not ISO 8601 anyway.
    """
    if "." in text:
        return datetime.strptime(text, FIX_TIMESTAMP_FMT)
    return datetime.strptime(text, "%Y%m%d-%H:%M:%S")


class Clock(object):
    """Interface for a time source."""

    __slots__ = ()

    def now(self):
        # type: () -> datetime
        """Current UTC time as a naive datetime."""
        raise NotImplementedError

    def monotonic(self):
        # type: () -> float
        """Seconds from an arbitrary origin, never decreasing.

        Used for timers and heartbeat policing, never for timestamps.
        """
        raise NotImplementedError

    def timestamp(self):
        # type: () -> str
        """Current time as a FIX UTCTimestamp string."""
        return format_utc(self.now())


class RealClock(Clock):
    """Wall-clock time. The only clock used by a running simulator."""

    __slots__ = ()

    def now(self):
        return datetime.utcnow()

    def monotonic(self):
        return time.monotonic()


class FixedClock(Clock):
    """Manually advanced clock, for tests.

    Both :meth:`now` and :meth:`monotonic` advance together via :meth:`advance`,
    so a test can drive heartbeat timeouts without sleeping.
    """

    __slots__ = ("_now", "_monotonic")

    def __init__(self, start=None):
        # type: (datetime) -> None
        self._now = start if start is not None else datetime(2026, 1, 1, 0, 0, 0)
        self._monotonic = 0.0

    def now(self):
        return self._now

    def monotonic(self):
        return self._monotonic

    def advance(self, seconds):
        # type: (float) -> None
        """Move both clocks forward by ``seconds``."""
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds

    def set(self, when):
        # type: (datetime) -> None
        """Jump wall time to ``when`` without disturbing the monotonic clock."""
        self._now = when
