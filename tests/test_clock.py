"""Clock behaviour and FIX timestamp formatting."""

import unittest
from datetime import datetime

from exchangesim.core.clock import FixedClock, RealClock, format_utc, parse_utc


class TimestampTest(unittest.TestCase):

    def test_format_truncates_to_milliseconds(self):
        moment = datetime(2026, 8, 8, 1, 2, 3, 456789)
        self.assertEqual("20260808-01:02:03.456", format_utc(moment))

    def test_format_pads_sub_millisecond_values(self):
        moment = datetime(2026, 8, 8, 1, 2, 3, 7000)
        self.assertEqual("20260808-01:02:03.007", format_utc(moment))

    def test_parse_accepts_millisecond_form(self):
        self.assertEqual(datetime(2026, 8, 8, 1, 2, 3, 456000),
                         parse_utc("20260808-01:02:03.456"))

    def test_parse_accepts_second_form(self):
        # Permitted by FIX 4.2 and emitted by some clients.
        self.assertEqual(datetime(2026, 8, 8, 1, 2, 3),
                         parse_utc("20260808-01:02:03"))

    def test_format_parse_round_trip(self):
        moment = datetime(2026, 12, 31, 23, 59, 59, 999000)
        self.assertEqual(moment, parse_utc(format_utc(moment)))


class FixedClockTest(unittest.TestCase):

    def test_advance_moves_wall_and_monotonic_together(self):
        clock = FixedClock(datetime(2026, 1, 1, 9, 0, 0))
        start = clock.monotonic()

        clock.advance(1.5)

        self.assertEqual(datetime(2026, 1, 1, 9, 0, 1, 500000), clock.now())
        self.assertAlmostEqual(start + 1.5, clock.monotonic())

    def test_set_moves_wall_time_only(self):
        clock = FixedClock(datetime(2026, 1, 1))
        clock.advance(10.0)
        monotonic = clock.monotonic()

        clock.set(datetime(2027, 6, 1))

        self.assertEqual(datetime(2027, 6, 1), clock.now())
        self.assertAlmostEqual(monotonic, clock.monotonic())

    def test_timestamp_uses_fix_format(self):
        clock = FixedClock(datetime(2026, 8, 8, 12, 0, 0, 123000))
        self.assertEqual("20260808-12:00:00.123", clock.timestamp())


class RealClockTest(unittest.TestCase):

    def test_monotonic_never_decreases(self):
        clock = RealClock()
        first = clock.monotonic()
        second = clock.monotonic()
        self.assertGreaterEqual(second, first)

    def test_timestamp_is_well_formed(self):
        stamp = RealClock().timestamp()
        self.assertEqual(21, len(stamp))
        self.assertEqual("-", stamp[8])
        parse_utc(stamp)  # raises if malformed


if __name__ == "__main__":
    unittest.main()
