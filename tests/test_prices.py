"""Exact price arithmetic.

Japannext prices carry one decimal place and tick sizes go down to 0.1, which
is precisely the value binary floating point cannot represent. These tests pin
the integer representation that keeps matching exact.
"""

import unittest

from exchangesim.core.prices import PriceCodec, PriceError


class ParseTest(unittest.TestCase):

    def setUp(self):
        self.codec = PriceCodec(decimals=1)

    def test_whole_numbers(self):
        self.assertEqual(28450, self.codec.parse("2845"))

    def test_one_decimal_place(self):
        self.assertEqual(28455, self.codec.parse("2845.5"))

    def test_tenths_that_floats_cannot_represent(self):
        self.assertEqual(1001, self.codec.parse("100.1"))
        self.assertEqual(3, self.codec.parse("0.3"))

    def test_repeated_parsing_is_stable(self):
        # The classic float failure: 0.1 * 3 != 0.3.
        self.assertEqual(self.codec.parse("0.1") * 3, self.codec.parse("0.3"))

    def test_trailing_zeros_are_accepted(self):
        self.assertEqual(28450, self.codec.parse("2845.0"))

    def test_surrounding_whitespace_is_tolerated(self):
        self.assertEqual(28455, self.codec.parse("  2845.5  "))

    def test_excess_precision_is_rejected_not_rounded(self):
        # Silently rounding would make the simulator disagree with the venue,
        # which rejects the order instead.
        with self.assertRaises(PriceError) as caught:
            self.codec.parse("2845.55")
        self.assertIn("decimal place", str(caught.exception))

    def test_non_numeric_input_is_rejected(self):
        with self.assertRaises(PriceError):
            self.codec.parse("not-a-price")

    def test_infinity_is_rejected(self):
        with self.assertRaises(PriceError):
            self.codec.parse("Infinity")

    def test_a_codec_with_more_decimals_accepts_more_precision(self):
        codec = PriceCodec(decimals=4)
        self.assertEqual(28455556, codec.parse("2845.5556"))


class FormatTest(unittest.TestCase):

    def setUp(self):
        self.codec = PriceCodec(decimals=1)

    def test_units_render_with_the_venue_precision(self):
        self.assertEqual("2845.5", self.codec.format(28455))
        self.assertEqual("2845.0", self.codec.format(28450))

    def test_small_values(self):
        self.assertEqual("0.1", self.codec.format(1))

    def test_round_trip(self):
        for text in ("0.1", "100.1", "2845.5", "99999999.9"):
            self.assertEqual(text, self.codec.format(self.codec.parse(text)), text)


class AverageTest(unittest.TestCase):

    def setUp(self):
        self.codec = PriceCodec(decimals=1)

    def test_a_single_fill_averages_to_its_price(self):
        notional = self.codec.parse("2845.5") * 100
        self.assertEqual("2845.5000", self.codec.format_average(notional, 100))

    def test_two_fills_average_between_them(self):
        notional = (self.codec.parse("2846") * 100
                    + self.codec.parse("2848") * 100)
        self.assertEqual("2847.0000", self.codec.format_average(notional, 200))

    def test_an_unfilled_order_averages_zero(self):
        # This is what the specification's Order Accepted report carries.
        self.assertEqual("0.0000", self.codec.format_average(0, 0))

    def test_averages_use_the_four_decimals_avgpx_allows(self):
        notional = (self.codec.parse("100.1") * 1
                    + self.codec.parse("100.2") * 2)
        self.assertEqual("100.1667", self.codec.format_average(notional, 3))

    def test_averaging_rounds_half_up(self):
        codec = PriceCodec(decimals=1)
        # 3 fills totalling 100.05 average exactly, testing the rounding mode.
        notional = codec.parse("0.1") * 1 + codec.parse("0.2") * 1
        self.assertEqual("0.1500", codec.format_average(notional, 2))


class NotionalTest(unittest.TestCase):

    def setUp(self):
        self.codec = PriceCodec(decimals=1)

    def test_order_value_in_whole_currency_units(self):
        self.assertEqual(284550, self.codec.notional(self.codec.parse("2845.5"), 100))

    def test_fractional_value_rounds_up_so_limits_are_not_undershot(self):
        # 0.1 * 1 share = 0.1 JPY, which must not count as zero.
        self.assertEqual(1, self.codec.notional(self.codec.parse("0.1"), 1))

    def test_exact_notional_keeps_the_fraction(self):
        self.assertEqual("0.1",
                         str(self.codec.exact_notional(self.codec.parse("0.1"), 1)))

    def test_the_hundred_million_yen_cap_is_representable(self):
        # Japannext's standard FIX order value limit.
        value = self.codec.notional(self.codec.parse("1000.0"), 100000)
        self.assertEqual(100000000, value)


class TickTest(unittest.TestCase):

    def setUp(self):
        self.codec = PriceCodec(decimals=1)

    def test_a_price_on_a_tick_boundary(self):
        tick = self.codec.parse("0.5")
        self.assertTrue(self.codec.is_multiple_of(self.codec.parse("100.5"), tick))

    def test_a_price_off_a_tick_boundary(self):
        tick = self.codec.parse("0.5")
        self.assertFalse(self.codec.is_multiple_of(self.codec.parse("100.1"), tick))

    def test_a_zero_tick_accepts_anything(self):
        self.assertTrue(self.codec.is_multiple_of(self.codec.parse("100.1"), 0))


if __name__ == "__main__":
    unittest.main()
